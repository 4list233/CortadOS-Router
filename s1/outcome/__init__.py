"""S1 outcome loop (docs/S1-OUTCOME-LOOP.md, RT-S1-OUTCOME-001): tasks from real work, run at band -1/0/+1, judged.

Run from s1/: `python3 -B -m outcome.loop`. The repository root goes on sys.path so the provider's shared rules
(injected-text stripping, served routes) are imported from their one owner, not copied.

Backends (2026-11-03): with CORTADOS_PROVIDER_URL set (token from CORTADOS_PROVIDER_TOKEN or
CORTADOS_PROVIDER_TOKEN_FILE, default ~/.config/cortados/provider-token), judge (sub/claude-fable-5-1) and generate
(sub/<CORTADOS_OUTCOME_GENERATOR>) run through the cortados provider, which picks the account (claude, claude#b, codex)
by scarcity; their quota door is the provider's GET /resources usage reading, per account and weekly window
(outcome/provider.py): held below 5 % left, on a stale reading, or when /resources is unreachable (fail closed). CORTADOS_OUTCOME_BACKEND=local restores the local CLI path and the local usage reading (quota.py). In provider
mode every ladder band, Claude or Codex, runs as a sandboxed Claude Code session pointed at the provider
(ANTHROPIC_BASE_URL/AUTH_TOKEN, `--model sub/<band model>`, run-id + reveal headers); the run row records the model and
lane that served, and the band's door is provider.admit on that lane. Bash keeps an empty network allowlist; only the
CLI process reaches the provider. In local mode quota.py treats Claude's dropped 5-hour
panel as absent (weekly window required) and lets an unknown-ownership model ride the weekly window while the
per-model panel is down (CORTADOS_OUTCOME_STRICT_PANEL=1 restores the old refusal).
"""
import sys
from pathlib import Path

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
