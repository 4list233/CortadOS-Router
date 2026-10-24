# S1 outcome loop

contract_id: RT-S1-OUTCOME-001 · version 1 · owner Forest · status: approved for build 2026-10-24
implements: `docs/PROVIDER-PLAN.md` S1 track items S1-a (teacher rule), S1-b (task context), S1-d (outcome
benchmark), S1-e (surplus quota).

## Outcome

S1 learns from what happens when a task is actually carried out, not from a teacher's guess at its difficulty.
Tasks come from Forest's current work (Claude Code, Codex and Cowork transcripts), not from the stopped Hermes fleet.

## Forest's decisions (2026-10-24)

| ID | Decision |
|---|---|
| [FIXED] D1 | Built on `main`'s `s1/` (reviewed line); deployed to the Studio. The Studio's unreviewed teacher release is not used by this loop. |
| [FIXED] D2 | Seeds may be read from Claude Code (`~/.claude/projects`), Codex (`~/.codex/sessions`) and Cowork (`local-agent-mode-sessions/**/audit.jsonl`) transcripts on both Macs, client work included. They stay in the local data home and are never committed or published. |
| [FIXED] D3 | Labels: a teacher labels the 12 routing features after seeing the actual run (trace teacher); the outcome band (lowest band whose run passed) grades S1 and gates promotion. |
| [FIXED] D4 | Quota: every pool may be used until its projected use at reset reaches 95 %, never in the last hour before that pool's reset (S1-e). |
| [FIXED] D5 | Holdout judge and trace teacher: Claude Fable 5.1. Outward effects run against mock tools only. |
| [FIXED] D6 | A second teacher's labels count only after it clears the agreement bar (S1-a). On 2026-10-21 `gpt-6.1-sol` and `gpt-6-astra` did not (ambiguity / urgency / blast-radius agreement 0.63–0.72 against Claude's self-agreement 0.84–0.91; outward recall 5/8). Train-split trace labels therefore use a Claude teacher in v1. |

## Pipeline

```
mine ──► seeds.jsonl ──► generate (Codex) ──► tasks.jsonl ──► run ladder ──► runs.jsonl ──► judge ──► outcomes.jsonl
                                                               │  S1 band B; routes for B−1, B, B+1
                                                               │  each in a disposable git workspace
                                                               └─ check command + mock outward tools
```

| Stage | Module | Reads | Writes (all under the data home `s1/data/outcome/`) |
|---|---|---|---|
| mine | `s1/outcome/mine.py` | transcripts (read-only) | `seeds.jsonl`: one task statement per session, with the tool names the session used |
| generate | `s1/outcome/generate.py` | seeds | `tasks.jsonl`: runnable task specs (schema below) |
| run | `s1/outcome/run.py` | tasks, S1, router | `runs.jsonl`: one row per (task, band) with route, trace summary, check result |
| judge | `s1/outcome/judge.py` | runs | `outcomes.jsonl`: per task: judge verdict per band, outcome band, trace-teacher feature labels |
| loop | `s1/outcome/loop.py` | quota readings | drives the stages until stopped or every pool is at its stop rule |

Store classes (ENG-09): transcripts are external **Evidence**, never written. `seeds`, `tasks`, `runs`, `outcomes` are
append-only **Canonical** stores owned by their stage; a row is never rewritten. Each stage is resume-safe: it skips
ids already present downstream. A crash mid-row leaves at most one unterminated last line, which readers ignore.

### Task spec (closed schema; unknown fields refuse)

`{id, seed_id, prompt, category, work_kind, fixture: {files: {relative_path: text}}, check: {argv: [str], timeout_s},
rubric: [str], outward_tools: [str]}`

- `fixture` files are written into a fresh git repo; paths must be relative and stay inside it.
- `check.argv` runs inside the workspace after the agent finishes; exit 0 = pass. It may be empty for work with no
  executable check (writing, research); then the judge alone decides.
- `outward_tools` are served by the mock MCP server (`s1/outcome/mock_tools.py`), which records every call and returns
  a fixed success. A task whose prompt needs an outward effect must list it; real network stays off.

### Band ladder

S1 scores the prompt → band B. The route for band b is the router's own choice when b is the floor:
`core.resolve(zero_decision(work_kind), execution=True, minimum_band=b, allowed_routes=served)`. The zero decision
carries only the task's work kind, so the band is exactly b and the work-kind preference still applies. Bands outside
R0–R4 are dropped (B−1 at R0, B+1 at R4). No second model list exists (ENG-03).

### Runs

- Claude lane: `claude -p --model <m> --output-format json --permission-mode acceptEdits` with built-in web tools
  disallowed and only the mock MCP server configured (`--strict-mcp-config`).
- Codex lane: `codex exec -m <m> -s workspace-write -C <workspace> --json` (network off in that sandbox), mock MCP
  server via `-c mcp_servers…`.
- The child environment drops `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` (no loop back into the
  provider; no API-key billing), as the provider bridge does.

### Outcome band and labels

- A band passes when its check passes (if any) and the judge accepts the result against the rubric.
- Outcome band = the lowest band in the ladder that passed. None passed → `above` (recorded, not a label).
- Under-routing on a task = S1's band B is below the outcome band.
- Trace teacher: given the prompt, the run trace (tool calls, files touched, check result) of the lowest passing run,
  it answers the 12 routing questions. These rows train S1; the prompt alone is the student's input.

### Quota rule (D4)

Before each model call the loop reads `scarcity.lane_scarcity()` for that lane. It stops using a pool when its
projected use at reset ≥ 95 % or its reset is under 1 hour away. Unknown readings stop the pool (never assume room).

## Limits (ENG-10)

| Limit | Value | Reason · too small · too large |
|---|---|---|
| Seeds per session | 1 (first user request) | The session's task; later turns are follow-ups. Too few: misses multi-task sessions. Too many: follow-ups score as tasks. |
| Seed text | 6,000 chars | S1's request clip (`core.decide_s1`). Smaller loses the task; larger is clipped by S1 anyway. |
| Generation batch | 8 seeds per Codex call | Keeps one call's output well under the CLI's reply size. Smaller: more calls. Larger: truncated JSON. |
| Run timeout | 900 s per (task, band) | A coding task by a strong model finishes in minutes. Smaller: false failures. Larger: a stuck run holds the loop. |
| Check timeout | from the spec, ≤ 300 s | Tests for a fixture repo. Larger hides a hung check. |
| Fixture size | ≤ 200 kB, ≤ 40 files | Small repos keep runs fast and reviewable. Larger makes generation slow and runs costly. |

## Acceptance cases

| AC | Behaviour |
|---|---|
| AC-01 | `mine` extracts the first real user request from each Claude Code / Cowork / Codex transcript, skipping tool results, system and injected reminder text; unreadable lines are counted, not fatal. |
| AC-02 | `mine` is idempotent: rerunning adds no duplicate seed ids. |
| AC-03 | A task spec with an unknown field, an absolute or escaping fixture path, or an oversize fixture is refused with a named reason. |
| AC-04 | The ladder for B gives routes for B−1, B, B+1 within R0–R4, each from `core.resolve` with `minimum_band`. |
| AC-05 | A run writes the fixture into a fresh git repo, runs the agent there, runs the check there, and records pass/fail and the trace summary; the workspace is removed afterwards. |
| AC-06 | The child environment never carries `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`. |
| AC-07 | Mock outward tools record each call and return success; the run's trace lists them. |
| AC-08 | Outcome band = lowest passing band; none passing = `above`. |
| AC-09 | The quota gate stops a pool at projected ≥ 95 %, within 1 h of reset, or when the reading is unknown. |
| AC-10 | Every stage resumes after a crash without duplicating rows. |
| AC-11 | Holdout tasks (by `split_of`) are judged and trace-labelled by Fable 5.1 only. |

## Not in v1

Training and promotion on outcome data (next: `evaluate.py` gains an outcome-band grade; `build_sft.py` reads
`outcomes.jsonl`). Multi-account pools. Remote CLI hosts. The Pro's manual training commands (S1-f).
