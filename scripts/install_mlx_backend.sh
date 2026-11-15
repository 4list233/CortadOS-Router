#!/bin/bash
# Install the MLX model server (vllm-mlx, continuous batching) on THIS machine (the Pro) as a LaunchAgent serving the
# configured model on loopback, behind the local gate. Run it on the Pro itself, from this checkout:
#
#   scripts/install_mlx_backend.sh [--config PATH] [--dry-run]
#   scripts/install_mlx_backend.sh --uninstall [--dry-run]
#
#   --config     the backend config (default $CORTADOS_LOCAL_BACKEND_CONFIG or ~/.config/cortados/local-backend.json;
#                example: tables/local-backend.example.json). Its backend must be "vllm-mlx"; the model path, served
#                name, port and flags all come from it (the model is config, never this script: a 6-bit copy is a
#                config change).
#   --uninstall  boot the LaunchAgent out and remove its plist (the rollback's last step; the config is kept)
#   --dry-run    print the plist and the launchctl calls; change nothing
#
# Needs, before it is run: the venv the config names with vllm-mlx 0.6.0 in it, and the model directory. It copies the
# prompt guard (integrations/vllm-mlx/cortados_vllm_mlx_guard.py) to <venv>/bin/cortados-vllm-mlx-guard.py and runs its
# --check first: vllm-mlx must be the pinned 0.6.0 and the model must resolve to vllm-mlx's text route, or nothing is
# installed (the LaunchAgent runs vllm-mlx through the guard, which refuses a prompt over the context;
# docs/LOCAL-RESOURCES.md "The backend's prompt cap"). --dry-run runs the check but copies nothing.
# Writes ~/Library/LaunchAgents/com.cortados.vllm-mlx.plist and loads it with launchctl bootstrap gui/<uid>
# (booting out a previous copy first). Logs to ~/.cortados/vllm-mlx.log. It does NOT touch Ollama or the gate: the
# switch (unload Ollama's 27B, set the gate's backend, restart the gate) is in docs/LOCAL-RESOURCES.md "MLX backend".
# Test seams: CORTADOS_LAUNCHCTL (the launchctl binary), HOME.
set -euo pipefail

LABEL="com.cortados.vllm-mlx"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHCTL="${CORTADOS_LAUNCHCTL:-/bin/launchctl}"
PYTHON="${CORTADOS_MLX_INSTALL_PYTHON:-/usr/bin/python3}"
CONFIG="${CORTADOS_LOCAL_BACKEND_CONFIG:-$HOME/.config/cortados/local-backend.json}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/.cortados"
DRY=0; UNINSTALL=0

die() { echo "install_mlx_backend: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="${2:?}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) die "unknown option $1 (usage: $0 [--config PATH] [--uninstall] [--dry-run])" ;;
  esac
done

if [ "$UNINSTALL" = 1 ]; then
  if [ "$DRY" = 1 ]; then
    echo "# would run: $LAUNCHCTL bootout gui/$(id -u)/$LABEL; rm -f $PLIST"
    exit 0
  fi
  "$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  rm -f "$PLIST"
  echo "install_mlx_backend: $LABEL booted out and its plist removed (config $CONFIG kept)"
  exit 0
fi

[ -f "$CONFIG" ] || die "no backend config at $CONFIG (copy tables/local-backend.example.json there and edit it)"
cd "$REPO"
# The config is validated by the same code the gate runs (cortados_router/local_backend.py); exit 2 names the problem.
RESOLVED="$("$PYTHON" -B -m cortados_router.local_backend --config "$CONFIG" --print-config)" \
  || die "the backend config $CONFIG was refused; nothing was installed"
BACKEND="$(printf '%s' "$RESOLVED" | "$PYTHON" -I -c 'import json,sys; print(json.load(sys.stdin)["backend"])')"
[ "$BACKEND" = "vllm-mlx" ] || die "$CONFIG has backend \"$BACKEND\"; set it to \"vllm-mlx\" to install the MLX server"

# The venv's python, the guard and the model must be there before anything is scheduled (a LaunchAgent that cannot
# start would restart every ThrottleInterval and fill the log).
PATHS="$("$PYTHON" -B -m cortados_router.local_backend --config "$CONFIG" --install-paths)"
path_of() { printf '%s' "$PATHS" | "$PYTHON" -I -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
VPY="$(path_of python)"; GUARD="$(path_of guard)"; GUARD_SRC="$(path_of guard_source)"; MODEL_DIR="$(path_of model)"
[ -x "$VPY" ] || die "no python at $VPY (the venv the config names, with vllm-mlx 0.6.0 installed)"
[ -f "$GUARD_SRC" ] || die "no guard source at $GUARD_SRC"
[ -d "$MODEL_DIR" ] || die "no model directory at $MODEL_DIR (vllm_mlx.models.<model>.path in $CONFIG)"
# The guard's own check, run from the source: vllm-mlx version and pinned files, and the model's route.
"$VPY" -B "$GUARD_SRC" --check "$MODEL_DIR" || die "the prompt guard refused this venv or model; nothing was installed"
PLIST_XML="$("$PYTHON" -B -m cortados_router.local_backend --config "$CONFIG" --plist "$LOG_DIR")"

if [ "$DRY" = 1 ]; then
  echo "# would copy $GUARD_SRC to $GUARD"
  echo "# $PLIST"
  printf '%s\n' "$PLIST_XML"
  echo "# then: $LAUNCHCTL bootout gui/$(id -u)/$LABEL; $LAUNCHCTL bootstrap gui/$(id -u) $PLIST"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
install -m 0644 "$GUARD_SRC" "$GUARD.tmp" && mv "$GUARD.tmp" "$GUARD"
printf '%s\n' "$PLIST_XML" > "$PLIST.tmp" && mv "$PLIST.tmp" "$PLIST"
chmod 644 "$PLIST"
"$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
"$LAUNCHCTL" bootstrap "gui/$(id -u)" "$PLIST"
PORT="$(printf '%s' "$RESOLVED" | "$PYTHON" -I -c 'import json,sys; print(json.load(sys.stdin)["serve"]["port"])')"
cat <<EOF
install_mlx_backend: loaded $LABEL (log: $LOG_DIR/vllm-mlx.log). The model takes a minute to load; check with
  curl -s http://127.0.0.1:$PORT/health
Next (docs/LOCAL-RESOURCES.md "MLX backend"): unload Ollama's 27B (ollama stop Qwen27b_heretic:latest), restart the
local gate so it reads backend "vllm-mlx" from $CONFIG, and raise the Pro block's max_inflight on the provider host.
EOF
