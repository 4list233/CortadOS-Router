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

S1 scores the prompt → band B (stored once per task in `scores.jsonl`, so a retry keeps the same ladder). The route
for band b is the router's own choice when b is the floor:
`core.resolve(zero_decision(work_kind), execution=True, minimum_band=b, allowed_routes=served)`. The zero decision
carries only the task's work kind, so the band is exactly b and the work-kind preference still applies. The ladder
spans R0–R3: R4 is the table's escalation-only ceiling and `core.required_index` gives it no capability floor, so a
floored R4 resolves to an R1-level model (review finding). Each run row records the rung's required index. No second
model list exists (ENG-03).

### Containment (untrusted: mined text, generated specs, fixture code)

- **Checks and the runner's git calls** run under macOS Seatbelt (`s1/outcome/containment.py`): no network, file
  writes only inside the workspace, minimal environment with HOME/TMPDIR inside it. Missing `sandbox-exec` refuses
  the check instead of running it unconfined. Git runs with hooks and fsmonitor off.
- **Claude lane:** `claude -p --model <m> --tools Bash,Read,Edit,Write,Glob,Grep --permission-mode acceptEdits` with
  Claude Code's OS sandbox for Bash and its children (`sandbox.enabled`, `failIfUnavailable`,
  `allowUnsandboxedCommands=false`, empty network allowlist), no settings files, hooks off, only the mock MCP server.
  Edit/Write are approved only inside the working directory; in `-p` anything needing approval is denied.
- **Codex lane:** `codex exec -s workspace-write` (Seatbelt, network pinned off), with the provider's isolation rules
  reused from `provider/codex_isolation.py`: every MCP server of the home disabled by name, hooks/apps/plugins/
  memories/browser/computer use off, provider pinned to the OpenAI subscription, project docs off. Generation is
  tool-free (every built-in tool disabled, read-only sandbox).
- The mock-call journal lives in a runner-owned folder outside the workspace, so neither a fixture nor an agent can
  forge outward-call evidence. Fixtures may not create `.git`, `.claude`, `.codex` or `.tmp`.
- The child environment drops the provider's stripped variables (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_API_KEY`, and for Codex the OpenAI key and base URL).
- A timeout kills the CLI's whole process group.

### Concurrency and recovery

- One loop per data home (`loop.lock`, an exclusive lock across processes); within it, a task is claimed before any
  paid work, so two workers never run the same task.
- Every run is announced in `attempts.jsonl` before it starts; a crash mid-run leaves a visible spent attempt. A
  sandboxed run has no outside effect, so it is safe to repeat.
- A torn last line is moved to `<store>.torn` under the writer lock before the next append.

### Outcome band and labels

- A band passes when its check passes (if any) and the judge accepts the result against the rubric.
- Outcome band = the lowest band in the ladder that passed. None passed → `above` (recorded, not a label).
- Under-routing on a task = S1's band B is below the outcome band.
- Trace teacher: given the prompt, the run trace (tool calls, files touched, check result) of the lowest passing run,
  it answers the 12 routing questions. These rows train S1; the prompt alone is the student's input.

### Quota rule (D4)

Every model call (generation, each run, each judgement) goes through `quota.admit(lane, model)`. It checks every
window that applies to that model (the lane's general windows and the model's own pool), computes the time to reset
from the window's absolute reset time, and refuses when any window is at projected or used ≥ 95 %, under 1 hour from
reset, incomplete, or the lane's reading is stale (> 1 h) or refusing. Admission is atomic, and each call admitted on
one reading adds an assumed 1 % to that pool until the next sample (an unmeasured assumption, recorded as such), so
concurrent workers cannot overshoot between samples.

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
| AC-04 | The ladder for B gives routes for B−1, B, B+1 within R0–R3, each from `core.resolve` with `minimum_band`; the required index rises up the ladder with the real tables. |
| AC-05 | A run writes the fixture into a fresh git repo, runs the agent there, runs the check there, and records pass/fail and the trace summary; the workspace is removed afterwards. |
| AC-06 | The child environment never carries `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`. |
| AC-07 | Mock outward tools record each call and return success; the run's trace lists them. |
| AC-08 | Outcome band = lowest passing band; none passing = `above`. |
| AC-09 | The quota gate stops a pool at projected ≥ 95 %, within 1 h of reset, or when the reading is unknown. |
| AC-10 | Every stage resumes after a crash without duplicating rows. |
| AC-11 | Holdout tasks (by `split_of`) are judged and trace-labelled by Fable 5.1 only. |
| AC-12 | A generated check cannot write outside its workspace or open a network connection; a Claude run starts only with its OS sandbox; a Codex run disables every MCP server and integration of the home. |
| AC-13 | Outward-call evidence cannot be forged from the workspace; fixtures cannot create reserved directories. |
| AC-14 | A claimed task is not run twice; one loop per data home; an append after a torn line survives. |

## Known containment limit (Codex review 2026-10-24, [ESCALATE] for Forest)

Seatbelt is a syscall-policy sandbox, not an OS-owned process boundary. A child can `exec` a new process with an
empty environment, its own session (`setsid`), `cwd=/` and closed descriptors; that process then carries no run
token, is in no tracked process group, and holds no workspace file, so all three userspace cleanup mechanisms
(kill-by-token, `killpg`, workspace sweep) miss it. It cannot be reliably killed from userspace.

What it still **cannot** do, because Seatbelt binds every descendant: write outside the workspace, open any network
connection, send Apple Events, use LaunchServices, or signal another process (all verified live 2026-10-24). So the
residual risk of a fully-detached child is **lingering compute** (CPU/RAM of an orphan), never data exfiltration, an
outside write, or a real outward effect. The task prompts are generated by Codex from Forest's own transcripts, not
adversarial input, so this is defence-in-depth, not a live exploit path.

The robust fix is an OS-owned boundary (a disposable VM or container per run, or a dedicated throwaway UID whose
processes are killed as a set). That is a scope decision, not a patch. Until then the loop ships with the Seatbelt
boundary and this accepted residual; a periodic `pkill`-style reaper cannot help, because an env-cleared orphan is
indistinguishable from unrelated processes.

## Outcome → weights (RT-S1-OUTCOME-002, Forest 2026-10-25)

The loop produces the data; this turns it into trained weights, reusing the existing trainer (`s1/nightly.sh`) and its
one promoter — no second training or promotion path (ENG-03).

| ID | Decision |
|---|---|
| [FIXED] W1 | `build_sft.py` merges outcome examples with the teacher labels (outcomes **+** existing labels), one example per train-split outcome from its trace-teacher labels; `unit_kind` fixed `interactive`; an outcome where S1 under-routed (its band below the outcome band) counts `UPWEIGHT_HIGH` times. |
| [FIXED] W2 | `evaluate.py` grades the candidate's band against the **outcome band** on the holdout outcomes (`grade_outcome`), and the promote rule gains a no-regression-on-outcome-under-routing clause whenever the promoted model has a fresh outcome bar (cached in parallel to the teacher bar). |
| [FIXED] W3 | **Hold-first (checkpoint-bound):** the first candidate that actually *trains* on outcomes is held for review (`report.promote` forced false, `promote_recommended` kept). Each run records its own provenance (`$RUN/outcome-provenance.json`, copied from the build's `sft-provenance.json` by `nightly.sh` *only* when a segment trains, preserved across no-op resumes); `evaluate` reads that run's provenance for hold-first, not the latest dataset build, so a completed teacher-only checkpoint re-evaluated after the count later crosses 150 is never mislabelled. `python3 -B -m outcome.train_gate --arm --report <run>/report.json` requires a reviewed report whose candidate trained on outcomes; after that, later outcome-trained candidates auto-promote on no-regression. |
| [FIXED] W4 | **Fire threshold:** `outcome.train_gate` reports fire/hold at `MIN_TRAIN` = 150 train-split labelled outcomes (`CORTADOS_OUTCOME_MIN_TRAIN`). The gate counts and arms only; it does not train or promote. |

Deploy note: the trainer, its MLX venv and the base model live in the Studio's training deployment; the outcome-aware
`build_sft.py`/`evaluate.py` must be reconciled into that deployment (diff first — it is a separate repo line) before a
real train fires. The actual fine-tune stays gated behind W4 (data) and W3 (review).

## Not in v1

Multi-account pools. Remote CLI hosts. The Pro's manual training commands (S1-f). Sampling the trace teacher k times
for soft outcome labels (today each outcome is one labelling; the outcome band carries the orthogonal signal).
