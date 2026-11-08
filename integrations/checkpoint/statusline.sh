#!/bin/sh
# Claude Code statusLine command (template). Reads the status JSON on stdin, keeps it under $CORTADOS_HOME/checkpoint/,
# appends the used-token count to <session>.history when it changed (an approximation of per-turn samples for the
# velocity rule: the status line refreshes on assistant messages), and prints the checkpoint level. Files are private (umask 077): the JSON names paths.
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
hist="$dir/$sid.history"
fresh=""
[ -f "$hist" ] || fresh="--fresh"
cd "$REPO"
# check reads the history BEFORE this sample is appended (history = earlier turn ends)
python3 -B -m cortados_router.compaction.checkpoint check --status-file "$tmp" --history-file "$hist" $fresh \
  ${CORTADOS_CACHE_TTL_CLASS:+--cache-ttl-class "$CORTADOS_CACHE_TTL_CLASS"} \
  ${CORTADOS_ROUTED_MODEL:+--model "$CORTADOS_ROUTED_MODEL"} || echo "checkpoint: status unreadable"
python3 -c 'import json,sys
cw=json.load(open(sys.argv[1])).get("context_window") or {}
cu=cw.get("current_usage") or {}
n=sum(cu.get(k) or 0 for k in ("input_tokens","cache_creation_input_tokens","cache_read_input_tokens")) or cw.get("total_input_tokens")
last=None
try: last=open(sys.argv[2]).read().split()[-1]
except (OSError, IndexError): pass
if n and str(n)!=last: open(sys.argv[2],"a").write("%d\n"%n)' "$tmp" "$hist"
mv "$tmp" "$cur"
