"""S1's system-prompt prefix cache (s1/student.py PrefixCache): the fixed prefix is processed once per loaded model and
every request runs on its own copy, so no request can change the stored state or another request's. Driven through the
real Scorer.score path with a fake model, tokenizer and cache: no MLX.

The fake cache behaves like mlx_lm's KVCache where aliasing matters: `state = ...` stores the given object as is, and
the fake model appends to it IN PLACE. A scorer that handed a request the stored arrays (the 2026-09 leak) would grow
the stored state and change later answers; these tests would see both."""
import os
import sys
import types
import unittest
import zlib
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "s1"))
sys.path.insert(0, str(REPO))
import student  # noqa: E402


class FakeArr:
    def __init__(self, v):
        self.v = v

    def tolist(self):
        return list(self.v)


class FakeLogits:
    """[0, -1] -> a vector over the vocabulary; indexed by an array of ids -> their logits."""

    def __init__(self, history):
        self.history = tuple(history)

    def __getitem__(self, key):
        if isinstance(key, tuple):
            return self
        return FakeArr([((zlib.crc32(repr((self.history, i)).encode()) % 1000) / 100.0) for i in key.v])


class FakeMx:
    @staticmethod
    def array(v):
        return FakeArr(v)

    @staticmethod
    def clear_cache():
        pass


class KVCache:                                            # named like mlx_lm's, so PREFIX_SAFE_CACHES admits it
    def __init__(self):
        self.tokens = []

    @property
    def state(self):
        return (self.tokens,)

    @state.setter
    def state(self, v):
        (self.tokens,) = v                                # stored as given: aliasing is visible


class RotatingKVCache(KVCache):
    pass


class FakeModel:
    """Appends the fed tokens to every layer's cache in place; the logits depend on the whole history, so a request
    that started from a wrong (or shared, mutated) prefix gets different answers."""

    def __init__(self, layers=2):
        self.layers = layers
        self.processed = 0                                # tokens run through the model, over all calls
        self.prefix_passes = 0

    def __call__(self, x, cache):
        toks = x.v[0]
        self.processed += len(toks)
        for c in cache:
            c.tokens.extend(toks)
        return FakeLogits(cache[0].tokens)


class FakeTok:
    """Character-level: every character one token id (digits are single tokens)."""

    def encode(self, s, add_special_tokens=False):
        return [ord(c) for c in s]

    def apply_chat_template(self, msgs, add_generation_prompt=True, tokenize=True):
        text = "".join(f"<{m['role']}>\n{m['content']}</{m['role']}>" for m in msgs) + "<assistant>\n"
        return self.encode(text)


class ChunkSensitiveModel(FakeModel):
    """Like the real 4-bit/bf16 model on the Studio (2026-11-23): the same tokens prefilled in different chunks round
    differently. Each stored token carries the length of the forward pass that computed it, and the logits depend on
    that, so a full single-pass prefill and a prefix-then-rest prefill give different answers."""

    def __call__(self, x, cache):
        toks = x.v[0]
        self.processed += len(toks)
        for c in cache:
            c.tokens.extend((t, len(toks)) for t in toks)
        return FakeLogits(cache[0].tokens)


def make_scorer(slots=student.PREFIX_SLOTS, cache_cls=KVCache, model_cls=FakeModel):
    model = model_cls()
    copies = {"n": 0}

    def copy_array(a):
        copies["n"] += 1
        return list(a)

    def make_cache(m):
        return [cache_cls() for _ in range(m.layers)]

    s = student.Scorer.__new__(student.Scorer)
    s._setup(FakeMx, make_cache, model, FakeTok(), copy_array=copy_array, materialize=lambda arrays: None,
             prefix_slots=slots)
    return s, model, copies


PREFIX_LEN = len(f"<system>\n{student.SYSTEM}</system><user>\n")   # the system turn and the user turn's opening


class PrefixCacheTests(unittest.TestCase):
    def test_prefix_is_processed_once(self):
        s, model, _ = make_scorer()
        s.score("rename a variable in one file")
        after_first = model.processed
        s.score("plan a database migration")
        s.score("write a haiku")
        self.assertEqual(s.prefixes.builds, 1)
        prefix = s.last_stats["prefix_tokens"]
        self.assertEqual(prefix, PREFIX_LEN)
        # the later requests processed only their own tokens: nothing of the prefix again
        cold, _, _ = make_scorer(slots=0)
        cold.score("plan a database migration"); cold_tokens_2 = cold.model.processed
        cold.score("write a haiku"); cold_tokens_3 = cold.model.processed - cold_tokens_2
        self.assertEqual(model.processed - after_first, (cold_tokens_2 - prefix) + (cold_tokens_3 - prefix))

    def test_answers_match_the_uncached_path(self):
        warm, _, _ = make_scorer()
        cold, _, _ = make_scorer(slots=0)
        for req in ("fix the failing test", "delete the production bucket", "summarize this thread"):
            self.assertEqual(warm.score(req)["probs"], cold.score(req)["probs"], req)
        self.assertEqual(cold.last_stats["prefix_tokens"], 0)

    def test_requests_cannot_affect_each_other_or_the_stored_state(self):
        s, _, copies = make_scorer()
        s.score("first request, long " + "x" * 200)
        (prefix, stored), = s.prefixes._entries.items()
        snapshot = [tuple(tuple(a) for a in layer) for layer in stored]
        self.assertEqual([len(a) for layer in stored for a in layer], [len(prefix)] * len(stored))
        b_after_a = s.score("second request")["probs"]
        fresh, _, _ = make_scorer()
        self.assertEqual(b_after_a, fresh.score("second request")["probs"])   # A left nothing behind for B
        self.assertEqual([tuple(tuple(a) for a in layer) for layer in s.prefixes._entries[prefix]], snapshot)
        self.assertGreater(copies["n"], 0)

    def test_each_request_gets_new_cache_objects_and_arrays(self):
        s, _, _ = make_scorer()
        s.score("warm up")
        prefix = next(iter(s.prefixes._entries))
        stored = s.prefixes._entries[prefix]
        a = s.prefixes.fresh_copy(prefix, FakeMx.array)
        b = s.prefixes.fresh_copy(prefix, FakeMx.array)
        for ca, cb, layer in zip(a, b, stored):
            self.assertIsNot(ca, cb)
            self.assertIsNot(ca.tokens, cb.tokens)
            self.assertIsNot(ca.tokens, layer[0])
            ca.tokens.append(-1)                          # a request writing into its copy
            self.assertEqual(len(cb.tokens), len(prefix))
            self.assertEqual(len(layer[0]), len(prefix))

    def test_memory_is_bounded_to_the_slots(self):
        s, _, _ = make_scorer(slots=1)
        ctx_system = student.SYSTEM + "\nother system variant"    # e.g. the task-context system prompt
        s._system_prefix(ctx_system)
        s.score("plain")
        s.prefixes.fresh_copy(s._system_prefix(ctx_system), FakeMx.array)
        self.assertEqual(len(s.prefixes._entries), 1)     # LRU: the older prefix was dropped
        self.assertEqual(s.prefixes.builds, 2)

    def test_a_reload_starts_with_an_empty_prefix_cache(self):
        # ensure_loaded builds a new Scorer on a promotion; the prefix state belongs to the old one and goes with it
        s1, _, _ = make_scorer()
        s1.score("a")
        s2, model2, _ = make_scorer()
        self.assertEqual(s2.prefixes.builds, 0)
        s2.score("a")
        self.assertEqual(s2.prefixes.builds, 1)

    def test_unknown_cache_classes_disable_reuse(self):
        s, model, _ = make_scorer(cache_cls=RotatingKVCache)
        cold, _, _ = make_scorer(slots=0)
        self.assertEqual(s.score("a request")["probs"], cold.score("a request")["probs"])
        self.assertEqual(s.last_stats["prefix_tokens"], 0)
        self.assertEqual(s.prefixes.builds, 0)

    def test_a_prompt_that_does_not_start_with_the_prefix_runs_in_full(self):
        s, _, _ = make_scorer()
        real = s.tok.apply_chat_template
        s.tok.apply_chat_template = lambda msgs, **k: ([0] + real(msgs, **k)) if msgs[-1]["content"].startswith("unit_kind") else real(msgs, **k)
        s.score("a request")
        self.assertEqual(s.last_stats["prefix_tokens"], 0)


    def test_a_failing_copy_turns_reuse_off_and_scores_in_full(self):
        s, _, _ = make_scorer()
        cold, _, _ = make_scorer(slots=0)
        def boom(a):
            raise TypeError("cannot copy")
        s.prefixes._copy = boom
        self.assertEqual(s.score("a request")["probs"], cold.score("a request")["probs"])
        self.assertEqual(s.last_stats["prefix_tokens"], 0)
        self.assertEqual(s.score("another")["probs"], cold.score("another")["probs"])   # stays off, still correct

    def test_stats_report_slots_and_bytes_on_every_score(self):
        class Sized(list):
            @property
            def nbytes(self):
                return 2 * len(self)                      # e.g. 2 bytes per element
        s, _, _ = make_scorer()
        s.prefixes._copy = lambda a: Sized(a)
        self.assertEqual(s.prefixes.stats(), {"prefix_slots": 0, "prefix_bytes": 0})
        s.score("a request")
        self.assertEqual(s.last_stats["prefix_slots"], 1)
        self.assertEqual(s.last_stats["prefix_bytes"], 2 * PREFIX_LEN * s.model.layers)

    def test_use_prefix_false_runs_the_uncached_path(self):
        # the opt-in deploy check (s1/check_prefix_cache.py) compares both paths on one loaded model
        s, model, _ = make_scorer()
        cold, _, _ = make_scorer(slots=0)
        cached = s.score("compare me")
        uncached = s.score("compare me", use_prefix=False)
        self.assertEqual(s.last_stats["prefix_tokens"], 0)
        self.assertEqual(cached["probs"], uncached["probs"])
        self.assertEqual(uncached["probs"], cold.score("compare me")["probs"])


class PrefixCacheFlagTests(unittest.TestCase):
    """Off by default (CORTADOS_S1_PREFIX_CACHE=on enables it): the deploy of main must not depend on the cache."""

    def env(self, value=None):
        e = {k: v for k, v in os.environ.items() if k != student.PREFIX_CACHE_ENV}
        if value is not None:
            e[student.PREFIX_CACHE_ENV] = value
        return mock.patch.dict(os.environ, e, clear=True)

    def test_flag_values(self):
        for v, on in ((None, False), ("", False), ("off", False), ("1", False), ("true", False), ("on", True), (" ON ", True)):
            with self.env(v):
                self.assertIs(student.prefix_cache_enabled(), on, v)

    def test_default_path_never_touches_the_cache(self):
        with self.env(None):
            s, model, copies = make_scorer(slots=None)
        def boom(*a, **k):
            raise AssertionError("prefix cache touched")
        s.prefixes.fresh_copy = boom
        s.prefixes._stored = boom
        s._system_prefix = boom
        cold, _, _ = make_scorer(slots=0)
        for req in ("fix the failing test", "plan a migration"):
            self.assertEqual(s.score(req)["probs"], cold.score(req)["probs"])
            self.assertEqual(s.last_stats["prefix_tokens"], 0)
        self.assertFalse(s.prefixes.enabled)
        self.assertEqual(s.prefixes.builds, 0)
        self.assertEqual(s.prefixes.stats(), {"prefix_slots": 0, "prefix_bytes": 0})
        self.assertEqual(copies["n"], 0)

    def test_on_enables_reuse(self):
        with self.env("on"):
            s, _, _ = make_scorer(slots=None)
        s.score("a request")
        self.assertEqual(s.last_stats["prefix_tokens"], PREFIX_LEN)
        self.assertEqual(s.prefixes.builds, 1)

    def test_scorer_init_maps_the_flag_and_the_override(self):
        # Scorer.__init__ with fake mlx modules: None follows the env, True/False force it (check_prefix_cache.py: True)
        mx = types.ModuleType("mlx.core")
        mx.set_cache_limit = lambda n: None
        mx.array = FakeMx.array
        mx.eval = lambda *a: None
        mx.clear_cache = lambda: None
        mlx = types.ModuleType("mlx"); mlx.core = mx
        mlx_lm = types.ModuleType("mlx_lm"); mlx_lm.load = lambda base, adapter_path=None: (FakeModel(), FakeTok())
        models = types.ModuleType("mlx_lm.models"); cache_mod = types.ModuleType("mlx_lm.models.cache")
        cache_mod.make_prompt_cache = lambda m: [KVCache() for _ in range(m.layers)]
        mods = {"mlx": mlx, "mlx.core": mx, "mlx_lm": mlx_lm, "mlx_lm.models": models, "mlx_lm.models.cache": cache_mod}
        with mock.patch.dict(sys.modules, mods):
            for env, arg, slots in ((None, None, 0), ("on", None, student.PREFIX_SLOTS), (None, True, student.PREFIX_SLOTS),
                                    ("on", False, 0)):
                with self.env(env):
                    s = student.Scorer("base", None, prefix_cache=arg)
                self.assertEqual(s.prefixes._slots, slots, (env, arg))


class ChunkingTests(unittest.TestCase):
    """Root cause of the 2026-11-22 check failure (28/200 requests differ): not the cache. On the Studio the cached path
    equals an uncached prefill split at the same point bit for bit, while splitting the uncached prefill anywhere (256,
    284, 512, 568, 569) moves the first answer's logits by one bf16 step (0.125) and flips near-ties. The check therefore
    compares the cached path with the split reference (prefill_split), and reports the full-pass difference apart."""

    def test_cached_equals_the_split_reference_and_full_pass_differs(self):
        s, _, _ = make_scorer(model_cls=ChunkSensitiveModel)
        for req in ("fix the failing test", "delete the production bucket"):
            cached = s.score(req)
            self.assertEqual(s.last_stats["prefix_tokens"], PREFIX_LEN)
            ref = s.score(req, use_prefix=False, prefill_split=PREFIX_LEN)
            self.assertEqual(s.last_stats["prefix_tokens"], 0)
            full = s.score(req, use_prefix=False)
            self.assertEqual(cached["probs"], ref["probs"], req)
            self.assertNotEqual(cached["probs"], full["probs"], req)   # chunking alone changes the numbers

    def test_prefill_split_is_ignored_when_the_cache_is_used_or_out_of_range(self):
        s, _, _ = make_scorer(model_cls=ChunkSensitiveModel)
        self.assertEqual(s.score("x", prefill_split=PREFIX_LEN)["probs"], s.score("x")["probs"])
        full = s.score("x", use_prefix=False)["probs"]
        self.assertEqual(s.score("x", use_prefix=False, prefill_split=0)["probs"], full)
        self.assertEqual(s.score("x", use_prefix=False, prefill_split=10**6)["probs"], full)


    def test_the_check_compares_with_the_split_reference(self):
        import check_prefix_cache as check
        s, _, _ = make_scorer(model_cls=ChunkSensitiveModel)
        reqs = ["fix the failing test", "plan a migration", "write a haiku"]
        split = len(s._system_prefix(check.scorer_system()))
        self.assertEqual(split, PREFIX_LEN)
        cached = [s.score(r) for r in reqs]
        ref = [s.score(r, use_prefix=False, prefill_split=split) for r in reqs]
        full = [s.score(r, use_prefix=False) for r in reqs]
        self.assertEqual(check.compare(cached, ref), (0, 0, 0.0))
        self.assertGreater(check.compare(cached, full)[2], 0.0)


if __name__ == "__main__":
    unittest.main()
