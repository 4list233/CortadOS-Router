"""Compaction for long-living sessions across every routed model.

Three parts share the shapes in `events.py`, so a transcript is parsed once and every part reads the same records:
- checkpoint (phase 0): a deterministic handoff document a fresh session of any provider resumes from;
- provider pruning (phase 1): keep/drop decisions on past tool calls, logged in shadow first, with labels harvested
  from what later turns actually reused;
- S1 keep heads (phase 2): a separate S1 adapter that answers "keep the call" / "keep the result" from those labels.
"""
