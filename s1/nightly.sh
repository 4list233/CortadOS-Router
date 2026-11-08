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
#   CORTADOS_S1_SERVE_POINTER   file the scorer reads; promotion writes it atomically + keeps <file>.prev   (default: s1/current.json).
#                               It must name <scorer checkout>/s1/current.json: serve.py reads only the current.json beside itself.
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
#   CORTADOS_S1_EVAL_MINUTES    minutes an evaluation needs (default 110). Evaluation is not started with less time than this
#                               before the deadline: past evaluations took 78-100 min, and overnight.py TERMs at 07:00, so a late
#                               start (2026-11-03: training ended 06:19) is always killed and wastes the night. 0 disables the check.
#   CORTADOS_S1_DEADLINE        HHMM local (overnight.py sets TZ=America/Toronto) when overnight.py stops the run (default 0700).
#                               CORTADOS_S1_EVAL_MINUTES within 30 min of the 01:30-to-deadline window can never fit and falls back to 110.
#   CORTADOS_S1_NOW             HHMM that replaces the clock for deadline checks, including Laya (tests); not HHMM → logged, the clock is used
#   nightly.sh --minutes-left [HHMM]   prints the minutes from HHMM (or now) to the deadline plus any settings notes and exits;
#                               no pid file, stop flag or overnight.py (diagnostic / tests). An invalid HHMM exits 2.
# Continuous mode (Forest 2026-11-03; window mode stays the default until the launchd job is switched): a launchd KeepAlive job runs
#   python3 <repo>/s1/overnight.py --continuous -- /bin/bash <repo>/s1/nightly.sh
# which loops cycles with no window/deadline, keeps the scorer up, and pauses this tree on scorer latency / low memory.
#   CORTADOS_S1_MIN_LEFT_PCT    label.py --min-left-pct per-account quota floor (default 15, Forest 2026-11-03)
#   CORTADOS_S1_TRAIN_ONLY_IN_WINDOW  continuous mode: gated training/eval only inside CORTADOS_S1_NIGHT_WINDOW (default 1;
#                               0 = any time; Forest 2026-11-08). overnight.py decides per cycle and passes CORTADOS_S1_TRAIN_ALLOWED
#                               (0 = label/dataset only, every gated step skipped, rc 0) and stops the cycle at the window end.
# Exit codes: 0 ok / skipped, 3 labeling failed, 1 other failure, 2 base model missing, 130 stopped.
set -u
cd "$(dirname "$0")" || exit 1
# `[ a -lt b ]` on a non-number or an out-of-range number prints "integer expression expected" and is simply false, so a
# bad setting would silently take the permissive branch. Every configurable number a comparison reads is validated here:
# 1-9 digits (well inside bash's 64-bit arithmetic), else the documented default, with a logged reason.
# A validated value is then normalised to decimal: bash arithmetic reads 08 / 010 as octal (08 is an error, 010 is 8).
whole() { [[ "$1" =~ ^[0-9]{1,9}$ ]]; }
dec() { echo "$((10#$1))"; }   # only ever called on a value whole() accepted
setting() {   # setting <var name> <default> [<min>]: replace an invalid or too-small value with the default (logged once the log is open)
  local v="${!1}" min="${3:-0}"
  if ! whole "$v"; then
    SETTING_NOTES="${SETTING_NOTES:-}$1=$v is not a whole number up to 9 digits; using $2. "; printf -v "$1" '%s' "$2"; return
  fi
  v=$(dec "$v")
  if [ "$v" -lt "$min" ]; then
    SETTING_NOTES="${SETTING_NOTES:-}$1=$v is below $min; using $2. "; v="$2"
  fi
  printf -v "$1" '%s' "$v"
}
deadline_settings() {   # the deadline settings (pure: no files touched); run after data/train-config.env is sourced
EVAL_MINUTES="${CORTADOS_S1_EVAL_MINUTES:-110}"; setting EVAL_MINUTES 110
DEADLINE="${CORTADOS_S1_DEADLINE:-0700}"
hhmm "$DEADLINE" || { SETTING_NOTES="${SETTING_NOTES:-}CORTADOS_S1_DEADLINE=$DEADLINE is not HHMM; using 0700. "; DEADLINE=0700; }
# an invalid clock override is dropped, not used: bash arithmetic on "abc" / "7" errors or prints nothing, and an empty
# minutes_left would read as "not enough time" every night (deferring the candidate forever)
if [ -n "${CORTADOS_S1_NOW:-}" ] && ! hhmm "$CORTADOS_S1_NOW"; then
  SETTING_NOTES="${SETTING_NOTES:-}CORTADOS_S1_NOW=$CORTADOS_S1_NOW is not HHMM; using the clock. "; unset CORTADOS_S1_NOW
elif [ -n "${CORTADOS_S1_NOW:-}" ]; then
  # a valid override silently replacing the clock is a test hook; a leftover value in train-config.env must show in every log
  SETTING_NOTES="${SETTING_NOTES:-}CORTADOS_S1_NOW=$CORTADOS_S1_NOW replaces the clock for the deadline check (test hook). "
fi
# ceiling: evaluation starts after labeling (~75 min into the window that opens at 01:30), so a value within 30 min of the whole
# 01:30-to-deadline window (330 min to 07:00: >= 300) can never fit: every candidate would wait forever. That is a config error,
# not a policy: fall back to the default. Derived from the real window so a later CORTADOS_S1_DEADLINE allows a longer evaluation.
local window; window=$(minutes_left 0130)   # minutes in the 01:30-to-deadline window
if [ "$EVAL_MINUTES" -gt 0 ] && [ "$EVAL_MINUTES" -ge $((window - 30)) ]; then
  SETTING_NOTES="${SETTING_NOTES:-}EVAL_MINUTES=$EVAL_MINUTES can never fit the 01:30-$DEADLINE window (>= $((window - 30))); using 110. "; EVAL_MINUTES=110
fi
# the 110 fallback is re-checked: a window too short even for it (e.g. DEADLINE=0300: 90 min) would defer every candidate
# forever, so the deadline check is disabled (0) with a logged reason; overnight.py's TERM at the deadline still applies
if [ "$EVAL_MINUTES" -gt 0 ] && [ "$EVAL_MINUTES" -ge $((window - 30)) ]; then
  SETTING_NOTES="${SETTING_NOTES:-}the 01:30-$DEADLINE window is too short even for EVAL_MINUTES=$EVAL_MINUTES; deadline check disabled (0). "; EVAL_MINUTES=0
fi
}
hhmm() { [[ "$1" =~ ^([01][0-9]|2[0-3])[0-5][0-9]$ ]]; }   # 0000-2359
# Share the validated test clock: a watcher using date directly would still depend on the time of day.
clock_hhmm() { printf '%s\n' "${CORTADOS_S1_NOW:-$(date +%H%M)}"; }
minutes_left() {   # minutes_left [HHMM]: minutes from now (or HHMM) to the next $DEADLINE (wraps: a 01:30 start has 330 min to 07:00)
  # CORTADOS_S1_NOW (HHMM, validated in deadline_settings) overrides the clock so tests can pin the time of night
  local now dl; now=${1:-$(clock_hhmm)}; now=$((10#${now:0:2} * 60 + 10#${now:2:2})); dl=$((10#${DEADLINE:0:2} * 60 + 10#${DEADLINE:2:2}))
  echo $(( (dl - now + 1440) % 1440 ))
}
# diagnostic / test hook, handled BEFORE any side effect (pid file, stop flag, overnight.py re-exec, which unloads the scorer):
# print the minutes to the deadline for HHMM (or now) and the settings notes, then exit. An invalid HHMM is refused.
if [ "${1:-}" = "--minutes-left" ]; then
  if [ -f data/train-config.env ]; then set -a; . data/train-config.env; set +a; fi
  deadline_settings
  if [ -n "${2:-}" ] && ! hhmm "$2"; then echo "--minutes-left $2 is not HHMM (0000-2359)"; exit 2; fi
  minutes_left "${2:-}"; [ -n "${SETTING_NOTES:-}" ] && echo "settings: $SETTING_NOTES"; exit 0
fi
PIDFILE=data/nightly.pid; STOPFLAG=data/nightly.stop
if [ "${1:-}" = "--stop" ]; then                       # the one supported way to stop a run: the loop AND its trainer, then the services come back
  touch "$STOPFLAG"
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    # SIGCONT first: overnight.py --continuous may have SIGSTOPped the tree (latency guard); a stopped process does not act on
    # TERM, so without CONT it would sit out the grace period and then be SIGKILLed with no cleanup.
    # The group CONT reaches stopped grandchildren too (the trainer under a python wrapper), which -P (direct children) misses.
    p=$(cat "$PIDFILE"); kill -CONT -- -"$p" 2>/dev/null; pkill -CONT -P "$p" 2>/dev/null; kill -CONT "$p" 2>/dev/null
    pkill -TERM -P "$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null
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
# Window mode: a new night clears yesterday's stop. Continuous mode: the stop flag outlives the cycle (overnight.py checks it
# between cycles), so a cycle never clears it and refuses to start while it is there (exit 130 = stop requested).
if [ "${CORTADOS_S1_CONTINUOUS:-0}" = 1 ]; then
  [ -f "$STOPFLAG" ] && { echo "stop flag $STOPFLAG present: not starting a cycle (remove it to resume)"; exit 130; }
else rm -f "$STOPFLAG"; fi
echo $$ > "$PIDFILE"
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
NIGHT="${CORTADOS_S1_NIGHT_DATE:-$(date +%F)}"   # the stall tracker counts nights by this date (override for tests / backfilled runs)
# one quiet night is normal (weekend); two in a row means the labeler is stuck
STALL_NIGHTS="${CORTADOS_S1_STALL_NIGHTS:-2}"
EARLY_STOP_NIGHTS="${CORTADOS_S1_EARLY_STOP_NIGHTS:-2}"   # same label.py stop reason this many nights running → ALERT
EVAL_FAIL_NIGHTS="${CORTADOS_S1_EVAL_FAIL_NIGHTS:-2}"     # evaluation unavailable/crashed this many nights running → ALERT
HOLDOUT_BLOCKED_NIGHTS="${CORTADOS_S1_HOLDOUT_BLOCKED_NIGHTS:-2}"   # a candidate blocked by the holdout floor this many nights → ALERT
LOG_KEEP="${CORTADOS_S1_LOG_KEEP:-14}"
DB_MAX_AGE_MIN="${CORTADOS_S1_DB_MAX_AGE_MIN:-60}"   # the sync runs every 15 min; an hour means several missed syncs
setting DB_MAX_AGE_MIN 60
setting STALL_NIGHTS 2 1; setting EARLY_STOP_NIGHTS 2 1; setting EVAL_FAIL_NIGHTS 2 1; setting HOLDOUT_BLOCKED_NIGHTS 2 1   # 0 would alert on every night
GPU_SETTLE="${CORTADOS_S1_GPU_SETTLE:-3}"; EVAL_SETTLE="${CORTADOS_S1_EVAL_SETTLE:-20}"
setting GPU_SETTLE 3; setting EVAL_SETTLE 20
# label.py --min-left-pct: a percentage, so 0-100; an invalid or out-of-range value falls back to the 15% floor (Forest 2026-11-03)
MIN_LEFT_PCT="${CORTADOS_S1_MIN_LEFT_PCT:-15}"
setting MIN_LEFT_PCT 15
[ "$MIN_LEFT_PCT" -gt 100 ] && { SETTING_NOTES="${SETTING_NOTES:-}MIN_LEFT_PCT=$MIN_LEFT_PCT is above 100; using 15. "; MIN_LEFT_PCT=15; }
deadline_settings
eval_time_ok() {   # eval_time_ok <run> <reason>: true when an evaluation can finish before the deadline; else logs why, raises
  # the eval_deferred streak (a candidate deferred night after night is a stall someone must see) and is false
  # Forest 2026-11-03: continuous mode (overnight.py --continuous sets CORTADOS_S1_CONTINUOUS=1) has no deadline, so evaluation is never skipped.
  [ "${CORTADOS_S1_CONTINUOUS:-0}" = 1 ] && return 0
  local left; left=$(minutes_left)
  [ "$EVAL_MINUTES" -eq 0 ] || [ "$left" -ge "$EVAL_MINUTES" ] && return 0
  echo "not evaluating $1: $left min to the $DEADLINE deadline, evaluation needs ~$EVAL_MINUTES (CORTADOS_S1_EVAL_MINUTES); it stays pending ($2) and is evaluated first next night"
  streak eval_deferred hit "$EVAL_FAIL_NIGHTS" "evaluation deferred: not enough time before the deadline"
  return 1
}
# write_pending <sft> <eval> <reason> <run>: atomic (tmp + mv, like mark_trained) and checked; a marker that cannot be written
# is fatal: evaluating without it would let a TERM leave the trained run unmarked and the next retention pass delete it
write_pending() {
  if ! { echo "$1 $2 $3 $4" > data/pending-candidate.tmp && mv data/pending-candidate.tmp data/pending-candidate; }; then
    echo "ERROR: could not write data/pending-candidate ($3 $4); stopping without evaluating"; exit 1
  fi
}
# read_pending: sets p_sft p_eval p_reason p_run from a VALID marker (exactly four non-empty fields) and returns 0; else 1.
# An invalid marker is treated as absent: recovery then re-adopts the trained run, so a garbled marker never hides it from prune.
read_pending() {
  local extra=""
  p_sft=""; p_eval=""; p_reason=""; p_run=""
  [ -s data/pending-candidate ] || return 1
  read -r p_sft p_eval p_reason p_run extra < data/pending-candidate || [ -n "$p_run" ] || return 1
  [ -n "$p_sft" ] && [ -n "$p_eval" ] && [ -n "$p_reason" ] && [ -n "$p_run" ] && [ -z "$extra" ]
}
BASE="${CORTADOS_S1_BASE:-mlx-community/Qwen3-4B-Instruct-2507-4bit}"
export CORTADOS_S1_BASE="$BASE"
# Forest 2026-10-26: fold the Air's synced routed usage into training (another machine rsyncs its router.db here);
# dataset.py namespaces those rows by the db stem so they cannot collide.
if [ -z "${CORTADOS_ROUTER_DB_EXTRA+x}" ] && [ -f "$HOME/.cortados/router-air.db" ]; then
  export CORTADOS_ROUTER_DB_EXTRA="$HOME/.cortados/router-air.db"
fi
trained_unevaluated_runs() {   # runs whose training completed but that have no report.json and are not the pending candidate
  # completion = mlx_lm's final adapters.safetensors ("Saved final weights"): it is written only when a `lora` invocation runs all
  # its iterations, and a completed segment always goes straight to evaluation, so it means "trained, evaluation owed".
  # (Warm starts copy to 0000000_adapters.safetensors and checkpoints are 0*_adapters.safetensors, so neither is mistaken for it.)
  local d
  for d in $(ls -td runs/20*/ 2>/dev/null); do
    d=${d%/}
    [ -f "$d/segment-target" ] && [ -f "$d/adapter/adapters.safetensors" ] && [ ! -f "$d/report.json" ] && echo "$d"
  done
}
newest_unfinished_run() {   # most recent run stopped mid-training: a segment began, checkpoints exist, no final adapter, no report
  # A run with the final adapters.safetensors is trained-but-unevaluated (the pending/recovery path's, see trained_unevaluated_runs),
  # not resumable. It is skipped HERE, inside the search, so an older interrupted run behind it is still found; rejecting it after
  # the search (as before) returned nothing, the older run was not carried, and the retention pass deleted its checkpoints.
  local d
  for d in $(ls -td runs/20*/ 2>/dev/null); do
    d=${d%/}
    [ -f "$d/segment-target" ] && [ ! -f "$d/report.json" ] && [ ! -f "$d/adapter/adapters.safetensors" ] \
      && compgen -G "$d/adapter/*_adapters.safetensors" >/dev/null && { echo "$d"; return; }
  done
}
prune_unresumable_runs() {   # only tonight's run is ever resumed, so other unfinished runs' checkpoints (~0.65 GB each) are dead weight
  local d promoted pending_run=""
  promoted=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['adapter'])" "${SERVE_POINTER:-current.json}" 2>/dev/null)
  # a pending candidate (e.g. its evaluation crashed, so it has no report.json) is kept: it is re-evaluated, not retrained
  read_pending && pending_run=$p_run
  for d in runs/20*/; do
    d=${d%/}
    [ "$d" = "$RUN" ] || [ -f "$d/report.json" ] || [ ! -d "$d/adapter" ] && continue
    [ -n "$pending_run" ] && [ "$d" = "$pending_run" ] && continue
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
# Cross-night resume (continuous mode, Forest 2026-11-08): training only runs in the night window and overnight.py stops it at the
# window end, so a LoRA segment longer than one window spans nights. Each night gets a new runs/<date> folder, and without this the
# retention pass below would delete the previous night's checkpoints, so such a run would never finish. When CORTADOS_S1_RUN is unset
# and today's folder has not started training, carry on in the newest run stopped mid-LoRA (checkpoints, segment-target, no final
# adapters.safetensors, no report: a completed-but-unevaluated run is the pending/recovery path's, not this one's). It becomes $RUN,
# which prune_unresumable_runs always keeps. Rule for older unfinished runs: only the newest is resumed (no age limit); every
# other unfinished, unpromoted, non-pending run loses its checkpoints in the retention pass, as before.
CARRIED=""
if [ "${CORTADOS_S1_CONTINUOUS:-0}" = 1 ] && [ -z "${CORTADOS_S1_RUN:-}" ] && [ ! -d "$RUN/adapter" ] && [ ! -f "$RUN/report.json" ]; then
  carry=$(newest_unfinished_run)
  if [ -n "$carry" ] && [ "$carry" != "$RUN" ]; then CARRIED="$RUN"; RUN="$carry"; fi
fi
mkdir -p "$RUN" data
exec >> "$RUN/nightly.log" 2>&1
echo "=== $(date) nightly start (base $BASE, run $RUN)"
[ -n "$CARRIED" ] && echo "cross-night resume: $CARRIED has not started; continuing the unfinished run $RUN from its checkpoints"
[ -n "${SETTING_NOTES:-}" ] && echo "settings: $SETTING_NOTES" && SETTING_NOTES=""
# Recovery (2026-11-03: evaluation TERMed at 07:00 left runs/2026-11-03 trained, unevaluated and unmarked, so the next night pruned
# it and retrained from step 0, forever). A trained, unevaluated run with no pending marker is adopted as evaluation_pending BEFORE
# the retention pass. Placeholder fingerprints are fine: evaluation_pending is re-evaluated whatever the fingerprints say.
# Only the newest is adopted (the marker holds one run); older ones are logged and left to the retention pass.
if ! read_pending; then
  [ -e data/pending-candidate ] && echo "data/pending-candidate is invalid ($(head -c 200 data/pending-candidate | tr -d '\000\n')); treated as absent"
  adopt=$(trained_unevaluated_runs | grep -vxF "$RUN" | head -1)
  if [ -n "$adopt" ]; then
    write_pending unknown unknown evaluation_pending "$adopt"
    echo "recovered $adopt: training completed but it has no report.json; pending (evaluation_pending), evaluated before any new training"
    for d in $(trained_unevaluated_runs | grep -vxF "$RUN" | grep -vxF "$adopt"); do
      echo "recovery: $d is also trained but unevaluated; only the newest is kept pending, so the retention pass removes its checkpoints"
    done
  else
    rm -f data/pending-candidate
  fi
fi
prune_unresumable_runs
# names only: values can be an internal provider URL or a secret's path
[ -f data/train-config.env ] && echo "train-config sets: $(grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' data/train-config.env | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' | tr '\n' ' ')"
rotate_logs() {   # keep the newest $LOG_KEEP nightly logs; run folders (adapters, reports) are untouched
  local old
  whole "$LOG_KEEP" || { echo "log rotation: CORTADOS_S1_LOG_KEEP=$LOG_KEEP is not a whole number; keeping 14"; LOG_KEEP=14; }
  LOG_KEEP=$(dec "$LOG_KEEP")
  # 0 would delete tonight's own log (tail -n +1 lists every file): keep at least the current one
  [ "$LOG_KEEP" -lt 1 ] && { echo "log rotation: CORTADOS_S1_LOG_KEEP=$LOG_KEEP would delete tonight's log; keeping 1"; LOG_KEEP=1; }
  old=$(ls -t runs/*/nightly.log 2>/dev/null | tail -n +"$((LOG_KEEP + 1))")
  [ -n "$old" ] && echo "$old" | while IFS= read -r f; do rm -f -- "$f"; done && echo "log rotation: removed $(echo "$old" | wc -l | tr -d ' ') old nightly log(s)"
  return 0
}
rotate_logs
gpu_alone() {   # called only right before training starts, so it marks the cycle worked. Forest 2026-09-27: per-turn extraction must never stop, so ollama (the 8B) and the memory gateway STAY UP during training.
  # Forest 2026-11-03: in continuous mode the live scorer has priority and STAYS UP; overnight.py pauses this tree instead when
  # scorer latency rises more than CORTADOS_S1_LATENCY_BUDGET_S above baseline (inside CORTADOS_S1_NIGHT_WINDOW, local time,
  # default 01:00-07:00, the looser CORTADOS_S1_NIGHT_LATENCY_BUDGET_S applies instead, default 6 s; free-memory guard unchanged).
  worked_cycle
  [ "${CORTADOS_S1_CONTINUOUS:-0}" = 1 ] && { echo "continuous mode: scorer stays up during training (overnight.py latency guard)"; return 0; }
  launchctl unload "$SCORER_PLIST" 2>/dev/null; sleep "$GPU_SETTLE"; echo "scorer stopped for training; ollama + memory gateway stay up"
}
# worked_cycle: tell overnight.py --continuous this cycle did real work (labels added, training ran, an evaluation/promotion was
# attempted). Absence means idle: every no-op exit sleeps CORTADOS_S1_CYCLE_IDLE_S instead of restarting at once (a missed call
# only costs one idle sleep; a missed "idle" mark used to hot-loop the provider and quota).
# quota_cycle: the labeler stopped at the Claude headroom floor; overnight.py then waits CORTADOS_S1_QUOTA_COOLDOWN_S. It wins over
# "worked" (labels added before the stop, training after it): once written, worked_cycle no longer overwrites it.
CYCLE_QUOTA=0
cycle_mark() {
  [ -n "${CORTADOS_S1_CYCLE_RESULT:-}" ] || return 0
  echo "$1" > "$CORTADOS_S1_CYCLE_RESULT" || echo "WARNING: could not write cycle result $1 to CORTADOS_S1_CYCLE_RESULT"
  return 0
}
worked_cycle() { [ "$CYCLE_QUOTA" = 1 ] || cycle_mark worked; return 0; }
quota_cycle() { CYCLE_QUOTA=1; cycle_mark quota; }
gpu_share() {
  # continuous mode never unloads the scorer (gpu_alone), so there is nothing to load back and no "scorer back" to log each cycle
  [ "${CORTADOS_S1_CONTINUOUS:-0}" = 1 ] && return 0
  launchctl load "$SCORER_PLIST" 2>/dev/null; echo "scorer back"
}
state() { python3 -B ./nightly_state.py "$@"; }
# Studio capacity handshake: each training/eval step runs under a training intent, so the capacity daemon unloads the
# local Ollama model (confirmed via /api/ps) BEFORE the step starts. Bounded 30 s wait; never proceeds silently.
GATE_ROOT="$(cd .. && pwd)"   # repo root, fixed here: gated also runs from inside s1/laya
# The gate interpreter is resolved to an absolute path HERE (cwd s1/): $PY defaults to ../.venv-mlx/bin/python, which
# would resolve to s1/.venv-mlx after the laya branch's `cd laya`. A bare name (python3) stays a PATH lookup.
case "$PY" in /*) GATE_PY="$PY" ;; */*) GATE_PY="$(cd "$(dirname "$PY")" 2>/dev/null && pwd)/$(basename "$PY")" || GATE_PY="$PY" ;; *) GATE_PY="$PY" ;; esac
# Test-only: CORTADOS_STUDIO_ACK_WAIT / CORTADOS_STUDIO_OLLAMA_PORT (see studio_capacity.py) pass through the environment.
# Exit 75 = the gate refused (model residency could not be confirmed absent); the command did not run.
# CORTADOS_S1_TRAIN_ALLOWED=0 (set by overnight.py outside the night window) is enforced again here so no gated step can slip
# through a future code path; the main flow exits before the first gated step, so reaching this refusal is a bug, not a skip.
gated() {
  [ "${CORTADOS_S1_TRAIN_ALLOWED:-1}" = 0 ] && { echo "gated step refused: ${CORTADOS_S1_TRAIN_SKIP_REASON:-training not allowed now}"; return 75; }
  PYTHONPATH="$GATE_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$GATE_PY" -B -m cortados_router.studio_capacity gate -- "$@"
}
# Calibration sweep (evaluation only, never promotes): when data/sweep-request.json exists
#   {"candidate": "runs/<c>/adapter", "live": "runs/<l>/adapter", "tilts": [...], "alphas": [...]}
# calibrate.py grades the requested tilt/blend settings and writes runs/<c>/sweep.json (+ sweep.md). Gated, so it holds the
# training lock and refuses when CORTADOS_S1_TRAIN_ALLOWED=0. When: from the EXIT trap (sweep_on_exit), so it runs on EVERY
# in-window cycle whatever path ended it (no new labels, pending candidate, laya night, resume, full train+evaluate), always
# AFTER that cycle's own work: it can delay the next cycle, never replace this one's training. A failed or interrupted sweep
# keeps the request for the next cycle (logged); a finished one renames it sweep-request.done-<stamp>.json.
# Bound on waiting for the gate after the stop TERM: the gate's own TRAINING_STOP_GRACE_SECONDS (30 s, studio_capacity.py)
# for its group plus 15 s margin for its own exit. Stays under the supervisor's 60 s TRAIN_STOP_GRACE_S, so nightly.sh
# finishes its cleanup before it could be SIGKILLed itself. Overridable only so tests do not wait 45 s.
SWEEP_STOP_WAIT_S="${CORTADOS_S1_SWEEP_STOP_WAIT_S:-45}"
sweep_alive() {   # running and not yet a zombie (an unreaped child keeps its pid, so kill -0 alone would say alive)
  local st; st=$(ps -o stat= -p "$1" 2>/dev/null) || return 1
  case "$st" in *Z*) return 1 ;; esac
}
# TERM the gate only (it stops its own group with a bounded grace), wait up to SWEEP_STOP_WAIT_S; if it still lives, SIGKILL
# the gate and its command's group (the gate's direct children lead their own groups: start_new_session) and log it. The pid
# stays reserved until the final wait reaps it, so no signal here can reach a recycled pid.
stop_sweep() {
  local pid=$SWEEP_PID kids k end=$((SECONDS + SWEEP_STOP_WAIT_S))
  [ -n "$pid" ] || return 0
  kill -TERM "$pid" 2>/dev/null || echo "sweep stop: TERM to gate $pid failed (already gone)"
  while sweep_alive "$pid" && [ $SECONDS -lt $end ]; do sleep 0.2; done
  if sweep_alive "$pid"; then
    kids=$(pgrep -P "$pid")
    echo "SWEEP STOP: gate $pid still running ${SWEEP_STOP_WAIT_S}s after TERM; SIGKILL to the gate and groups: ${kids:-none}"
    kill -KILL "$pid" 2>/dev/null || echo "sweep stop: SIGKILL to gate $pid failed"
    for k in $kids; do kill -KILL -- "-$k" 2>/dev/null || echo "sweep stop: SIGKILL to group $k failed (gone)"; done
  fi
  wait "$pid" 2>/dev/null
  SWEEP_PID=""
}
sweep_request() {
  local req=data/sweep-request.json args rc
  [ -f "$req" ] || return 0
  [ "${CORTADOS_S1_TRAIN_ALLOWED:-1}" = 1 ] || { echo "sweep request waits: training not allowed now"; return 0; }
  if ! args=$(python3 - "$req" <<'PY'
import json, sys
from pathlib import Path
r = json.load(open(sys.argv[1]))
if not isinstance(r.get("candidate"), str) or not isinstance(r.get("live"), str):
    sys.exit("sweep request needs string candidate and live")
for k in ("tilts", "alphas"):
    if not isinstance(r.get(k, []), list) or not all(isinstance(x, (int, float)) for x in r.get(k, [])):
        sys.exit(f"sweep request {k} must be a list of numbers")
out = Path(r["candidate"]).parent / "sweep.json"
print("\n".join(["--candidate", r["candidate"], "--live", r["live"], "--out", str(out),
                  "--tilts", *map(str, r.get("tilts", [0])), "--alphas", *map(str, r.get("alphas", [1.0]))]))
PY
  ); then echo "SWEEP REQUEST INVALID ($req); left in place"; return 0; fi
  local IFS=$'\n'; set -f; local -a argv=($args); set +f; unset IFS
  # The GATE ITSELF is the background job (an external simple command with `&`: bash forks and execs it, so $! is the gate's
  # pid, not a bash wrapper's). A stop then signals that pid and the gate stops its own process group (the calibrate run
  # holding the inherited lock fd); a wrapper pid could die first and orphan the gate with the lock held.
  # SWEEP_CANCELLED is latched by the TERM/INT trap set in sweep_on_exit; it is checked before the launch, after the pid is
  # known (a trap that fired between the check and the assignment still stops the new gate) and before the rename.
  [ "$SWEEP_CANCELLED" = 1 ] && { echo "SWEEP NOT STARTED: stop requested; request kept for the next cycle"; return 0; }
  PYTHONPATH="$GATE_ROOT${PYTHONPATH:+:$PYTHONPATH}" HF_HUB_OFFLINE=1 CORTADOS_S1_FAIL_UP="${CORTADOS_S1_FAIL_UP:-0.75}" \
    CORTADOS_S1_OUTWARD_AT="${CORTADOS_S1_OUTWARD_AT:-0.25}" \
    "$GATE_PY" -B -m cortados_router.studio_capacity gate -- "$PY" calibrate.py --base "$BASE" "${argv[@]}" > data/sweep-last.log 2>&1 &
  SWEEP_PID=$!
  while :; do
    [ "$SWEEP_CANCELLED" = 1 ] && { stop_sweep; echo "SWEEP INTERRUPTED by a stop; request kept for the next cycle"; return 0; }
    wait "$SWEEP_PID"; rc=$?
    # wait also returns (>128) when the TERM/INT trap ran; the gate is then still running: loop to stop it.
    [ "$SWEEP_CANCELLED" = 1 ] && continue
    break
  done
  SWEEP_PID=""
  if [ $rc -ne 0 ]; then echo "SWEEP FAILED rc=$rc: $(tail -1 data/sweep-last.log | cut -c1-200); request kept for next night"; return 0; fi
  [ "$SWEEP_CANCELLED" = 1 ] && { echo "SWEEP INTERRUPTED by a stop after it finished; request kept for the next cycle"; return 0; }
  grep '^sweep:' data/sweep-last.log | tail -1
  mv "$req" "data/sweep-request.done-$(date +%Y%m%d-%H%M%S).json"
}
# Armed once the cycle's traps are in place and only when CORTADOS_S1_TRAIN_ALLOWED=1; the TERM/INT trap disarms it, so a stop
# (window end, supervisor, --stop) never starts a sweep. Runs at most once (disarmed before it starts). Not after exit 130
# (stop requested) or a signal death (>128), nor while the stop flag is present. During the sweep TERM/INT latch
# SWEEP_CANCELLED; sweep_request stops and reaps the gate (stop_sweep), keeps the request, and cleanup finishes normally.
SWEEP_ARMED=0; SWEEP_PID=""; SWEEP_CANCELLED=0
sweep_on_exit() {
  local rc=$1
  [ "$SWEEP_ARMED" = 1 ] || return 0
  SWEEP_ARMED=0
  [ "${CORTADOS_S1_TRAIN_ALLOWED:-1}" = 1 ] || return 0
  [ "$rc" -lt 128 ] || return 0
  [ -f "${STOPFLAG:-/nonexistent}" ] && return 0
  [ -f data/sweep-request.json ] || return 0
  # Latch only: sweep_request acts on it at its checkpoints (before launch, after the pid is known, after wait, before rename).
  trap 'echo "stop requested during sweep"; SWEEP_CANCELLED=1' TERM INT
  echo "=== $(date) sweep request (after this cycle's work)"
  sweep_request
}
evaluate_run() {   # grade <run>/adapter against the CURRENT incumbent and holdout; writes <run>/report.json
  worked_cycle
  HF_HUB_OFFLINE=1 CORTADOS_S1_FAIL_UP="${CORTADOS_S1_FAIL_UP:-0.75}" CORTADOS_S1_OUTWARD_AT="${CORTADOS_S1_OUTWARD_AT:-0.25}" \
    gated "$PY" evaluate.py --base "$BASE" --adapter "$1/adapter" --out "$1" --baseline
}
# The no-new-data marker is committed only once a night's outcome is final (published, or intentionally not promoted): saved
# before a failed promotion, the next unchanged-data night would skip and the approved candidate would never be served.
mark_trained() { echo "$sft_hash" > data/last-train-labels.tmp && mv data/last-train-labels.tmp data/last-train-labels; }
# streak <name> hit|clear <threshold> [<detail>]: record a consecutive-night condition; print its ALERT; a failing
# nightly_state call is logged (an alert that silently failed to record is an alert nobody sees)
streak() {
  local out rc
  out=$(state streak "$STATUS" "$1" "$2" "$NIGHT" "$3" "${4:-}"); rc=$?
  if [ "$rc" -ne 0 ]; then echo "WARNING: recording the $1 streak failed (nightly_state rc $rc): its alert state is unknown"; return 0; fi
  [ -n "$out" ] && echo "$out"; return 0
}
eval_failed() { streak evaluation_failed hit "$EVAL_FAIL_NIGHTS" "evaluation unavailable or crashed"; }
eval_ok() { streak evaluation_failed clear "$EVAL_FAIL_NIGHTS"; }
# report.json readers exit 2 when the file is unreadable or malformed (no boolean "promote"); report_unavailable treats that
# like an unavailable evaluation, so a broken report goes to the evaluation_failed branch, never to a verdict.
report_check() { python3 - "$1/report.json" <<'PY'
import json, sys
try:
    r = json.load(open(sys.argv[1]))
    assert isinstance(r, dict) and isinstance(r.get("promote"), bool)
except Exception:
    sys.exit(2)
sys.exit(0 if r.get("evaluation_status") == "unavailable" else 1)
PY
}
report_unavailable() { report_check "$1"; [ $? -ne 1 ]; }
report_why() { python3 -c "import json,sys
try: print(json.load(open(sys.argv[1])).get('evaluation_unavailable') or 'report.json unreadable or malformed')
except Exception: print('report.json unreadable or malformed')" "$1/report.json"; }
holdout_count() { CORTADOS_S1_FAIL_UP="${CORTADOS_S1_FAIL_UP:-0.75}" CORTADOS_S1_OUTWARD_AT="${CORTADOS_S1_OUTWARD_AT:-0.25}" $PY evaluate.py --holdout-count; }
holdout_hit() { streak holdout_blocked hit "$HOLDOUT_BLOCKED_NIGHTS" "candidate waits for the Claude holdout backfill"; }
holdout_clear() {   # the holdout-blocked candidate is resolved: its blocked streak and any failed-count streak end with it
  streak holdout_blocked clear "$HOLDOUT_BLOCKED_NIGHTS"
  streak holdout_count_failed clear "$HOLDOUT_BLOCKED_NIGHTS"
}
report_blocked() { python3 -c "import json,sys;sys.exit(0 if json.load(open(sys.argv[1])).get('promote_blocked') else 1)" "$1/report.json"; }
eval_fingerprint() {   # effective holdout labels + inference settings (evaluate.py --holdout-key) + the serving incumbent
  local hk inc
  hk=$(CORTADOS_S1_FAIL_UP="${CORTADOS_S1_FAIL_UP:-0.75}" CORTADOS_S1_OUTWARD_AT="${CORTADOS_S1_OUTWARD_AT:-0.25}" $PY evaluate.py --holdout-key) || return 1
  inc=$(state fingerprint "$SERVE_POINTER" 2>/dev/null) || inc=none
  echo "${hk}-${inc}"
}
report_promotes() { python3 -c "import json,sys;sys.exit(0 if json.load(open(sys.argv[1]))['promote'] else 1)" "$1/report.json"; }
deadline=""
stop_deadline() {
  # Own and reap the watcher on every exit, not just successful training; otherwise it can set a stale stop flag later.
  local pid
  if [ -n "$deadline" ]; then
    # Bash may already have reaped a naturally completed watcher. Its saved PID can
    # then be reused, so signal only a running/stopped job still owned by this shell.
    for pid in $(jobs -pr) $(jobs -ps); do
      [ "$pid" = "$deadline" ] || continue
      kill -TERM "$deadline" 2>/dev/null
      break
    done
    wait "$deadline" 2>/dev/null
    deadline=""
  fi
}
cleanup() { local rc=$?; sweep_on_exit "$rc"; stop_deadline; pkill -TERM -P $$ 2>/dev/null; gpu_share; rm -f "$PIDFILE"; }
trap cleanup EXIT
# Continuous mode: the stop flag means "the operator ran nightly.sh --stop" (only --stop creates it) and overnight.py will not
# relaunch while it exists. A TERM here can also be the supervisor's own stop_tree (launchctl unload/kickstart -k, logout,
# shutdown): flagging that would leave continuous training stopped for good after a reboot, so the trap does not touch it.
# Window mode keeps the old behaviour (a TERM is a stop for tonight; the next night clears the flag).
trap 'echo "stop requested"; SWEEP_ARMED=0; [ "${CORTADOS_S1_CONTINUOUS:-0}" = 1 ] || touch "$STOPFLAG"; exit 130' TERM INT
[ "${CORTADOS_S1_TRAIN_ALLOWED:-1}" = 1 ] && SWEEP_ARMED=1   # the sweep request runs from cleanup on a normal end of this cycle
# unitcost / refresh-tiers live in the router checkout; a missing one is logged, never fatal (labelling and training do not need them)
if [ -f "$REPO_ROOT/cortados_router/unitcost.py" ]; then python3 "$REPO_ROOT/cortados_router/unitcost.py" > /dev/null 2>&1 || echo "unitcost.py failed (rc $?)"   # daily re-normalisation: tokens per percent per pool
else echo "unitcost.py not found under $REPO_ROOT: skipped"; fi
# every night: what Cursor/Antigravity/Codex list today + fresh benchmark scores → every model re-scored per use case.
# Continuous mode runs a cycle every few minutes, and refresh-tiers.sh fetches public leaderboards (lmarena, swebench, epoch,
# aider, ...): refreshing every cycle would hammer those sites for scores that change daily at most. So in continuous mode the
# refresh runs at most once per CORTADOS_S1_BENCH_REFRESH_S (default 86400 = 24 h), timed from data/last-bench-refresh, which
# is stamped only after a successful refresh (a failed one is retried next cycle). Window mode refreshes every night as before.
bench_due=1
if [ "${CORTADOS_S1_CONTINUOUS:-0}" = 1 ]; then
  BENCH_REFRESH_S="${CORTADOS_S1_BENCH_REFRESH_S:-86400}"
  whole "$BENCH_REFRESH_S" || { echo "CORTADOS_S1_BENCH_REFRESH_S=$BENCH_REFRESH_S is not a whole number up to 9 digits; using 86400"; BENCH_REFRESH_S=86400; }
  BENCH_REFRESH_S=$(dec "$BENCH_REFRESH_S")
  last_bench=$(cat data/last-bench-refresh 2>/dev/null)
  # a future-dated stamp (clock change, bad write) gives a negative age: treat it as due, not as fresh for ever
  if [[ "$last_bench" =~ ^[0-9]{1,12}$ ]] && [ $(( $(date +%s) - last_bench )) -ge 0 ] \
     && [ $(( $(date +%s) - last_bench )) -lt "$BENCH_REFRESH_S" ]; then
    echo "benchmark/tiers refresh skipped: last refresh $(( $(date +%s) - last_bench )) s ago, under CORTADOS_S1_BENCH_REFRESH_S=$BENCH_REFRESH_S (continuous mode)"
    bench_due=0
  fi
fi
if [ "$bench_due" = 0 ]; then :
elif [ -x "$REPO_ROOT/bin/refresh-tiers.sh" ]; then
  if "$REPO_ROOT/bin/refresh-tiers.sh"; then
    date +%s > data/last-bench-refresh || echo "WARNING: could not write data/last-bench-refresh: the next cycle refreshes again"
  else echo "refresh-tiers.sh failed (rc $?)"; fi
else echo "refresh-tiers.sh not found under $REPO_ROOT: skipped"; fi
# The Air's router.db is rsynced here every 15 min by another machine; a stale copy means its recent usage is missing tonight.
# Stale data is still valid data, so the night goes on, but the gap is logged and put in the status file for the dashboard.
IFS=: read -ra extra_dbs <<< "${CORTADOS_ROUTER_DB_EXTRA:-}"   # os.pathsep list, as dataset.py reads it; paths may contain spaces
for db in "${extra_dbs[@]+"${extra_dbs[@]}"}"; do
  [ -n "$db" ] || continue
  if [ ! -f "$db" ]; then echo "WARNING: synced router db $db is missing"; state event "$STATUS" stale_router_db "$db missing"; continue; fi
  mtime=$(stat -f %m "$db" 2>/dev/null || stat -c %Y "$db" 2>/dev/null)
  if ! [[ "$mtime" =~ ^[0-9]{1,12}$ ]]; then
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
# Forest 2026-11-03: two Claude accounts are under-utilized, so labeling may run in the daytime down to a 15% per-account floor.
labels_before=$(state labels data/labels.jsonl 2>/dev/null) && whole "$labels_before" || labels_before=""
"$LABEL_PY" -B label.py --min-left-pct "$MIN_LEFT_PCT" $TEACHER_ARGS
rc=$?
# Count labels and update the stall tracker BEFORE branching on rc: a labeler that fails every night is exactly the stall to alert on.
if ! labelled=$(state labels data/labels.jsonl) || ! whole "$labelled"; then
  echo "LABEL COUNT FAILED (data/labels.jsonl unreadable): training skipped"; state event "$STATUS" label_count_failure "labels.jsonl unreadable"; exit 1
fi
[ -n "$labels_before" ] && [ "$labelled" -gt "$labels_before" ] && worked_cycle
if alert=$(state growth "$STATUS" "$labelled" "$STALL_NIGHTS" "$NIGHT"); then
  [ -n "$alert" ] && echo "$alert"
else
  echo "WARNING: recording label growth failed (nightly_state rc $?): the stall alert state is unknown"
fi
# label.py exit codes: 0 completed, 2 config error, 3 stopped early (quota/auth/retries; the labels it wrote are kept).
if [ "$rc" -eq 3 ]; then
  # partial labels are real labels: go on, and let the no-new-labels check below decide whether there is anything to train on
  stop_reason=$(head -1 data/label-stop-reason 2>/dev/null | tr -cd 'a-z_'); stop_reason=${stop_reason:-unknown}
  echo "LABELING STOPPED EARLY (label.py rc 3, reason $stop_reason): continuing with the labels written so far"
  case "$stop_reason" in low_headroom|unknown_headroom) quota_cycle ;; esac   # the headroom floor: overnight.py cools down
  state event "$STATUS" labeling_stopped_early "label.py rc 3: $stop_reason"
  # one batch then a stop still "grows" labels, so the growth stall never fires: alert on the same stop reason night after night
  streak label_early_stop hit "$EARLY_STOP_NIGHTS" "$stop_reason"
elif [ "$rc" -eq 0 ]; then
  streak label_early_stop clear "$EARLY_STOP_NIGHTS"
else
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
last=$(cat data/last-train-count 2>/dev/null || echo 0); whole "$last" || { echo "data/last-train-count unreadable ($last); counting from 0"; last=0; }; last=$(dec "$last")
new=$((labelled - last))
pending=0
if [ ! -f "$RUN/report.json" ] && { [ -f "$RUN/segment-target" ] || compgen -G "$RUN/adapter/0*_adapters.safetensors" >/dev/null; }; then
  pending=1
fi
# A pending candidate is a trained run whose outcome is not final yet, kept in data/pending-candidate as
#   "<sft fingerprint> <eval fingerprint> <reason> <run>"
#   reason promotion_failed: approved but the pointer write failed → re-evaluate and publish on the next night;
#   reason evaluation_failed: evaluate.py crashed or could not build the bar (evaluation_status unavailable) → re-evaluate next
#                            night whatever the fingerprints say; never finalised as a rejection;
#   reason evaluation_pending: training finished but evaluation never produced a report (deadline TERM, stop, or not enough time
#                            left). Written BEFORE evaluate_run starts so a TERM mid-evaluation leaves it. Re-evaluated next night
#                            WHATEVER the SFT fingerprint says: labeling adds data every night, so superseding on new data (the
#                            policy for the other reasons) would discard every trained candidate and the pipeline would never promote.
#   reason holdout_blocked:  evaluate.py refused for a too-small Claude holdout. Holdout labels never enter the SFT data, so the
#                            SFT marker cannot notice the backfill; the run waits and is re-evaluated when the eval fingerprint
#                            (effective holdout labels + settings + serving incumbent) changes.
# Re-evaluation always grades against today's holdout and incumbent (an earlier approval may be stale). New SFT data supersedes it.
# Night-window training (Forest 2026-11-08): everything below may run a gated (Studio-heavy) step, so outside the window the cycle
# ends here with rc 0 after labeling and the dataset refresh. Not a failure; overnight.py sleeps the idle interval unless labels grew.
if [ "${CORTADOS_S1_TRAIN_ALLOWED:-1}" = 0 ]; then
  echo "${CORTADOS_S1_TRAIN_SKIP_REASON:-training skipped: outside the night window}"
  echo "=== $(date) nightly end"; exit 0
fi
force="${1:-}"; [ -f data/force-train ] && force="--train" && rm -f data/force-train
if [ -f data/pending-candidate ] && [ "$force" = "--train" ]; then
  # an explicit --train / data/force-train is a request for a fresh candidate: it never waits behind a pending one
  read_pending
  echo "forced training: pending candidate $p_run ($p_reason) superseded"; rm -f data/pending-candidate
  holdout_clear   # the waiting candidate (and any failed count about it) is resolved
fi
if read_pending; then
  if [ ! -d "$p_run/adapter" ]; then
    echo "pending candidate $p_run adapter missing; dropped"; state event "$STATUS" pending_failure "$p_run adapter missing"
    rm -f data/pending-candidate
  elif [ "$p_sft" = "$sft_hash" ] || [ "$p_reason" = evaluation_pending ]; then
    # Convergence: a night with an evaluation_pending candidate spends itself on that evaluation (it starts right after labeling,
    # ~02:45, so 110 min fits) and ends; the next night trains on the newer data. Only a run whose training COMPLETED is protected
    # this way. Mid-training progress on a non-completed run resumes within its own run folder (segment-target / iters-base); in
    # continuous mode that folder is carried across nights (cross-night resume above), in window mode it is not.
    # Only when the data is unchanged does a verdict mark it trained (mark_trained): newer labels were never trained on.
    # Every rewrite of this marker below keeps "$p_sft", the data the candidate was TRAINED on (or recovery's deliberate
    # "unknown"): stamping tonight's hash would claim it saw labels it never trained on, and a later verdict would then mark them trained.
    same_data=0; [ "$p_sft" = "$sft_hash" ] && same_data=1
    trained_mark() { [ "$same_data" -eq 1 ] && mark_trained; return 0; }
    if ! now_eval=$(eval_fingerprint); then
      echo "EVAL FINGERPRINT FAILED for pending $p_run; retried next night"; state event "$STATUS" pending_failure "$p_run eval fingerprint"; exit 1
    fi
    if [ "$p_reason" = holdout_blocked ] && [ "$now_eval" = "$p_eval" ]; then
      echo "skipped: no new labels; pending $p_run waits for the holdout backfill (holdout and incumbent unchanged)"
      state event "$STATUS" skip "pending $p_run waits for holdout ($p_reason)"; holdout_hit
      echo "=== $(date) nightly end"; exit 0
    fi
    if ! eval_time_ok "$p_run" "$p_reason"; then
      state event "$STATUS" skip "pending $p_run: not enough time to evaluate"; echo "=== $(date) nightly end"; exit 0
    fi
    echo "pending candidate $p_run ($p_reason): re-evaluating against the current holdout and incumbent"
    if ! evaluate_run "$p_run"; then
      echo "re-evaluation of $p_run failed; retried next night"; state event "$STATUS" pending_failure "$p_run (re-evaluation failed)"; eval_failed; exit 1
    fi
    if report_unavailable "$p_run"; then
      write_pending "$p_sft" "$now_eval" evaluation_failed "$p_run"
      echo "EVALUATION UNAVAILABLE again for $p_run: $(report_why "$p_run"); retried next night"
      state event "$STATUS" evaluation_failure "$p_run (retry)"; eval_failed; exit 1
    fi
    eval_ok; streak eval_deferred clear "$EVAL_FAIL_NIGHTS"
    if [ "$same_data" -eq 1 ]; then next_msg="no new data, nothing to train"; else next_msg="the newer labels are trained next night"; fi
    if ! report_promotes "$p_run"; then
      if report_blocked "$p_run"; then
        write_pending "$p_sft" "$now_eval" holdout_blocked "$p_run"
        echo "pending $p_run still blocked: $(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['promote_blocked'])" "$p_run/report.json")"
        state event "$STATUS" skip "pending $p_run still holdout-blocked"; holdout_hit
      else
        rm -f data/pending-candidate; trained_mark; holdout_clear
        state event "$STATUS" skip "pending $p_run no longer promotes"
        echo "pending $p_run no longer promotes (see $p_run/report.json); incumbent stays; $next_msg"
      fi
      echo "=== $(date) nightly end"; exit 0
    fi
    if state promote "$p_run/report.json" "$PWD/$p_run/adapter" "$SERVE_POINTER"; then
      rm -f data/pending-candidate; state event "$STATUS" promotion "$p_run (pending, $p_reason)"; trained_mark; holdout_clear
      echo "PROMOTED (retry) $p_run → $SERVE_POINTER; $next_msg"; echo "=== $(date) nightly end"; exit 0
    fi
    write_pending "$p_sft" "$now_eval" promotion_failed "$p_run"
    echo "PROMOTION FAILED again for $p_run → $SERVE_POINTER"; state event "$STATUS" promotion_failure "$p_run (retry)"; exit 1
  else
    # New train labels. While the Claude holdout is below the floor the bar cannot judge ANY candidate: retraining would only
    # produce another blocked run every night (a GPU loop) and would discard this one. So the holdout readiness is checked
    # before superseding a pending candidate whatever its last reason (holdout_blocked, evaluation_failed, promotion_failed);
    # --train / data/force-train override (handled above).
    # The count must be trustworthy: a failed or garbled count (e.g. malformed holdout answers crash build_eval_sets) must not
    # fall through to discarding the candidate and training.
    h_rows=""; h_floor=""; hc_ok=0
    # stdout only (evaluate.py's label loader reports its Claude-only filter on stderr), captured byte-exact in a file:
    # $(...) would strip every trailing newline and let "300 200\n\n" pass
    hc_out=$(mktemp "${TMPDIR:-/tmp}/holdout-count.XXXXXX"); hc_err=$(mktemp "${TMPDIR:-/tmp}/holdout-count-err.XXXXXX")
    hc=""
    if holdout_count >"$hc_out" 2>"$hc_err"; then
      # validated as BYTES (bash strings drop NULs): exactly "<rows> <floor>", one line, at most one terminal newline,
      # ASCII digits 1-9 long; prints the normalised integers
      if hc=$(python3 -c 'import re,sys
m = re.fullmatch(rb"(\d{1,9}) (\d{1,9})\n?", open(sys.argv[1], "rb").read())
sys.exit(1) if not m else print(int(m[1]), int(m[2]))' "$hc_out"); then
        read -r h_rows h_floor <<< "$hc"; hc_ok=1
      fi
    fi
    hc=$(head -c 400 "$hc_out" | tr -d '\000')
    if [ "$hc_ok" -ne 1 ]; then
      echo "HOLDOUT COUNT FAILED (evaluate.py --holdout-count: $( { printf '%s\n' "$hc"; cat "$hc_err"; } | grep -v '^$' | tail -1 | cut -c1-160)); pending $p_run kept, not retraining"
      rm -f "$hc_out" "$hc_err"
      state event "$STATUS" pending_failure "$p_run holdout count failed"
      streak holdout_count_failed hit "$HOLDOUT_BLOCKED_NIGHTS" "evaluate.py --holdout-count failed"
      exit 1
    fi
    rm -f "$hc_out" "$hc_err"
    streak holdout_count_failed clear "$HOLDOUT_BLOCKED_NIGHTS"
    if [ "$h_rows" -lt "$h_floor" ]; then
      echo "holdout still $h_rows/$h_floor; not retraining (pending $p_run ($p_reason) waits; --train or data/force-train overrides)"
      state event "$STATUS" skip "holdout still $h_rows/$h_floor; $p_run waits"; holdout_hit
      echo "=== $(date) nightly end"; exit 0
    fi
    echo "pending candidate $p_run superseded by new training data"; rm -f data/pending-candidate
  fi
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
  LAYA_STOP_AT="${LAYA_STOP_AT:-0645}"; [[ "$LAYA_STOP_AT" =~ ^[0-9]{4}$ ]] || { echo "LAYA_STOP_AT=$LAYA_STOP_AT is not HHMM; using 0645"; LAYA_STOP_AT=0645; }
  (
    sleeper=""
    # Wait on an asynchronous sleep so TERM runs immediately, rather than after the 60-second foreground sleep.
    trap 'exit 0' TERM INT
    # The sleep belongs to this watcher: reap it too so exiting nightly leaves no orphan timer.
    trap '[ -z "$sleeper" ] || { kill "$sleeper" 2>/dev/null; wait "$sleeper" 2>/dev/null; }' EXIT
    while now=$(clock_hhmm); [ "$((10#$now))" -lt "$((10#$LAYA_STOP_AT))" ] || [ "$((10#$now))" -gt 1200 ]; do
      sleep 60 & sleeper=$!
      wait "$sleeper"; sleeper=""
      [ -f "$LRUN/train-meta.json" ] && exit 0
    done
    touch "$LRUN/stop"; echo "laya: deadline reached, stop file set"
  ) </dev/null >> "$RUN/nightly.log" 2>&1 &  # Explicitly detach caller pipes, even if the parent's logging changes.
  deadline=$!
  # data/laya-init names a checkpoint dir (e.g. laya/runs/2026-09-30/checkpoint_latest): tonight's run warm-starts from it (2026-09-30: 1,110 of
  # 2,000 steps fitted before the 06:45 deadline at 15.8 s/step — the 8B and nomic share the GPU all night, so a full run takes two nights)
  init=""; [ -f data/laya-init ] && init="$(cat data/laya-init)" && rm -f data/laya-init && [ -d "$init" ] && echo "laya: warm start from $init"
  # Each gated stage's status is kept: 75 = the studio gate refused (the command did not run). A refusal is a nonzero night,
  # and when training never started the one-shot flag (and warm-start pointer) is put back so the trial runs next night.
  ( cd laya && gated ../.venv-laya/bin/python convert.py ); crc=$?; trc=0; erc=""; refused=""
  if [ "$crc" -eq 0 ]; then
    ( cd laya && LAYA_INIT="${init:+../$init}" LAYA_RUN="../$LRUN" LAYA_MAX_STEPS="${LAYA_MAX_STEPS:-2000}" gated ./train.sh ); trc=$?
  fi
  lrc=$crc; [ "$crc" -eq 0 ] && lrc=$trc
  # Restore the one-shot flags the moment a refusal is seen, before the cleanup and settle delay below: a supervisor TERM
  # there runs the exit trap, which does not know about them, and the requested trial and warm-start pointer would be lost.
  if [ "$crc" -eq 75 ] || [ "$trc" -eq 75 ]; then
    if [ "$crc" -eq 75 ]; then refused=convert; else refused=train; fi
    touch data/laya-tonight; [ -n "$init" ] && printf '%s\n' "$init" > data/laya-init
  fi
  stop_deadline
  gpu_share; sleep 20
  if [ -n "$refused" ]; then
    echo "studio gate refused laya $refused (local model residency unconfirmed): laya flag kept for the next night"
  elif [ "$lrc" -eq 0 ] && [ -f "$LRUN/model.safetensors" ]; then
    HF_HUB_OFFLINE=1 CORTADOS_S1_FAIL_UP="${CORTADOS_S1_FAIL_UP:-0.75}" CORTADOS_S1_OUTWARD_AT="${CORTADOS_S1_OUTWARD_AT:-0.25}" gated .venv-laya/bin/python evaluate.py --engine laya --checkpoint "$LRUN" --out "$LRUN"; erc=$?
    if [ "$erc" -eq 75 ]; then
      refused=eval; echo "studio gate refused laya eval (local model residency unconfirmed): $LRUN trained but not evaluated"
    elif [ "$erc" -eq 0 ]; then
      python3 -c "import json;r=json.load(open('$LRUN/report.json'));c=r['candidate'];print('laya holdout: band',c['band_accuracy'],'under',c['under_routed'],'over',c['over_routed'],'outward',c['outward_recall'],'| would promote:',r['promote'])"
    fi
  else
    echo "laya training failed (rc $lrc); see $LRUN/train.log"
  fi
  echo "laya night: Qwen resume skipped tonight"; echo "=== $(date) nightly end"
  [ -z "$refused" ] || exit 1
  exit 0
fi
HF_HUB_OFFLINE=1 gated "$PY" -c "from mlx_lm import load; load('$BASE')" >/dev/null 2>&1; prc=$?   # loads the base: gated
if [ "$prc" -eq 75 ]; then
  echo "studio gate refused (local model residency unconfirmed): training skipped"; exit 1
elif [ "$prc" -ne 0 ]; then
  echo "base model $BASE is not on disk: training skipped (download needs Forest's OK)"; exit 2
fi
n=$(wc -l < data/sft/train.jsonl | tr -d ' '); [[ "$n" =~ ^[0-9]{1,9}$ ]] || { echo "data/sft/train.jsonl line count unreadable: training skipped"; exit 1; }
gpu_alone
BATCH="${CORTADOS_S1_BATCH:-2}"                                   # 2: the 8B stays resident beside training now, so keep the trainer's footprint small
LR="${CORTADOS_S1_LR:-1e-4}"; EPOCHS="${CORTADOS_S1_EPOCHS:-2}"
MAX_ITERS="${CORTADOS_S1_MAX_ITERS:-3600}"; MAX_DEFAULT=3600                          # a night is ~6 h; at ~6 s/step that is ~3,600 steps (2026-09-28: 7,499 steps ran into the day)
# Forest 2026-10-13: a tested candidate every night. A new run (nothing to resume) warm-starts from the promoted adapter and is
# sized to finish AND be evaluated before the 07:00 stop. 1,500 steps: ~3.2 h at the measured 0.13 step/s from ~02:00, leaving
# ~1 h for evaluation; too many and the run is cut off unevaluated (the 10-03..10-13 failure), too few and a night adds little.
# Warm LR 5e-5: half the from-scratch rate, so one night's continued training refines the adapter rather than overwriting it.
if [ -z "${CORTADOS_S1_INIT:-}" ] && [ "$pending" -eq 0 ] && [ -f "${SERVE_POINTER:-current.json}" ]; then
  CORTADOS_S1_INIT=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['adapter'] + '/adapters.safetensors')" "${SERVE_POINTER:-current.json}")
  MAX_ITERS="${CORTADOS_S1_NIGHT_ITERS:-1500}"; MAX_DEFAULT=1500; LR="${CORTADOS_S1_WARM_LR:-5e-5}"
fi
if [ -n "${CORTADOS_S1_INIT:-}" ] && [ -f "$CORTADOS_S1_INIT" ]; then mkdir -p "$RUN/adapter"; cp "$CORTADOS_S1_INIT" "$RUN/adapter/0000000_adapters.safetensors"; cp "$(dirname "$CORTADOS_S1_INIT")/adapter_config.json" "$RUN/adapter/" 2>/dev/null; echo "warm start from $CORTADOS_S1_INIT"; fi
setting MAX_ITERS "$MAX_DEFAULT" 1;   # the selected cap falls back to ITS default (warm night 1500, fresh run 3600)
 setting BATCH 2 1; setting EPOCHS 2 1   # MAX_ITERS bounds the segment below: never let a bad value pass
[ -n "${SETTING_NOTES:-}" ] && echo "settings: $SETTING_NOTES" && SETTING_NOTES=""
iters=$(( n * EPOCHS / BATCH ))
echo "training on $n examples, $iters iterations at batch $BATCH"
attempt=0
while :; do
  resume=""
  last=$(ls -t "$RUN"/adapter/0*_adapters.safetensors 2>/dev/null | head -1)          # newest by time: counters restart at 1 after each resume
  # mlx_lm counts iterations from 1 again on resume: keep a cumulative count in the run dir and hand it only the remaining ones
  ckpt=$(basename "${last:-0000000_x}" | sed -E 's/^0*([0-9]+)_.*/\1/')
  # a checkpoint whose step count cannot be read cannot be resumed correctly (the cumulative count would be wrong): stop
  [[ "$ckpt" =~ ^[0-9]{1,9}$ ]] || { echo "ERROR: checkpoint $last has no readable step count; not resuming (move it aside to start the segment over)"; exit 1; }
  ckpt=$((10#$ckpt))
  # cumulative steps = steps credited BEFORE this checkpoint's segment + the checkpoint's own count. The segment base is recorded when a
  # segment starts (iters-base), never re-added on the next resume (the 2026-09-26 double count).
  base_done=$(cat "$RUN/iters-base" 2>/dev/null || echo 0)
  [[ "$base_done" =~ ^[0-9]{1,9}$ ]] || { echo "iters-base unreadable ($base_done); counting from 0"; base_done=0; }
  done_iters=$(( 10#$base_done + ckpt ))
  if [ -z "$last" ]; then
    last=$(ls -t "$RUN"/adapter/resume_*_adapters.safetensors 2>/dev/null | head -1)
    [ -n "$last" ] && done_iters=$(basename "$last" | sed -E 's/^resume_([0-9]+)_adapters.safetensors$/\1/')
    [[ "$done_iters" =~ ^[0-9]{1,9}$ ]] || { echo "resume checkpoint name unreadable ($last); counting from 0"; done_iters=0; }
    done_iters=$((10#$done_iters))
  fi
  if [ -f "$RUN/segment-target" ]; then
    target=$(cat "$RUN/segment-target")
    if [[ "$target" =~ ^[0-9]{1,9}$ ]]; then
      target=$((10#$target))
    else
      echo "segment-target unreadable ($target); recomputing"
      # if it cannot be removed, recomputing would loop forever with the scorer stopped: fail (the EXIT trap brings it back)
      rm -f "$RUN/segment-target" && [ ! -e "$RUN/segment-target" ] || { echo "ERROR: cannot remove $RUN/segment-target; stopping"; exit 1; }
      continue
    fi
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
  HF_HUB_OFFLINE=1 gated "$PY" -m mlx_lm lora --model "$BASE" --train --data data/sft --fine-tune-type lora --num-layers 16 --batch-size "$BATCH" --iters "$remaining" \
    --learning-rate "$LR" --mask-prompt --max-seq-length 1536 --grad-checkpoint --steps-per-eval 100 --save-every 100 --adapter-path "$RUN/adapter" $resume && break
  [ -f "$STOPFLAG" ] && { echo "stopped by request; not relaunching"; exit 130; }
  attempt=$((attempt + 1)); echo "trainer died (attempt $attempt)"; [ "$attempt" -ge 4 ] && exit 1
  sleep 60
  [ -f "$STOPFLAG" ] && { echo "stopped by request; not relaunching"; exit 130; }
done
# written BEFORE evaluation: a TERM (07:00 deadline, --stop) during evaluate_run must leave the trained run pending, or the next
# night's retention pass deletes its adapter and retrains from step 0 (2026-11-03). Every verdict below overwrites or removes it.
# "+N since last training" counts from the last COMPLETED training, so it is recorded here, when training finishes, not after
# evaluation: a run TERMed mid-evaluation (and re-evaluated by a pending night) was still trained on these labels.
echo "$labelled" > data/last-train-count
# The eval fingerprint is a subprocess that can hang or fail; evaluation_pending ignores it, so the marker goes down first with
# "unknown" and is refreshed after.
write_pending "$sft_hash" unknown evaluation_pending "$RUN"
if run_eval_fp=$(eval_fingerprint); then write_pending "$sft_hash" "$run_eval_fp" evaluation_pending "$RUN"; fi
if ! eval_time_ok "$RUN" evaluation_pending; then
  state event "$STATUS" skip "$RUN trained; not enough time to evaluate"; echo "=== $(date) nightly end"; exit 0
fi
gpu_share; sleep "$EVAL_SETTLE"                                            # the grader's incumbent baseline needs ollama back; the 14B rows are cached anyway
if ! evaluate_run "$RUN"; then
  # a crashed evaluation is not a verdict: keep the run pending and re-evaluate it next night, data unchanged or not
  write_pending "$sft_hash" "$(eval_fingerprint || echo unknown)" evaluation_failed "$RUN"
  state event "$STATUS" evaluation_failure "$RUN (evaluate.py failed)"; eval_failed
  echo "EVALUATION FAILED for $RUN; it stays pending and is re-evaluated next night"; exit 1
fi
state event "$STATUS" train "$RUN ($labelled labels)"
if report_unavailable "$RUN"; then
  write_pending "$sft_hash" "$(eval_fingerprint || echo unknown)" evaluation_failed "$RUN"
  state event "$STATUS" evaluation_failure "$RUN (bar unavailable)"; eval_failed
  echo "EVALUATION UNAVAILABLE for $RUN: $(report_why "$RUN"); not a rejection: re-evaluated next night"
  exit 1
fi
eval_ok; streak eval_deferred clear "$EVAL_FAIL_NIGHTS"
if report_promotes "$RUN"; then
  # 2026-11-02: write the file the scorer reads (it used to be a copy the scorer never saw). serve.py reloads when its mtime changes;
  # the previous pointer is kept as <pointer>.prev — rollback: mv <pointer>.prev <pointer>.
  if state promote "$RUN/report.json" "$PWD/$RUN/adapter" "$SERVE_POINTER"; then
    state event "$STATUS" promotion "$RUN"
    rm -f data/pending-candidate; mark_trained; holdout_clear
    echo "PROMOTED → $SERVE_POINTER (previous kept as $SERVE_POINTER.prev)"
  else
    write_pending "$sft_hash" "$(eval_fingerprint || echo unknown)" promotion_failed "$RUN"   # retried next night
    state event "$STATUS" promotion_failure "$RUN → $SERVE_POINTER"
    echo "PROMOTION FAILED writing $SERVE_POINTER; incumbent stays; publication retried next night"; exit 1
  fi
elif report_blocked "$RUN"; then
  # not final: the holdout is too small to judge. Keep the run waiting for the backfill instead of marking the data trained.
  write_pending "$sft_hash" "$(eval_fingerprint || echo unknown)" holdout_blocked "$RUN"
  state event "$STATUS" skip "$RUN holdout-blocked; waits for the holdout backfill"; holdout_hit
  echo "not promoted: holdout too small (see $RUN/report.json); $RUN waits and is re-evaluated when the holdout grows"
else
  rm -f data/pending-candidate; mark_trained; holdout_clear
  echo "not promoted (see $RUN/report.json); incumbent stays"
fi
echo "=== $(date) nightly end"
