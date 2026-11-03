# CortadOS Router — completion checklist

Written 2026-11-03 from the repo docs, memory notes, open branches and Forest's goals. One line per ticket;
`[x]` done, `[~]` built and awaiting review/merge/deploy, `[ ]` open. **[ESCALATE]** = needs Forest's decision first.

## 1. Resource pool — every subscription and machine Forest owns is a lane
- [x] Claude accounts (2) on subscription pins, chosen by scarcity
- [x] Codex (codex-cli lane, `/status` quota parser)
- [x] Pro local models (Ollama behind the token gate on the Pro; qwen-27b, qwen-abliterated, qwythos-9b, phi-4)
- [ ] **Cursor lane**: Cursor subscription usage as a provider backend (`cursor-agent` headless), quota read like Codex's (ROUTER-DESIGN.md:167, PROVIDER-PLAN.md "Open")
- [ ] **Grok Bot lane** [ESCALATE]: Grok Bot (xAI desktop app, `/Applications/Grok Bot.app`, daemon in `~/.grokbot/`) has no API or MCP endpoint for routing today. First ticket: research whether it exposes a headless/CLI or local API, how its usage is metered, and whether the terms allow driving it; then decide lane vs. read-only usage tracking
- [ ] Codex multi-account (Phase B) and a dedicated logged-in `CODEX_HOME` for the provider
- [ ] Studio and Air local models as lanes (verify SSH targets and remote inference; LOCAL-RESOURCES.md:184)

## 2. Local hardware as a measured resource
- [ ] Hardware metric per machine: GPU/CPU busy %, free memory, model-resident memory, tokens/s per local model, collected like scarcity (periodic, cached, no vendor CLIs)
- [ ] Router treats spare local compute as headroom (cheap lane when idle, avoided when busy), same shape as subscription % left
- [ ] **Empower local models**: evaluate local lanes per task kind, add routing-table candidates, then turn on `auto_route` for them (off since the 2026-09-28 ruling, LOCAL-RESOURCES.md:130ff)
- [ ] Per-turn context-fit + quality checks for local lanes feed back into S1 labels (outcome loop)

## 3. Usage dashboard
- [ ] One view across all lanes: % left per Claude account / Codex / Cursor / Grok Bot, local hardware load, and routed calls per lane (today / 7 days), what S1 picked and why, refusals, child tasks
- [ ] Data sources: provider run records (`/v1/cortados/runs`), router.db, scarcity.json, child-tasks.jsonl, S1 status.json (alerts, last promotion), hardware metrics (2)
- [ ] Decide the surface [ESCALATE]: Claude Code pane (the `cortados-pulse` mod in `integrations/claude-mod/` is a start), a local web page on the Air, or both

## 4. S1 scorer and training
- [~] Evaluation never lost to the 07:00 deadline (branch `s1-eval-deadline`, round-2 review running)
- [~] Continuous training/labeling with scorer priority: pause trainer at +1 s scoring latency or <8 GB free; labeler floor 15% (branch `s1-continuous`; still to do: benchmark refresh once a day, review)
- [ ] Switch the Studio launchd job to continuous mode after review; watch status.json alerts for a week
- [ ] Use spare compute well: settings sweeps, an 8B base-model trial, regrade as labels grow
- [ ] OS-owned per-run boundary for the outcome loop (VM/container/throwaway user) [ESCALATE] (S1-OUTCOME-LOOP.md:139)
- [ ] Soft outcome labels; route-log tool lists (S1-c); manual training on the Pro (S1-f)
- [ ] P3s: iters-base/resume checkpoint parse can fail open; cap tests don't reach the caps

## 5. Child tasks, desktop app, Chrome
- [x] start_task / task_status / wait_task / list_tasks / cancel_task; connectors opt-in; permission defaults; redaction
- [~] wait_task fits the app's 60 s request timeout (branch `child-chrome-wait`)
- [~] chrome:true runs on the claude.ai login with Claude in Chrome; AppleScript/Playwright blocked (branch `child-chrome-wait`, review round 2 in progress)
- [ ] Claude in Chrome reaches a headless child while the app is open ("Browser extension is not connected"); Forest's 2-minute terminal test decides shared vs. one-client
- [ ] Live-check `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` routes every subagent (session.py:116, "INFERRED, NOT VERIFIED")
- [ ] Worktree-isolated subagents bypass routing; 10-minute cap on routed subagents
- [ ] Update `/cortados` command + orchestrator directive (connectors option, haiku permission note, chrome login mode)

## 6. Provider known limits
- [ ] Session rebuild instead of 409 after an endpoint restart or compaction
- [ ] Enforce `max_tokens`; token-by-token streaming; context window per backend model
- [ ] Pins for Codex clients on `/v1/responses`
- [ ] Image support

## 7. Ops and deploy
- [ ] Air: drop reserve flags in `accounts.json`; Air→Studio `router.db` sync (verify current state)
- [ ] Codex CLI version drift (pinned 0.163.0); re-copy the code-mode sidecar after reinstalls
- [ ] Provider log rotation on the Air; remove the Air's leftover HF cache and MLX venv
- [ ] Decide old branches [ESCALATE]: `fix/teacher-identity`, `routed-subagent-auth`, `plan/subagent-mcp`
- [ ] Connector auth Forest must do: Apollo, Notion; revoke the disabled API keys in the Console

## 8. Docs and hygiene
- [ ] README test counts, Status and Known limits (still mention `api/` pins); PROVIDER-PLAN S1-f window text
- [ ] Clean ~22 merged worktrees; untracked `cortados-pulse/tsconfig.json`
