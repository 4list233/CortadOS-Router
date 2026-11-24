#!/bin/bash
# Refresh tables/inventory/*: what every subscription's own CLI says it can run. Lists only; placing a model in a band is a routing-table edit.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)" || exit 1
cd "$ROOT/tables/inventory" || exit 1
export AGENT_CLI_CREDENTIAL_STORE=file PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
cursor-agent models 2>/dev/null | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | grep " - " > cursor.txt.new && mv cursor.txt.new cursor.txt
# agy (#144): through the one agy launch path, cortados_router/agy_isolation.py (`prepare`: CORTADOS_AGY_BIN must be an
# absolute path, never searched on PATH; the allowlisted env; a GEMINI_/GOOGLE_/AGY_/CLOUDSDK_ name in the router's env
# file refuses; the settings guard). This script never starts agy itself. agy 1.5.16 pads the id column with spaces,
# older versions used a tab: both become one tab, the format tables/benchmarks/build_tiers.py reads.
# Not configured (CORTADOS_AGY_BIN unset here and in the env file), refused or failed: the reason is on stderr and
# agy.txt is left as it is.
if agy_out=$(cd "$ROOT" && python3 -B -m cortados_router.agy_isolation models); then
  printf '%s\n' "$agy_out" | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | sed -E $'s/^([^ \t]+)[ \t]+/\\1\t/' \
    | grep -E $'^[a-z0-9][a-z0-9.-]*\t' > agy.txt.new && mv agy.txt.new agy.txt
else
  echo "agy: agy.txt left as it is" >&2
fi
ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' > ollama.txt
python3 -c "import json,os;print('\n'.join(m.get('slug') or m.get('id') for m in json.load(open(os.path.expanduser('~/.codex/models_cache.json')))['models']))" > codex.txt 2>/dev/null
wc -l *.txt
