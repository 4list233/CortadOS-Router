<!-- PURPOSE: Explain the actual scorer context flow and the limits of difficulty estimates.
     CONTRACT: RT-MCP-001; no scoring, model or permission change.
     DEPENDS: route_task.py, core.py, locally promoted S1 aggregate evaluation. -->
# S1 context and difficulty

Claude remains the planner. It gathers project context, defines one worker assignment, and sends
the relevant facts through MCP. S1 estimates the assignment's difficulty. The existing resolver
combines that estimate with capability requirements, available native models, context fit and fresh
subscription scarcity. The selected Codex model then works in its own native harness; Claude reviews
the result. S1 does not itself run the implementation or read quota.

| Boundary | What it receives |
|---|---|
| S1 scoring | Exactly `selection.routing_brief`, when supplied; otherwise `task + "\n\nCONTEXT:\n" + context`. Maximum 6,000 Unicode characters; larger inputs refuse rather than silently truncate. |
| Codex worker | Full `task` and `context`, separately assembled with a reference-data label. Maximum 65,536 UTF-8 bytes in the captured handoff. Later repository reads and tool output belong to the native worker's context. |
| Connectors | Only explicitly registered and granted MCP tools. Context text cannot grant permissions. |

S1 has no persistent conversation in this service. It does not automatically receive Claude's
conversation, repository files, Grounds memory or connector results. The 6,000-character service
boundary is distinct from the base model's theoretical context window. A long-context model would
still need Claude to supply useful evidence through this interface.

The scorer is a trained Qwen3 4B model. It classifies twelve task properties, including steps,
ambiguity, blast radius, context size and novelty. The difficulty sum uses steps, ambiguity, blast
radius and novelty; hard routing rules and capability bands then constrain selection. Low-confidence
feature predictions have the existing upward adjustment, but this does not eliminate mistakes.

At inspection on 2026-10-17, the running endpoint reported the adapter promoted at **2026-10-16 05:40**.
Its local promotion report contained **598 holdout examples**: **65.2% exact band agreement**,
**6.7% under-routing**, **28.1% over-routing**, and a **2,662 ms median scoring latency**. These are
agreement measures against the evaluation labels, not guaranteed success rates on new coding tasks.
Source: the aggregate fields in `train/current.json` and the referenced `report.json` in the local
CortadOS training workspace. The repository's older `s1/current.example.json` is a historical example.

Give S1 the facts that change difficulty: the goal, affected components, dependencies, uncertainty,
side effects, constraints and verification requirements. For example:

> Add one parser regression test for the documented invalid_reset error. The failing input and
> expected exception are known. One existing test module changes; runtime code and public APIs stay
> unchanged. No migrations, network calls or production effects. Run the parser suite and report its result.

For a risky task, Claude can supply `constraints.min_band`, restrict `constraints.allowed_models`, or
select an exact model. A vague brief such as “fix the bug” gives S1 too little evidence to distinguish
a one-file repair from a cross-service investigation. Improving the brief is the first remedy;
retraining or changing the scoring model is a separate evaluated change.
