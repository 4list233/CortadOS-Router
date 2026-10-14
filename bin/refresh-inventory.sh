#!/bin/bash
# Refresh tables/inventory/*: what every subscription's own CLI says it can run. Lists only; placing a model in a band is a routing-table edit.
set -u
cd "$(dirname "$0")/../tables/inventory" || exit 1
export AGENT_CLI_CREDENTIAL_STORE=file PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
cursor-agent models 2>/dev/null | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | grep " - " > cursor.txt.new && mv cursor.txt.new cursor.txt
(cd "$HOME/.cortados/usage-probe" && agy models 2>/dev/null) | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | grep "	" > agy.txt.new && mv agy.txt.new agy.txt
ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' > ollama.txt
python3 -c "import json,os;print('\n'.join(m.get('slug') or m.get('id') for m in json.load(open(os.path.expanduser('~/.codex/models_cache.json')))['models']))" > codex.txt 2>/dev/null
wc -l *.txt
