"""S1's system-prompt prefix cache (s1/student.py PrefixCache): the fixed prefix is processed once per loaded model and
every request runs on its own copy, so no request can change the stored state or another request's. Driven through the
real Scorer.score path with a fake model, tokenizer and cache: no MLX.

The fake cache behaves like mlx_lm's KVCache where aliasing matters: `state = ...` stores the given object as is, and
the fake model appends to it IN PLACE. A scorer that handed a request the stored arrays (the 2026-09 leak) would grow
the stored state and change later answers; these tests would see both."""
import sys
import unittest
import zlib
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


def make_scorer(slots=student.PREFIX_SLOTS, cache_cls=KVCache):
    model = FakeModel()
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


if __name__ == "__main__":
    unittest.main()
