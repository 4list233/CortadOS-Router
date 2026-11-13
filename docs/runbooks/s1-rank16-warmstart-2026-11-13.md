# Runbook: deploy the rank-16 warm-start fix on the Mac Studio, then lift the training and promotion holds

Prepared 2026-11-13 from a read-only survey of the Studio (`ssh studio`: cat/ls/grep only). **Not executed.** Every
command runs on the Studio (user velox) unless it says otherwise. This closes Part H and open question 1 of
`s1-promote-2026-11-11.md`. Owner decision needed: none beyond running it. The fix refuses a rank mismatch; it does not
warm-start from a rank-8 copy.

## What was wrong (root cause, with evidence)

1. **Warm start at the wrong rank.** `current.json` points at
   `runs/2026-11-08/promoted/2026-11-08-blend0.7/adapter` (promoted 2026-11-12 22:52). Its `adapter_config.json` says
   `lora_parameters {rank 16, scale 20.0}`, `num_layers 16`, model `mlx-community/Qwen3-4B-Instruct-2507-4bit`.
   `lora_params.py rank` on the first 400 KB of its `adapters.safetensors` prints `16 16` (rank 16 on 16 layers). A new
   nightly run copies that file as `0000000_adapters.safetensors` (`CORTADOS_S1_INIT`). Before this fix, `nightly.sh`
   then called `mlx_lm lora` with **no `-c` and no `lora_parameters`**, so mlx_lm 0.31.3 (the Studio venv) used
   `CONFIG_DEFAULTS["lora_parameters"] = {"rank": 8, ...}` (lora.py line 74). It builds rank-8 LoRA layers, then
   `model.load_weights(resume_file, strict=False)` (line 250), which checks no shapes. Line 258 then writes a rank-8
   `adapter_config.json` over the copied rank-16 one. The result is an adapter whose config does not describe its weights.
   Every later load (evaluation, serving) would build rank-8 layers from it.
2. **Vendor usage probes on the nightly path.** In `runs/2026-11-12/nightly.log` lines 59-64, the cycle made 0 teacher
   calls, so label.py opened nothing. Between build_sft.py's `labels: kept 18600 ...` and its `labelled train-split ...
   sft train ...` line, the log shows `cortados scarcity: codex reading failed ...` and `cursor reading failed ... /usage
   screen`. `build_sft.band_of` (and `evaluate.band_of`, the same code) stubbed `core.lane_health` but not `scarcity.current()`.
   resolve's capacity helpers call `scarcity.current()` with refresh on, and a due reading opens the claude, codex,
   cursor-agent and agy CLIs. promote_sweep.py already forced `CORTADOS_SCARCITY_REFRESH=off`.

## What the branch changes (`s1-rank16-warmstart`)

- `s1/lora_params.py` (new, stdlib). Before every `mlx_lm lora` segment it decides the LoRA parameters and writes
  `<run>/lora-train.yaml`, which nightly.sh passes as `-c`.
  - **Warm start or resume:** the rank is read from the start file's tensors and must equal its `adapter_config.json`.
    Scale and dropout come from that config, which must also match on `num_layers` 16, `fine_tune_type` lora and the base.
  - **From scratch:** `CORTADOS_S1_LORA_RANK` / `CORTADOS_S1_LORA_SCALE`, defaulting to 8 / 20.0 (mlx_lm's own defaults,
    which every run before the blend trained at).
  - **Refused, and the night exits 1** (no mlx_lm start, the scorer comes back, `status.json last_lora_refused`):
    - a configured rank or scale that disagrees with the start weights;
    - a config/tensor mismatch;
    - a missing config;
    - mixed ranks;
    - another layer count or base;
    - rank above `CORTADOS_S1_LORA_MAX_RANK` (default 64: each blend promotion adds the two ranks);
    - a later segment of a run whose parameters differ from its first (`<run>/lora-params.json`).
- `s1/common.py` `bands_only()`, used by build_sft.py, evaluate.py (and so calibrate.py) and drift.py, sets
  `CORTADOS_SCARCITY_REFRESH=off` in-process and stubs `lane_health`. label.py is untouched, because its cli backend needs
  live usage. No `CORTADOS_HOME` change is needed: with refresh off, `scarcity.current()` only reads the cache.
- Provenance: nightly promotions (`nightly_state.promote`) and `promote_sweep.py` both write `"lora": {rank, scale,
  num_layers}` into `current.json`. Serving ignores the field; it reads rank and scale from the adapter dir.
- Evaluation and promotion across ranks already worked:
  - `student.Scorer` loads through mlx_lm with the adapter's own config.
  - `calibrate.blend_plan` sums the ranks (16 + 16 = 32 for the next blend).
  - Temperatures are carried: the incumbent's frozen pointer temperatures, and the candidate's fitted ones in its report
    and in the promoted pointer.

## Survey of the Studio (2026-11-13)

| item | value |
|---|---|
| checkout `~/cortados-router` | branch main at `ce3c023` |
| `~/cortados/train/data` | `no-promote` and `no-train-tonight` present; no `pending-candidate`; no `nightly.stop` |
| `runs/2026-11-10` | evaluated (`report.json`); rank-8 tensors under a rank-8 config. Its 1,500 steps warm-started from the old rank-8 live, so it is consistent |
| `runs/2026-11-11` .. `-13` | `nightly.log` only: nothing half-trained that the new check could trip on |
| `CORTADOS_S1_LORA_*`, `CORTADOS_S1_INIT` | set in neither `data/train-config.env` nor the s1-continuous plist |

## Part 0. Pre-checks (read-only)

```bash
R=~/cortados-router; TR=~/cortados/train; S1=$R/s1
P=$TR/runs/2026-11-08/promoted/2026-11-08-blend0.7/adapter
ls $TR/data/no-promote $TR/data/no-train-tonight            # both present: nothing trains or publishes yet
cat $TR/data/pending-candidate 2>/dev/null || echo "no pending candidate"
pgrep -fl 'cortados-router/s1/nightly\.sh' || echo "no cycle running"   # deploy between cycles (see Part A)
git -C $R status --porcelain > /tmp/status-before.txt; git -C $R rev-parse HEAD   # ce3c023 (+ the tables/ refresh files)
grep -c LORA $TR/data/train-config.env ~/Library/LaunchAgents/com.cortados.s1-continuous.plist   # 0 and 0
shasum -a 256 $S1/current.json > /tmp/current.sha
```

## Part A. Deploy (after the owner merges `s1-rank16-warmstart` into main)

Run it between cycles. bash reads a script as it runs, and git replaces the file, so a running cycle keeps the old copy.
That is not harmful, but the first new-code cycle should be a whole one. No launchd restart is needed:
`overnight.py --continuous` starts `bash s1/nightly.sh` fresh each cycle, and `serve.py`/`student.py` are unchanged.

```bash
git -C $R fetch origin
git -C $R merge --ff-only origin/main
git -C $R status --porcelain | diff - /tmp/status-before.txt && echo "local table changes intact"
ls $S1/lora_params.py
```

## Part B. Check the fix against the live adapter (writes only to a temp dir)

```bash
python3 -B $S1/lora_params.py rank $P/adapters.safetensors
# expect: 16 16
T=$(mktemp -d)
python3 -B $S1/lora_params.py plan --run $T --num-layers 16 --base mlx-community/Qwen3-4B-Instruct-2507-4bit \
  --start $P/adapters.safetensors --config $P/adapter_config.json; echo "exit $?"
# expect: lora: LoRA rank 16, scale 20 from adapters.safetensors (tensors) and its adapter_config.json → $T/lora-train.yaml ; exit 0
cat $T/lora-train.yaml   # {"lora_parameters": {"rank": 16, "dropout": 0.0, "scale": 20.0}}
rm -rf $T
```

If either output differs, stop and report. Do not lift anything.

## Part C. Lift training first; keep promotion held for one night

Lift one hold at a time, so the first rank-16 run is trained and evaluated but cannot publish before someone has read it.

```bash
mv $TR/data/no-train-tonight $TR/data/no-train-tonight.lifted-$(date +%F)
```

The next in-window cycle shows, in `$TR/runs/<night>/nightly.log`:

- `warm start from /Users/velox/cortados/train/runs/2026-11-08/promoted/2026-11-08-blend0.7/adapter/adapters.safetensors`
- `resuming from resume_0_adapters.safetensors`
- `lora: LoRA rank 16, scale 20 from resume_0_adapters.safetensors (tensors) and its adapter_config.json → runs/<night>/lora-train.yaml`
- `Loading configuration file runs/<night>/lora-train.yaml` (mlx_lm)
- and, if the candidate passes, `PROMOTION HELD`.

Next morning (read-only):

```bash
N=$TR/runs/<night>
grep -n "lora:\|LORA REFUSED\|TRAINING REFUSED\|PROMOTION HELD\|scarcity" $N/nightly.log
#   lora: rank 16 lines; no REFUSED. Expect NO "cortados scarcity:" lines between "labels: kept" and "labelled train-split"
#   (build_sft), nor during evaluation. Lines during label.py teacher calls can still appear: label.py needs live usage.
cat $N/lora-params.json                                   # {"rank": 16, "dropout": 0.0, "scale": 20.0}
grep -A3 lora_parameters $N/adapter/adapter_config.json   # rank 16, scale 20.0
python3 -B $S1/lora_params.py rank $N/adapter/adapters.safetensors   # 16 16
cat $TR/data/pending-candidate 2>/dev/null; python3 -c "import json;r=json.load(open('$N/report.json'));print(r.get('promote'), r['candidate'])"
```

If a night prints `TRAINING REFUSED`, its `LORA REFUSED:` line names the cause. Nothing trained and the scorer came back.
Fix the cause, or `touch $TR/data/no-train-tonight` again.

## Part D. Lift promotion

Only after Part C looked right. A held candidate is re-evaluated against the current incumbent and holdouts, and it is
published only if that fresh report promotes.

```bash
mv $TR/data/no-promote $TR/data/no-promote.lifted-$(date +%F)
```

After a promotion, check that `current.json` has `"lora": {"rank": 16, "scale": 20.0, "num_layers": 16}` and that
`curl -s -m 10 http://<bind>:8431/health` answers (see `s1-promote-2026-11-11.md` Part G for the full health checks).

## Limits and their reasons

- **Rank cap 64 (`CORTADOS_S1_LORA_MAX_RANK`).** Every blend promotion doubles the rank: 16 now, then 32, then 64. Trainer
  memory and step time grow with it, beside the resident 8B. Past 64, shrinking the adapter (SVD) or training fresh at rank 8
  is an owner decision, not something a night drifts into. A night above the cap is refused.
- **`num_layers` 16.** Every promoted adapter was trained on 16 layers. LoRA tensors for other layers would be silently
  ignored, so another count is refused.
- **Configured rank/scale apply to from-scratch runs only.** A warm start keeps its weights' own rank and scale. Overriding
  them would discard or zero-pad trained factors, so a disagreeing value refuses the night.

## Rollback

- Hold again (immediate, no code change): `touch $TR/data/no-train-tonight $TR/data/no-promote`.
- Code: `git -C $R reset --keep ce3c023`. Not needed for safety: the old nightly.sh is the one with the bug.
- Serving: this change never touches `current.json`. A promotion made after Part D rolls back with
  `cp -p $S1/current.json.prev $S1/current.json`.
