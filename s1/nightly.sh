#!/bin/bash
# CortadOS Router S1 nightly. Runs from launchd at 01:30. Labels every night (cheap, only new rows, scarcity-gated); TRAINS only when it is worth it:
#   - no promoted model yet, or
#   - it is Sunday night (weekly cadence), or
#   - at least 150 newly labelled requests since the last training run (a busy week should not wait).
# Forest 2026-09-21: cadence is Claude's call. Reason: a day adds <10% data; nightly retrains on that are noise and churn promotions.
set -u
cd "$(dirname "$0")" || exit 1
PIDFILE=data/nightly.pid; STOPFLAG=data/nightly.stop
if [ "${1:-}" = "--stop" ]; then                       # the one supported way to stop a run: the loop AND its trainer, then the services come back
  touch "$STOPFLAG"
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    p=$(cat "$PIDFILE"); pkill -TERM -P "$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null
    for i in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
    kill -0 "$p" 2>/dev/null && { pkill -KILL -P "$p" 2>/dev/null; kill -KILL "$p" 2>/dev/null; }
    echo "stopped nightly $p and its children"
  else
    echo "no nightly running"; pkill -f "mlx_lm lora" 2>/dev/null && echo "killed an orphan trainer"
  fi
  rm -f "$PIDFILE"; exit 0
fi
if [ "${CORTADOS_NIGHTLY_SUPERVISED:-}" != "1" ]; then
  exec python3 overnight.py -- /bin/bash "$PWD/nightly.sh" "$@"
fi
[ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null && { echo "nightly already running (pid $(cat "$PIDFILE"))"; exit 0; }
echo $$ > "$PIDFILE"; rm -f "$STOPFLAG"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
PY=../.venv-mlx/bin/python
BASE="${CORTADOS_S1_BASE:-mlx-community/Qwen3-4B-Instruct-2507-4bit}"
[ -f data/train-config.env ] && . data/train-config.env
newest_unfinished_run() {   # most recent run that began a training segment but never wrote a report
  local d
  for d in $(ls -td runs/20*/ 2>/dev/null); do
    d=${d%/}
    [ -f "$d/segment-target" ] && [ ! -f "$d/report.json" ] && compgen -G "$d/adapter/*_adapters.safetensors" >/dev/null && { echo "$d"; return; }
  done
}
prune_unresumable_runs() {   # only tonight's run is ever resumed, so other unfinished runs' checkpoints (~0.65 GB each) are dead weight
  local d promoted
  promoted=$(python3 -c "import json;print(json.load(open('current.json'))['adapter'])" 2>/dev/null)
  for d in runs/20*/; do
    d=${d%/}
    [ "$d" = "$RUN" ] || [ -f "$d/report.json" ] || [ ! -d "$d/adapter" ] && continue
    case "$promoted" in *"/$d/"*) continue ;; esac
    rm -rf -- "$d/adapter" && echo "retention: removed checkpoints of unfinished $d (logs kept)"
  done
}
RUN="${CORTADOS_S1_RUN:-runs/$(date +%Y-%m-%d)}"
if [ -d "$RUN/adapter" ] && [ -f "$RUN/report.json" ]; then   # a finished run is never resumed or overwritten
  # 2026-10-13: a pin left on the finished 10-01 run made every night start a new folder from step 0 and stop at ~2,400 of 3,600
  # steps; resume the newest unfinished run instead, and start a new folder only when there is none.
  RUN=$(newest_unfinished_run); [ -n "$RUN" ] || RUN="runs/$(date +%Y-%m-%d)-$(date +%H%M)"
fi
mkdir -p "$RUN" data
exec >> "$RUN/nightly.log" 2>&1
echo "=== $(date) nightly start (base $BASE, run $RUN)"
prune_unresumable_runs
[ -f data/train-config.env ] && echo "train-config: $(grep -v '^#' data/train-config.env | tr '\n' ' ')"
gpu_alone() {   # Forest 2026-09-27: per-turn extraction must never stop, so ollama (the 8B) and the memory gateway STAY UP during training.
  launchctl unload ~/Library/LaunchAgents/com.cortados.s1-scorer.plist 2>/dev/null; sleep 3; echo "scorer stopped for training; ollama + memory gateway stay up"
}
gpu_share() {
  launchctl load ~/Library/LaunchAgents/com.cortados.s1-scorer.plist 2>/dev/null; echo "scorer back"
}
cleanup() { pkill -TERM -P $$ 2>/dev/null; gpu_share; rm -f "$PIDFILE"; }
trap cleanup EXIT
trap 'echo "stop requested"; touch "$STOPFLAG"; exit 130' TERM INT
python3 ../cortados_router/unitcost.py > /dev/null 2>&1   # daily re-normalisation: tokens per percent per pool
../bin/refresh-tiers.sh      # every night: what Cursor/Antigravity/Codex list today + fresh benchmark scores → every model re-scored per use case. New models appear in routing the next morning, nobody edits a list.
python3 dataset.py
python3 label.py --workers 3
python3 build_sft.py || exit 1

labelled=$(python3 -c "import json;print(len({json.loads(l)['id'] for l in open('data/labels.jsonl')}))")
last=$(cat data/last-train-count 2>/dev/null || echo 0)
new=$((labelled - last))
pending=0
if [ ! -f "$RUN/report.json" ] && { [ -f "$RUN/segment-target" ] || compgen -G "$RUN/adapter/0*_adapters.safetensors" >/dev/null; }; then
  pending=1
fi
force="${1:-}"; [ -f data/force-train ] && force="--train" && rm -f data/force-train
# Forest 2026-10-13: retrain every night (supersedes the 2026-09-21 Sunday / +150 labels / drift cadence).
echo "labelled $labelled (+$new since last training); training tonight (pending run: $pending)"
[ "$force" != "--train" ] && [ -f data/no-train-tonight ] && { echo "data/no-train-tonight present (another session holds the GPU): labelling only"; exit 0; }
# laya night (Forest 2026-09-29: an overnight trial of how it affects the router model): when data/laya-tonight is present the
# GPU goes to the ModernBERT candidate (s1/laya, not published; ~1.6 s/step on MPS) instead of the Qwen resume; it is graded on the same holdout against the
# promoted Qwen adapter. Evaluate only — serve.py cannot load a laya checkpoint yet, so nothing is promoted from this path. One-shot flag.
if [ -f data/laya-tonight ]; then
  rm -f data/laya-tonight
  LRUN="laya/runs/$(date +%Y-%m-%d)"; [ -f "$LRUN/train-meta.json" ] && LRUN="$LRUN-$(date +%H%M)"
  gpu_alone
  echo "=== $(date) laya night: training into $LRUN (cap ${LAYA_MAX_STEPS:-2000} steps, stop file at ${LAYA_STOP_AT:-0645})"
  ( while [ "$(date +%H%M)" -lt "${LAYA_STOP_AT:-0645}" ] || [ "$(date +%H%M)" -gt 1200 ]; do sleep 60; [ -f "$LRUN/train-meta.json" ] && exit 0; done; touch "$LRUN/stop"; echo "laya: deadline reached, stop file set" ) &
  deadline=$!
  # data/laya-init names a checkpoint dir (e.g. laya/runs/2026-09-30/checkpoint_latest): tonight's run warm-starts from it (2026-09-30: 1,110 of
  # 2,000 steps fitted before the 06:45 deadline at 15.8 s/step — the 8B and nomic share the GPU all night, so a full run takes two nights)
  init=""; [ -f data/laya-init ] && init="$(cat data/laya-init)" && rm -f data/laya-init && [ -d "$init" ] && echo "laya: warm start from $init"
  ( cd laya && ../.venv-laya/bin/python convert.py && LAYA_INIT="${init:+../$init}" LAYA_RUN="../$LRUN" LAYA_MAX_STEPS="${LAYA_MAX_STEPS:-2000}" ./train.sh ) ; lrc=$?
  kill "$deadline" 2>/dev/null
  gpu_share; sleep 20
  if [ "$lrc" -eq 0 ] && [ -f "$LRUN/model.safetensors" ]; then
    HF_HUB_OFFLINE=1 CORTADOS_S1_FAIL_UP="${CORTADOS_S1_FAIL_UP:-0.75}" CORTADOS_S1_OUTWARD_AT="${CORTADOS_S1_OUTWARD_AT:-0.25}" .venv-laya/bin/python evaluate.py --engine laya --checkpoint "$LRUN" --out "$LRUN" \
      && python3 -c "import json;r=json.load(open('$LRUN/report.json'));c=r['candidate'];print('laya holdout: band',c['band_accuracy'],'under',c['under_routed'],'over',c['over_routed'],'outward',c['outward_recall'],'| would promote:',r['promote'])"
  else
    echo "laya training failed (rc $lrc); see $LRUN/train.log"
  fi
  echo "laya night: Qwen resume skipped tonight"; echo "=== $(date) nightly end"; exit 0
fi
if ! HF_HUB_OFFLINE=1 $PY -c "from mlx_lm import load; load('$BASE')" >/dev/null 2>&1; then
  echo "base model $BASE is not on disk: training skipped (download needs Forest's OK)"; exit 2
fi
n=$(wc -l < data/sft/train.jsonl)
gpu_alone
BATCH="${CORTADOS_S1_BATCH:-2}"                                   # 2: the 8B stays resident beside training now, so keep the trainer's footprint small
LR="${CORTADOS_S1_LR:-1e-4}"; EPOCHS="${CORTADOS_S1_EPOCHS:-2}"
MAX_ITERS="${CORTADOS_S1_MAX_ITERS:-3600}"                          # a night is ~6 h; at ~6 s/step that is ~3,600 steps (2026-09-28: 7,499 steps ran into the day)
# Forest 2026-10-13: a tested candidate every night. A new run (nothing to resume) warm-starts from the promoted adapter and is
# sized to finish AND be evaluated before the 07:00 stop. 1,500 steps: ~3.2 h at the measured 0.13 step/s from ~02:00, leaving
# ~1 h for evaluation; too many and the run is cut off unevaluated (the 10-03..10-13 failure), too few and a night adds little.
# Warm LR 5e-5: half the from-scratch rate, so one night's continued training refines the adapter rather than overwriting it.
if [ -z "${CORTADOS_S1_INIT:-}" ] && [ "$pending" -eq 0 ] && [ -f current.json ]; then
  CORTADOS_S1_INIT=$(python3 -c "import json;print(json.load(open('current.json'))['adapter'] + '/adapters.safetensors')")
  MAX_ITERS="${CORTADOS_S1_NIGHT_ITERS:-1500}"; LR="${CORTADOS_S1_WARM_LR:-5e-5}"
fi
if [ -n "${CORTADOS_S1_INIT:-}" ] && [ -f "$CORTADOS_S1_INIT" ]; then mkdir -p "$RUN/adapter"; cp "$CORTADOS_S1_INIT" "$RUN/adapter/0000000_adapters.safetensors"; cp "$(dirname "$CORTADOS_S1_INIT")/adapter_config.json" "$RUN/adapter/" 2>/dev/null; echo "warm start from $CORTADOS_S1_INIT"; fi
iters=$(( n * EPOCHS / BATCH ))
echo "training on $n examples, $iters iterations at batch $BATCH"
attempt=0
while :; do
  resume=""
  last=$(ls -t "$RUN"/adapter/0*_adapters.safetensors 2>/dev/null | head -1)          # newest by time: counters restart at 1 after each resume
  # mlx_lm counts iterations from 1 again on resume: keep a cumulative count in the run dir and hand it only the remaining ones
  ckpt=$(basename "${last:-0000000_x}" | sed -E 's/^0*([0-9]+)_.*/\1/'); ckpt=${ckpt:-0}
  # cumulative steps = steps credited BEFORE this checkpoint's segment + the checkpoint's own count. The segment base is recorded when a
  # segment starts (iters-base), never re-added on the next resume (the 2026-09-26 double count).
  base_done=$(cat "$RUN/iters-base" 2>/dev/null || echo 0); done_iters=$(( base_done + ckpt ))
  if [ -z "$last" ]; then
    last=$(ls -t "$RUN"/adapter/resume_*_adapters.safetensors 2>/dev/null | head -1)
    [ -n "$last" ] && done_iters=$(basename "$last" | sed -E 's/^resume_([0-9]+)_adapters.safetensors$/\1/')
  fi
  if [ -f "$RUN/segment-target" ]; then
    target=$(cat "$RUN/segment-target")
  else
    target=$iters
    [ "$((target - done_iters))" -gt "$MAX_ITERS" ] && target=$((done_iters + MAX_ITERS))
    echo "$target" > "$RUN/segment-target"
  fi
  remaining=$((target - done_iters))
  [ "$remaining" -le 0 ] && { echo "segment target $target already checkpointed; proceeding to evaluation"; break; }
  if [ -n "$last" ]; then
    # Preserve a cumulative resume checkpoint before resetting the segment counter.
    # A stop before the first new checkpoint must not count the previous checkpoint twice.
    saved="$RUN/adapter/resume_${done_iters}_adapters.safetensors"
    [ "$last" = "$saved" ] || { cp "$last" "$saved.tmp" && mv "$saved.tmp" "$saved"; } || exit 1
    resume="--resume-adapter-file $saved"
    echo "resuming from $(basename "$saved")"
  fi
  for f in "$RUN"/adapter/0*_adapters.safetensors; do [ ! -f "$f" ] || mv "$f" "$f.old"; done
  echo "$done_iters" > "$RUN/iters-base.tmp"; mv "$RUN/iters-base.tmp" "$RUN/iters-base"
  echo "segment target $target; $done_iters checkpointed; $remaining steps remain"
  # On a SUCCESSFUL training segment (and only then), atomically bind this checkpoint to the outcomes it trained on, via
  # store.home()'s sft-provenance.json. evaluate reads $RUN/outcome-provenance.json for hold-first; publishing only on
  # success and preserving it across no-op resumes means a completed teacher-only checkpoint keeps its own provenance.
  # A missing source or failed publish ABORTS the run (exit 1) rather than leave a stale provenance that could exempt an
  # outcome-trained checkpoint from review (Codex review 2026-10-25). Kept on this line so the resume-planning test, which
  # extracts the loop up to here, does not run it.
  HF_HUB_OFFLINE=1 $PY -m mlx_lm lora --model "$BASE" --train --data data/sft --fine-tune-type lora --num-layers 16 --batch-size "$BATCH" --iters "$remaining" \
    --learning-rate "$LR" --mask-prompt --max-seq-length 1536 --grad-checkpoint --steps-per-eval 100 --save-every 100 --adapter-path "$RUN/adapter" $resume \
    && { python3 -c "import sys, build_sft; build_sft.publish_provenance(sys.argv[1])" "$RUN" || { echo "provenance publish failed; aborting before evaluation"; exit 1; }; break; }
  [ -f "$STOPFLAG" ] && { echo "stopped by request; not relaunching"; exit 130; }
  attempt=$((attempt + 1)); echo "trainer died (attempt $attempt)"; [ "$attempt" -ge 4 ] && exit 1
  sleep 60
  [ -f "$STOPFLAG" ] && { echo "stopped by request; not relaunching"; exit 130; }
done
gpu_share; sleep 20                                              # the grader's incumbent baseline needs ollama back; the 14B rows are cached anyway
HF_HUB_OFFLINE=1 CORTADOS_S1_FAIL_UP="${CORTADOS_S1_FAIL_UP:-0.75}" CORTADOS_S1_OUTWARD_AT="${CORTADOS_S1_OUTWARD_AT:-0.25}" $PY evaluate.py --base "$BASE" --adapter "$RUN/adapter" --out "$RUN" --baseline || exit 1
echo "$labelled" > data/last-train-count
if python3 -c "import json,sys;sys.exit(0 if json.load(open('$RUN/report.json'))['promote'] else 1)"; then
  python3 -c "import json;r=json.load(open('$RUN/report.json'));json.dump({'base':r['base'],'adapter':'$PWD/$RUN/adapter','temperatures':r['temperatures'],'promoted':r['when'],'report':'$PWD/$RUN/report.json','inference':{'fail_up_below':r.get('fail_up_below'),'outward_at':r.get('outward_at')},'holdout':{k:r['candidate'][k] for k in ('band_accuracy','under_routed','over_routed','outward_recall')}},open('current.json','w'),indent=1)"
  echo "PROMOTED → current.json"
else
  echo "not promoted (see $RUN/report.json); incumbent stays"
fi
echo "=== $(date) nightly end"
