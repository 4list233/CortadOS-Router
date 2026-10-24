"""S1 outcome loop (docs/S1-OUTCOME-LOOP.md, RT-S1-OUTCOME-001): tasks from real work, run at band -1/0/+1, judged.

Run from s1/: `python3 -B -m outcome.loop`. The repository root goes on sys.path so the provider's shared rules
(injected-text stripping, served routes) are imported from their one owner, not copied.
"""
import sys
from pathlib import Path

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
