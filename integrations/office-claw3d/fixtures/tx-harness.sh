# Test harness for deploy-lib.sh's activation transaction (deploy.test.mjs). Never touches the host:
# a fake install under $FAKE, with real links and files, and daemons as state files recording the
# release they were started with. FAIL lists steps that fail once, FAIL_ALWAYS steps that always fail.
set -euo pipefail
. "$LIB"
D="$FAKE"
fail() {
  if [[ ",$FAIL_ALWAYS," == *",$1,"* ]]; then return 0; fi
  [[ ",$FAIL," == *",$1,"* && ! -e "$D/failed-$1" ]] || return 1
  touch "$D/failed-$1"
}
fs_readlink() { readlink "$1" 2>/dev/null || true; }
fs_exists() { [[ -e "$1" || -L "$1" ]]; }
fs_link() { if fail "link:${2##*/}"; then return 1; fi; ln -sfn "$1" "$2"; }
fs_rm() { rm -f "$1"; }
fs_backup() { if fail "backup:${1##*/}"; then return 1; fi; cp -p "$1" "$1.prev"; }
fs_promote() { if fail "promote:${2##*/}"; then return 1; fi; cp -p "$1" "$2.next" && mv -f "$2.next" "$2"; }
fs_restore() { mv -f "$1.prev" "$1"; }
daemon_loaded() { [[ -e "$D/daemons/$1" ]]; }
daemon_start() {
  if fail "start:$1"; then return 1; fi
  { readlink "$D/current" || echo none; readlink "$D/adapter" || echo none; cat "$D/bin/run-$1.sh" 2>/dev/null || echo none
    cat "$D/secrets/token" 2>/dev/null || echo none; } > "$D/daemons/$1"
  echo "start $1" >> "$D/log"
}
daemon_stop() { if fail "stop:$1"; then return 1; fi; rm -f "$D/daemons/$1"; echo "stop $1" >> "$D/log"; }
health_check() {
  if fail term; then kill -TERM $$; fi
  if fail health; then return 1; fi
  echo health >> "$D/log"
}
TX_LINKS=("$D/builds/new|$D/current|" "$D/adapters/new|$D/adapter|")
TX_FILES=("$D/stage/run-bridge.sh|$D/bin/run-bridge.sh" "$D/stage/run-studio.sh|$D/bin/run-studio.sh"
  "$D/stage/token|$D/secrets/token")
TX_LABELS=(bridge studio)
if [[ -n "${PRELUDE:-}" ]]; then eval "$PRELUDE"; fi
rc=0; tx_run || rc=$?
echo "rc=$rc"
