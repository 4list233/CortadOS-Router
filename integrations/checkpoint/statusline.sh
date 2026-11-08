#!/bin/sh
# Claude Code statusLine command (template). Reads the status JSON on stdin, keeps it (and the previous sample) under
# $CORTADOS_HOME/checkpoint/, and prints the checkpoint level. Files are private (umask 077): the JSON names paths.
set -eu
umask 077
: "${CORTADOS_HOME:?set CORTADOS_HOME}"
: "${REPO:?set REPO to the CortadOS-Router checkout}"
dir="$CORTADOS_HOME/checkpoint"
mkdir -p "$dir"
tmp="$dir/.incoming.$$"
cat > "$tmp"
sid=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("session_id") or "unknown")' "$tmp")
cur="$dir/$sid.status.json"
[ -f "$cur" ] && mv "$cur" "$dir/$sid.prev.json"
mv "$tmp" "$cur"
cd "$REPO" && python3 -B -m cortados_router.compaction.checkpoint check --status-file "$cur" \
  --previous-status-file "$dir/$sid.prev.json" || echo "checkpoint: status unreadable"
