#!/bin/bash
# CortadOS Router S1 nightly. Runs from launchd at 01:30. Labels every night (cheap, only new rows, scarcity-gated); TRAINS only when it is worth it:
#   - no promoted model yet, or
#   - it is Sunday night (weekly cadence), or
#   - at least 150 newly labelled requests since the last training run (a busy week should not wait).
# Forest 2026-09-21: cadence is Claude's call. Reason: a day adds <10% data; nightly retrains on that are noise and churn promotions.
#
# This file is the ONE canonical nightly (2026-11-02: replaces the Studio's forked ~/cortados/train/nightly.sh). Run it from the
# checkout the scorer serves from, so a promotion lands where s1/serve.py reads. Every machine-specific value is an env var
# (set them in the launchd plist or data/train-config.env); none of them names a host:
#   CORTADOS_S1_SERVE_POINTER   file the scorer reads; promotion writes it atomically + keeps <file>.prev   (default: s1/current.json)
#   CORTADOS_S1_SCORER_PLIST    scorer LaunchAgent unloaded during training      (default ~/Library/LaunchAgents/com.cortados.s1-scorer.plist)
#   CORTADOS_S1_PY              training python with mlx_lm                      (default <repo>/.venv-mlx/bin/python)
#   CORTADOS_S1_LABEL_PY        python that runs s1/label.py                      (default python3; label.py runs from this repo)
#   CORTADOS_S1_TEACHER_ARGS    extra args for label.py, e.g. "--workers 3 --train-teacher <model> --holdout-teacher <model>"
#                               (default "--workers 3"; label.py exits 0 done, 2 config error, 3 stopped early)
#   CORTADOS_PROVIDER_URL, CORTADOS_PROVIDER_TOKEN_FILE   provider-backed teacher settings, passed to label.py through the env
#                               (URL has no default and is never committed; token file defaults to ~/.config/cortados/provider-token)
#   CORTADOS_ROUTER_DB_EXTRA    extra router dbs for dataset.py (os.pathsep list) (default ~/.cortados/router-air.db when it exists)
#   CORTADOS_MCP_LEARNING_V1, CORTADOS_TRAINING_TEACHER_V1, CORTADOS_MCP_LEARNING_SITE_PACKAGES   feature flags read by
#                               label.py / build_sft.py; =1 MCP learning also runs build_sft.py under CORTADOS_S1_PY
#   CORTADOS_S1_DB_MAX_AGE_MIN  synced router db older than this is logged as stale (default 60)
#   CORTADOS_S1_STATUS          status file for the dashboard (default data/status.json)
#   CORTADOS_S1_STALL_NIGHTS    nights without label growth before the ALERT (default 2)
#   CORTADOS_S1_LOG_KEEP        nightly logs kept (default 14)
#   CORTADOS_ROUTER_REPO        repo root for unitcost.py / bin/refresh-tiers.sh  (default ..)
# Exit codes: 0 ok / skipped, 3 labeling failed, 1 other failure, 2 base model missing, 130 stopped.
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
# set -a: every assignment in the config is exported, so label.py / evaluate.py see CORTADOS_PROVIDER_URL and friends
# (sourced without it they stayed shell-local and label.py exited 2, config error).
if [ -f data/train-config.env ]; then set -a; . data/train-config.env; set +a; fi
PY="${CORTADOS_S1_PY:-../.venv-mlx/bin/python}"
LABEL_PY="${CORTADOS_S1_LABEL_PY:-python3}"
TEACHER_ARGS="${CORTADOS_S1_TEACHER_ARGS:---workers 3}"   # label.py's own flags (--train-teacher, --holdout-teacher, --workers, --k, ...)
REPO_ROOT="${CORTADOS_ROUTER_REPO:-..}"
SERVE_POINTER="${CORTADOS_S1_SERVE_POINTER:-$PWD/current.json}"   # serve.py reads current.json beside itself
export CORTADOS_S1_SERVE_POINTER="$SERVE_POINTER"   # evaluate.py grades against the incumbent named here
SCORER_PLIST="${CORTADOS_S1_SCORER_PLIST:-$HOME/Library/LaunchAgents/com.cortados.s1-scorer.plist}"
STATUS="${CORTADOS_S1_STATUS:-data/status.json}"
STALL_NIGHTS="${CORTADOS_S1_STALL_NIGHTS:-2}"   # one quiet night is normal (weekend); two in a row means the labeler is stuck
LOG_KEEP="${CORTADOS_S1_LOG_KEEP:-14}"
DB_MAX_AGE_MIN="${CORTADOS_S1_DB_MAX_AGE_MIN:-60}"   # the sync runs every 15 min; an hour means several missed syncs
BASE="${CORTADOS_S1_BASE:-mlx-community/Qwen3-4B-Instruct-2507-4bit}"
export CORTADOS_S1_BASE="$BASE"
# Forest 2026-10-26: fold the Air's synced routed usage into training (another machine rsyncs its router.db here);
# dataset.py namespaces those rows by the db stem so they cannot collide.
if [ -z "${CORTADOS_ROUTER_DB_EXTRA+x}" ] && [ -f "$HOME/.cortados/router-air.db" ]; then
  export CORTADOS_ROUTER_DB_EXTRA="$HOME/.cortados/router-air.db"
fi
newest_unfinished_run() {   # most recent run that began a training segment but never wrote a report
  local d
  for d in $(ls -td runs/20*/ 2>/dev/null); do
    d=${d%/}
    [ -f "$d/segment-target" ] && [ ! -f "$d/report.json" ] && compgen -G "$d/adapter/*_adapters.safetensors" >/dev/null && { echo "$d"; return; }
  done
}
prune_unresumable_runs() {   # only tonight's run is ever resumed, so other unfinished runs' checkpoints (~0.65 GB each) are dead weight
  local d promoted
  promoted=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['adapter'])" "${SERVE_POINTER:-current.json}" 2>/dev/null)
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
# names only: values can be an internal provider URL or a secret's path
[ -f data/train-config.env ] && echo "train-config sets: $(grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' data/train-config.env | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' | tr '\n' ' ')"
rotate_logs() {   # keep the newest $LOG_KEEP nightly logs; run folders (adapters, reports) are untouched
  local old
  case "$LOG_KEEP" in ''|*[!0-9]*) echo "log rotation: CORTADOS_S1_LOG_KEEP=$LOG_KEEP is not a whole number; keeping 14"; LOG_KEEP=14 ;; esac
  # 0 would delete tonight's own log (tail -n +1 lists every file): keep at least the current one
  [ "$LOG_KEEP" -lt 1 ] && { echo "log rotation: CORTADOS_S1_LOG_KEEP=$LOG_KEEP would delete tonight's log; keeping 1"; LOG_KEEP=1; }
  old=$(ls -t runs/*/nightly.log 2>/dev/null | tail -n +"$((LOG_KEEP + 1))")
  [ -n "$old" ] && echo "$old" | while IFS= read -r f; do rm -f -- "$f"; done && echo "log rotation: removed $(echo "$old" | wc -l | tr -d ' ') old nightly log(s)"
  return 0
}
rotate_logs
gpu_alone() {   # Forest 2026-09-27: per-turn extraction must never stop, so ollama (the 8B) and the memory gateway STAY UP during training.
  launchctl unload "$SCORER_PLIST" 2>/dev/null; sleep "${CORTADOS_S1_GPU_SETTLE:-3}"; echo "scorer stopped for training; ollama + memory gateway stay up"
}
gpu_share() {
  launchctl load "$SCORER_PLIST" 2>/dev/null; echo "scorer back"
}
state() { python3 -B ./nightly_state.py "$@"; }
evaluate_run() {   # grade <run>/adapter against the CURRENT incumbent and holdout; writes <run>/report.json
  HF_HUB_OFFLINE=1 CORTADOS_S1_FAIL_UP="${CORTADOS_S1_FAIL_UP:-0.75}" CORTADOS_S1_OUTWARD_AT="${CORTADOS_S1_OUTWARD_AT:-0.25}" \
    $PY evaluate.py --base "$BASE" --adapter "$1/adapter" --out "$1" --baseline
}
# The no-new-data marker is committed only once a night's outcome is final (published, or intentionally not promoted): saved
# before a failed promotion, the next unchanged-data night would skip and the approved candidate would never be served.
mark_trained() { echo "$sft_hash" > data/last-train-labels.tmp && mv data/last-train-labels.tmp data/last-train-labels; }
report_blocked() { python3 -c "import json,sys;sys.exit(0 if json.load(open(sys.argv[1])).get('promote_blocked') else 1)" "$1/report.json"; }
eval_fingerprint() {   # effective holdout labels + inference settings (evaluate.py --holdout-key) + the serving incumbent
  local hk inc
  hk=$(CORTADOS_S1_FAIL_UP="${CORTADOS_S1_FAIL_UP:-0.75}" CORTADOS_S1_OUTWARD_AT="${CORTADOS_S1_OUTWARD_AT:-0.25}" $PY evaluate.py --holdout-key) || return 1
  inc=$(state fingerprint "$SERVE_POINTER" 2>/dev/null) || inc=none
  echo "${hk}-${inc}"
}
report_promotes() { python3 -c "import json,sys;sys.exit(0 if json.load(open(sys.argv[1]))['promote'] else 1)" "$1/report.json"; }
cleanup() { pkill -TERM -P $$ 2>/dev/null; gpu_share; rm -f "$PIDFILE"; }
trap cleanup EXIT
trap 'echo "stop requested"; touch "$STOPFLAG"; exit 130' TERM INT
# unitcost / refresh-tiers live in the router checkout; a missing one is logged, never fatal (labelling and training do not need them)
if [ -f "$REPO_ROOT/cortados_router/unitcost.py" ]; then python3 "$REPO_ROOT/cortados_router/unitcost.py" > /dev/null 2>&1 || echo "unitcost.py failed (rc $?)"   # daily re-normalisation: tokens per percent per pool
else echo "unitcost.py not found under $REPO_ROOT: skipped"; fi
# every night: what Cursor/Antigravity/Codex list today + fresh benchmark scores → every model re-scored per use case.
if [ -x "$REPO_ROOT/bin/refresh-tiers.sh" ]; then "$REPO_ROOT/bin/refresh-tiers.sh" || echo "refresh-tiers.sh failed (rc $?)"
else echo "refresh-tiers.sh not found under $REPO_ROOT: skipped"; fi
# The Air's router.db is rsynced here every 15 min by another machine; a stale copy means its recent usage is missing tonight.
# Stale data is still valid data, so the night goes on, but the gap is logged and put in the status file for the dashboard.
IFS=: read -ra extra_dbs <<< "${CORTADOS_ROUTER_DB_EXTRA:-}"   # os.pathsep list, as dataset.py reads it; paths may contain spaces
for db in "${extra_dbs[@]+"${extra_dbs[@]}"}"; do
  [ -n "$db" ] || continue
  if [ ! -f "$db" ]; then echo "WARNING: synced router db $db is missing"; state event "$STATUS" stale_router_db "$db missing"; continue; fi
  mtime=$(stat -f %m "$db" 2>/dev/null || stat -c %Y "$db" 2>/dev/null)
  if [ -z "$mtime" ]; then
    echo "WARNING: synced router db $db: age unknown (stat failed)"; state event "$STATUS" stale_router_db "$db age unknown"; continue
  fi
  age_min=$(( ($(date +%s) - mtime) / 60 ))
  if [ "$age_min" -gt "$DB_MAX_AGE_MIN" ]; then
    echo "WARNING: synced router db $db is ${age_min} min old (limit $DB_MAX_AGE_MIN): its newest usage is missing tonight"
    state event "$STATUS" stale_router_db "$db ${age_min} min old"
  else echo "synced router db $db fresh (${age_min} min)"; fi
done
python3 dataset.py || { rc=$?; echo "DATASET FAILED (rc $rc): training skipped"; state event "$STATUS" dataset_failure "rc $rc"; exit 1; }
# label.py runs from THIS checkout (not a release venv); the provider teacher reads CORTADOS_PROVIDER_URL / CORTADOS_PROVIDER_TOKEN_FILE
# from the env. Only whether they are set is logged: the URL may name an internal host and the token file is a secret's path.
purl=unset; [ -n "${CORTADOS_PROVIDER_URL:-}" ] && purl=set
ptok="default (~/.config/cortados/provider-token)"; [ -n "${CORTADOS_PROVIDER_TOKEN_FILE:-}" ] && ptok=set
echo "teacher: $LABEL_PY label.py $TEACHER_ARGS (provider url $purl, token file $ptok)"
# shellcheck disable=SC2086   # TEACHER_ARGS is a word list on purpose
"$LABEL_PY" -B label.py $TEACHER_ARGS
rc=$?
# Count labels and update the stall tracker BEFORE branching on rc: a labeler that fails every night is exactly the stall to alert on.
if ! labelled=$(state labels data/labels.jsonl); then
  echo "LABEL COUNT FAILED (data/labels.jsonl unreadable): training skipped"; state event "$STATUS" label_count_failure "labels.jsonl unreadable"; exit 1
fi
alert=$(state growth "$STATUS" "$labelled" "$STALL_NIGHTS")
[ -n "$alert" ] && echo "$alert"
# label.py exit codes: 0 completed, 2 config error, 3 stopped early (quota/auth/retries; the labels it wrote are kept).
if [ "$rc" -eq 3 ]; then
  # partial labels are real labels: go on, and let the no-new-labels check below decide whether there is anything to train on
  echo "LABELING STOPPED EARLY (label.py rc 3): continuing with the labels written so far"
  state event "$STATUS" labeling_stopped_early "label.py rc 3"
elif [ "$rc" -ne 0 ]; then
  # 2026-11-02: a failed labeler used to be ignored and the night retrained on yesterday's data.
  echo "LABELING FAILED (label.py rc $rc): training skipped"
  state event "$STATUS" labeling_failure "label.py rc $rc"
  exit 3
fi
if [ "${CORTADOS_MCP_LEARNING_V1:-}" = "1" ]; then
  "$PY" build_sft.py || exit 1
else
  python3 build_sft.py || exit 1
fi

# The no-new-data marker fingerprints what training actually reads: build_sft also merges outcome-loop examples and
# labels-override-*.jsonl, and it is deterministic (fixed seeds), so the same SFT files mean the same training data.
if ! sft_hash=$(state fingerprint data/sft/train.jsonl data/sft/valid.jsonl); then
  echo "SFT FINGERPRINT FAILED (data/sft missing after build_sft.py): training skipped"; state event "$STATUS" sft_fingerprint_failure "data/sft"; exit 1
fi
last=$(cat data/last-train-count 2>/dev/null || echo 0)
new=$((labelled - last))
pending=0
if [ ! -f "$RUN/report.json" ] && { [ -f "$RUN/segment-target" ] || compgen -G "$RUN/adapter/0*_adapters.safetensors" >/dev/null; }; then
  pending=1
fi
# A pending candidate is a trained run whose outcome is not final yet, kept in data/pending-candidate as
#   "<sft fingerprint> <eval fingerprint> <reason> <run>"
#   reason promotion_failed: approved but the pointer write failed → re-evaluate and publish on the next night;
#   reason holdout_blocked:  evaluate.py refused for a too-small Claude holdout. Holdout labels never enter the SFT data, so the
#                            SFT marker cannot notice the backfill; the run waits and is re-evaluated when the eval fingerprint
#                            (effective holdout labels + settings + serving incumbent) changes.
# Re-evaluation always grades against today's holdout and incumbent (an earlier approval may be stale). New SFT data supersedes it.
force="${1:-}"; [ -f data/force-train ] && force="--train" && rm -f data/force-train
if [ -f data/pending-candidate ] && [ "$force" = "--train" ]; then
  # an explicit --train / data/force-train is a request for a fresh candidate: it never waits behind a pending one
  read -r _ _ p_reason p_run < data/pending-candidate
  echo "forced training: pending candidate $p_run ($p_reason) superseded"; rm -f data/pending-candidate
fi
if [ -f data/pending-candidate ]; then
  read -r p_sft p_eval p_reason p_run < data/pending-candidate
  if [ "$p_sft" = "$sft_hash" ] && [ -d "$p_run/adapter" ]; then
    if ! now_eval=$(eval_fingerprint); then
      echo "EVAL FINGERPRINT FAILED for pending $p_run; retried next night"; state event "$STATUS" pending_failure "$p_run eval fingerprint"; exit 1
    fi
    if [ "$p_reason" = holdout_blocked ] && [ "$now_eval" = "$p_eval" ]; then
      echo "skipped: no new labels; pending $p_run waits for the holdout backfill (holdout and incumbent unchanged)"
      state event "$STATUS" skip "pending $p_run waits for holdout ($p_reason)"
      echo "=== $(date) nightly end"; exit 0
    fi
    echo "pending candidate $p_run ($p_reason): re-evaluating against the current holdout and incumbent"
    if ! evaluate_run "$p_run"; then
      echo "re-evaluation of $p_run failed; retried next night"; state event "$STATUS" pending_failure "$p_run (re-evaluation failed)"; exit 1
    fi
    if ! report_promotes "$p_run"; then
      if report_blocked "$p_run"; then
        echo "$sft_hash $now_eval holdout_blocked $p_run" > data/pending-candidate
        echo "pending $p_run still blocked: $(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['promote_blocked'])" "$p_run/report.json")"
        state event "$STATUS" skip "pending $p_run still holdout-blocked"
      else
        rm -f data/pending-candidate; mark_trained
        state event "$STATUS" skip "pending $p_run no longer promotes"
        echo "pending $p_run no longer promotes (see $p_run/report.json); incumbent stays; no new data, nothing to train"
      fi
      echo "=== $(date) nightly end"; exit 0
    fi
    if state promote "$p_run/report.json" "$PWD/$p_run/adapter" "$SERVE_POINTER"; then
      rm -f data/pending-candidate; state event "$STATUS" promotion "$p_run (pending, $p_reason)"; mark_trained
      echo "PROMOTED (retry) $p_run → $SERVE_POINTER; no new data, nothing to train"; echo "=== $(date) nightly end"; exit 0
    fi
    echo "$sft_hash $now_eval promotion_failed $p_run" > data/pending-candidate
    echo "PROMOTION FAILED again for $p_run → $SERVE_POINTER"; state event "$STATUS" promotion_failure "$p_run (retry)"; exit 1
  fi
  echo "pending candidate $p_run superseded by new training data"; rm -f data/pending-candidate
fi
# Forest 2026-10-13: retrain every night (supersedes the 2026-09-21 Sunday / +150 labels / drift cadence) — but only on new data:
# 2026-11-02, the same labels + a warm start from the same adapter reproduce the incumbent, so a night with no new labels skips.
# An interrupted run still resumes (its steps are unfinished work, not a repeat).
if [ "$force" != "--train" ] && [ "$pending" -eq 0 ] && [ "$(cat data/last-train-labels 2>/dev/null)" = "$sft_hash" ]; then
  echo "skipped: no new labels ($labelled labels; training data identical to the last training run)"
  state event "$STATUS" skip "no new labels ($labelled)"
  echo "=== $(date) nightly end"; exit 0
fi
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
if [ -z "${CORTADOS_S1_INIT:-}" ] && [ "$pending" -eq 0 ] && [ -f "${SERVE_POINTER:-current.json}" ]; then
  CORTADOS_S1_INIT=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['adapter'] + '/adapters.safetensors')" "${SERVE_POINTER:-current.json}")
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
  HF_HUB_OFFLINE=1 $PY -m mlx_lm lora --model "$BASE" --train --data data/sft --fine-tune-type lora --num-layers 16 --batch-size "$BATCH" --iters "$remaining" \
    --learning-rate "$LR" --mask-prompt --max-seq-length 1536 --grad-checkpoint --steps-per-eval 100 --save-every 100 --adapter-path "$RUN/adapter" $resume && break
  [ -f "$STOPFLAG" ] && { echo "stopped by request; not relaunching"; exit 130; }
  attempt=$((attempt + 1)); echo "trainer died (attempt $attempt)"; [ "$attempt" -ge 4 ] && exit 1
  sleep 60
  [ -f "$STOPFLAG" ] && { echo "stopped by request; not relaunching"; exit 130; }
done
gpu_share; sleep "${CORTADOS_S1_EVAL_SETTLE:-20}"                                            # the grader's incumbent baseline needs ollama back; the 14B rows are cached anyway
evaluate_run "$RUN" || exit 1
echo "$labelled" > data/last-train-count
state event "$STATUS" train "$RUN ($labelled labels)"
if report_promotes "$RUN"; then
  # 2026-11-02: write the file the scorer reads (it used to be a copy the scorer never saw). serve.py reloads when its mtime changes;
  # the previous pointer is kept as <pointer>.prev — rollback: mv <pointer>.prev <pointer>.
  if state promote "$RUN/report.json" "$PWD/$RUN/adapter" "$SERVE_POINTER"; then
    state event "$STATUS" promotion "$RUN"
    mark_trained
    echo "PROMOTED → $SERVE_POINTER (previous kept as $SERVE_POINTER.prev)"
  else
    echo "$sft_hash $(eval_fingerprint || echo unknown) promotion_failed $RUN" > data/pending-candidate   # retried next night
    state event "$STATUS" promotion_failure "$RUN → $SERVE_POINTER"
    echo "PROMOTION FAILED writing $SERVE_POINTER; incumbent stays; publication retried next night"; exit 1
  fi
elif report_blocked "$RUN"; then
  # not final: the holdout is too small to judge. Keep the run waiting for the backfill instead of marking the data trained.
  echo "$sft_hash $(eval_fingerprint || echo unknown) holdout_blocked $RUN" > data/pending-candidate
  state event "$STATUS" skip "$RUN holdout-blocked; waits for the holdout backfill"
  echo "not promoted: holdout too small (see $RUN/report.json); $RUN waits and is re-evaluated when the holdout grows"
else
  mark_trained
  echo "not promoted (see $RUN/report.json); incumbent stays"
fi
echo "=== $(date) nightly end"
