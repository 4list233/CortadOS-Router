# Compaction keep heads on S1 (phase 2)

Status: data, training and eval code only. Nothing is deployed; no scorer is started by this work.

## What it is
A second LoRA adapter on the S1 base (Qwen3-4B-Instruct-2507, 4-bit MLX) that answers two yes/no questions about a
past tool call when a session is compacted:

- **call**: does it still matter that this call happened? (keep the call and its input in the context)
- **result**: must this result stay verbatim, because re-running it would not do? (keep the full result)

P(yes) for each head is the probability of the token "yes", the same way the routing scorer reads its answers.

| Piece | File |
|---|---|
| Labels from what later happened | `cortados_router/compaction/labels.py` |
| SFT data (chat format, split by session) | `s1/compaction_dataset.py` |
| Trainer (night window + studio gate, dry run) | `s1/compaction_train.py`, config `s1/compaction_train.json` |
| Eval vs the phase-1 rules | `s1/compaction_eval.py` → `eval.json`, `eval.md` |
| Tests | `tests/test_compaction_labels.py`, `tests/test_s1_compaction.py` |

## Labels
`label_session` looks forward from each event. `needed_result` is true on a re-read / re-run of the same target
(the agent had to fetch it again), a later verbatim quote of a >= 40-char result line, or a later use of an
identifier (hash, uuid, path, id-like token) first seen in that result. `needed_call` adds a later mention of the
call's target and side effects (writes, edits, commits, installs). Every row carries its evidence (rule + later
index) and its horizon. Events issued in the last 4 messages are `unknown` and never become training data: too
little future to say "never needed", and treating them as "not needed" would teach the heads to drop recent work.

Known label noise: a result can matter without being quoted (the agent read it and acted on it). These labels
under-count need, so the eval's wrong-drop rate is a lower bound, and the switch-on rule below is conservative.

## Data
The prompt is the decision-time state only (goal = last 3 user prompts; the call; result head 800 + tail 400 chars;
the 4 messages up to the issuing one). Nothing after the issuing message is shown: the labels come from that
future, so showing it would leak them. Split by session hash (80/10/10, seed `compaction-v1`); in train only, the
minority class of each head is repeated up to parity (cap 4) because wrong drops are the costly error; valid and
test keep the natural mix so calibration and eval are honest. `stats.json` reports sessions, events, positives
and repetitions per split and head.

## Why a separate adapter
- The routing adapter's promotion (`current.json`, nightly evaluate/promote) is untouched: the trainer writes only
  `s1/runs/compaction-<stamp>/`, refuses any other output dir, and nightly.sh's `runs/20*/` globs never see it.
- Compaction can be trained, evaluated and rolled back on its own clock; a bad compaction adapter cannot move a
  routing band, and a routing promotion cannot change what compaction keeps.
- The heads are only switched on at thresholds where `compaction_eval` shows them beating the phase-1 rules on both
  axes (no higher wrong-drop rate, at least as many chars saved). AUC alone never switches them on.

## Training
`python3 s1/compaction_train.py --data <dir> [--dry-run]`. It asks `overnight.train_policy` (night window,
`CORTADOS_S1_TRAIN_ONLY_IN_WINDOW`, `CORTADOS_S1_NIGHT_WINDOW`) and honours `CORTADOS_S1_TRAIN_ALLOWED=0`; refused,
it exits 75 with the reason. Allowed, it runs `mlx_lm lora` under `python -m cortados_router.studio_capacity gate`,
the same gate as nightly.sh's `gated()`, so it holds the node training lock and the local model gate yields. Not yet
wired into nightly.sh: it should run after the routing steps in the same window, never instead of them.

`compaction_eval.main` (live scoring on the Studio) is a stub; `evaluate()` with an injected scorer is complete.

## Serving plan (not deployed)
- The routing scorer keeps `:8431` and priority. Compaction is off the request path: a compaction decision can wait
  tens of seconds, a routing decision cannot.
- A second scorer instance on the Studio, e.g. `:8433`, serves the compaction adapter: same base weights file,
  `nice`d (lower priority), one request at a time, and it pauses (returns 503, the caller falls back to the rules)
  while a routing request is in flight or the routing scorer's latency is above its budget.
- It yields to training like the local model gate does: each request takes `LOCK_SH` on the studio-gate node lock
  (non-blocking); while a trainer holds `LOCK_EX` it answers 503 and the rules decide.

### Memory on a 32 GB M1 Max
Per instance: 4-bit 4B weights about 2.3 GB, KV cache about 150 KB per token (36 layers x 8 KV heads x 128 dims x
K,V x fp16), so about 0.3 GB at a 2k-token prompt, plus about 0.5-1 GB of MLX runtime and Python. One instance is
about 3-3.5 GB. MLX loads weights into unified memory per process, so two processes do not share them:

| Option | Memory | Cost |
|---|---|---|
| Two instances (routing + compaction) | ~6.5 GB | +3 GB; full isolation; routing never waits on a swap |
| One instance, adapter hot-swap | ~3.5 GB + adapters (tens of MB) | each swap stalls routing; a swap bug corrupts routing answers |

**Recommendation: two instances**, the compaction one started on demand and unloaded after an idle timeout (and
whenever the gate's training intent is set). 3 GB is affordable next to the routing scorer and a local Ollama model
on 32 GB, and it keeps the routing path free of anything compaction does. Revisit hot-swap only if the Studio's
free memory (overnight.free_memory_gb) is regularly short with the compaction instance loaded.
