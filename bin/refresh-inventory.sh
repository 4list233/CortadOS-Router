#!/bin/bash
# Refresh tables/inventory/*: what every subscription's own CLI says it can run. Lists only; placing a model in a band is a routing-table edit.
set -u
cd "$(dirname "$0")/../tables/inventory" || exit 1
export AGENT_CLI_CREDENTIAL_STORE=file PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
cursor-agent models 2>/dev/null | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | grep " - " > cursor.txt.new && mv cursor.txt.new cursor.txt
# agy (#144): the configured absolute binary only (CORTADOS_AGY_BIN), on the same allowlisted env as the quota probe
# (cortados_router/agy_isolation.py: no GEMINI_/GOOGLE_/AGY_ variable reaches it). agy 1.5.16 pads the id column with
# spaces, older versions used a tab: both become one tab, the format tables/benchmarks/build_tiers.py reads.
if [ -n "${CORTADOS_AGY_BIN:-}" ]; then
  (mkdir -p "$HOME/.cortados/usage-probe" && cd "$HOME/.cortados/usage-probe" && \
    env -i HOME="$HOME" USER="${USER:-}" LOGNAME="${LOGNAME:-}" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}" \
      PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$CORTADOS_AGY_BIN" models 2>/dev/null) \
    | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | sed -E $'s/^([^ \t]+)[ \t]+/\\1\t/' | grep -E $'^[a-z0-9][a-z0-9.-]*\t' > agy.txt.new && mv agy.txt.new agy.txt
else
  echo "agy: CORTADOS_AGY_BIN not set (absolute path of agy); agy.txt left as it is" >&2
fi
ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' > ollama.txt
python3 -c "import json,os;print('\n'.join(m.get('slug') or m.get('id') for m in json.load(open(os.path.expanduser('~/.codex/models_cache.json')))['models']))" > codex.txt 2>/dev/null
wc -l *.txt
