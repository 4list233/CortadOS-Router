# Checkpoint and compaction policy for long coding-agent sessions (as of 2026-11-08)

## Recommended values

W = model context window. `min()` means the smaller value applies.

| Knob | Value | Why (source) |
|---|---|---|
| Tool-result pruning (drop old outputs that can be re-run) | Starts at min(100k, 40% W). Keep the last 4 tool results. Prune only if it frees ≥20k tokens | 100k / keep 3 is Anthropic's `clear_tool_uses` default. `clear_at_least` exists so a cache break pays for itself [A4]. Anthropic calls clearing the "lightest-touch" compaction [A6] |
| Warn | min(55% W, 150k) | 150k is Anthropic's server-compaction default trigger [A5]. Quality loss is measurable well below the window limit [R1, R2] |
| Checkpoint (agent writes its own handoff) | min(70% W, 200k) | Results above 256k are weak and uneven: MRCR at 512k–1M ranged 32–76% on recent models (secondary sources) [R4]. Codex's old effective window was 258k [O3] |
| Hard / forced restart | min(85% W, 300k, price cliff − 10k) | Leaves room for the handoff on 200k windows. The price cliffs are OpenAI API >272k (2× input) [O1] and Haiku 5.5 API >100k (5×) [A2] |
| Which limit applies | 200k W: the percentages (110k / 140k / 170k). 1M W (all Claude 5.x): the absolute caps (150k / 200k / 300k). Codex Sol (500k default in Codex): absolute caps, and 262k hard if billed through the API | — |
| Velocity rule | Checkpoint now if current + 2 × (average growth of the last 3 turns) ≥ hard. Also if one turn adds ≥10% of W | A single large read can jump past the threshold between checks (my rule, inferred) |
| Idle-boundary rule | At ≥ warn, checkpoint at the end of a turn instead of mid-tool-chain. If idle longer than the cache TTL and ≥ warn, checkpoint before resuming | Cold re-reads are expensive: TTL is 5 min (API key) or 1 h (subscription) on Claude [A3], 30 min default on OpenAI [O2]. Claude Code's `idleCompaction` follows the same logic [A3] |
| Handoff deadline | 4 min after the current tool call ends. Force at 6 min | About 8k output tokens with thinking takes about 2–3 min (estimate) |
| Total handoff budget | ≤16k tokens, about 8% of the 200k checkpoint | Claude Code re-injects ≤5 files at ≤5k each, and ≤25k of skills [A7]. Codex keeps about 20k of user messages [O4] |
| Goal | 400 tok | One paragraph plus the acceptance check |
| Standing rules (user instructions, verbatim) | 1,500 tok | Codex keeps user messages verbatim [O4] |
| Decisions plus reasons | 1,500 tok | Anthropic says to keep architectural decisions [A6] |
| In-flight work and next steps | 2,000 tok | Keep unresolved bugs and implementation state [A6] |
| Files touched | 800 tok (path plus one line each, ≤40 files) | The agent can re-read files; only paths are needed [A7, M1] |
| Ranked transcript excerpts | 6,000 tok | Subagent summaries of 1–2k tokens work [A6]; 6k leaves room for evidence |
| Recency pin | Last 3 user turns verbatim plus the last 2 tool calls raw, ≤4,000 tok | Manus keeps the most recent tool calls raw so the model's style and behaviour carry over [M1] |
| Tool-result truncation | 8k tokens per result (head plus tail). Write the full output to a file and keep the path | Codex's default is about 12k (unverified) and its tuning guides use 8k [O4]. Manus treats this as "reversible compaction" [M1] |
| Circuit breaker | 2 forced restarts in 30 min, or any fresh session that opens above 50% of the checkpoint threshold → stop and escalate | A handoff that is too big, or a loop, means restarts stop helping |
| Time-based refresh | No hard timer. Optionally checkpoint at task boundaries after 4 h wall-clock if ≥ warn | No primary evidence for a time-based effect. The "35-minute" claim is unverified [R3] |

**Characters versus tokens.** Claude's current tokenizer averages about 2.5 characters per token (1M tokens ≈ 2.5M characters) [A1]. The old 32k-character handoff cap was therefore about 12.8k tokens, not 8k. Set caps in tokens, or use 2.5 characters per token for Claude and about 4 for GPT.

## 1. Models

- **Claude:** `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` and `claude-fable-5-1` all have a **1M context window and 128K max output** (300K output on Batch with a beta header) [A1].
  - Input / output prices: Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Fable 5.1 $10/$50, Haiku 5.5 from $0.10/$0.50 [A1].
  - Long-context pricing: no premium for most models, but Haiku 5.5 costs 5× on prompts over 100k [A2].
- **OpenAI (Codex):** the recommended Codex models are `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6.1-terra` and `gpt-6-luna`. The docs do not name a default; their example config uses `gpt-6.1-sol` [O5].
  - GPT-6.1 Sol: 1.05M context window, 922k max input, 128k output [O1].
  - Codex and ChatGPT Work default to a **500k window** for Sol [O5]. On subscription there is no long-context surcharge. On the API, prompts over 272k pay 2× input and 1.5× output [O1].
- **Codex history:** GPT-5.6 in Codex was capped to about 258k effective (third-party source) [O3].

## 2. Built-in compaction today

- **Claude Code**
  - Native 1M models auto-compact at about **967k**. 200k sessions compact at the limit [A7].
  - The threshold can be set with `/autocompact`, `autoCompactWindow`, or `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (100k–1M) [A7].
  - The status line exposes `context_window.used_percentage`, `remaining_percentage`, `context_window_size`, `current_usage` and `exceeds_200k_tokens`. `used_percentage` counts input tokens only and is measured against the full window, not the compaction window [A8].
  - The `PreCompact` hook (matcher `manual`/`auto`) can block compaction with exit code 2, and its stdout becomes summary instructions. `PostCompact` is for side effects only [A9].
  - After compaction, Claude Code re-reads up to 5 recent files (≤5k each) [A7].
- **Claude API**
  - Threshold compaction (`compact_20260112`): default trigger 150k, minimum 50k, with `pause_after_compaction` [A5].
  - On-demand compaction (`compact-2026-09-04`) can keep recent turns and run in the background [A5].
- **Codex CLI**
  - `model_auto_compact_token_limit` is effectively clamped to **≤90% of the window** (since v0.100.0, per source and community reports) [O3, O4]. When unset, the per-model default applies.
  - OpenAI models compact remotely through `/responses/compact`, which returns an encrypted compaction item. User messages are kept and older items are dropped [O4].
  - `tool_output_token_limit` caps tool outputs stored in history [O4].

## 3. Quality versus length

- **Chroma (18 models):** quality drops as input grows, even on simple tasks. A focused prompt of about 300 tokens beat the full 113k one on LongMemEval for every model family. Distractors make it worse [R1].
- **SambaNova (ICLR 2026):** successful SWE-bench agent trajectories mostly stay **under 20k tokens**, and longer contexts correlate with lower success. Single-shot 64–128k inputs collapsed for small models [R2].
- **MRCR v2 at 512k–1M** (all secondary sources): Opus 4.6 about 76%, GPT-5.5 74%, Opus 4.7 32% [R4]. I could not verify any Claude 5.x or GPT-6 long-context score.
- **Anthropic:** "response quality degrades as a conversation grows" [A5], and context has "diminishing marginal returns" [A6].
- **Conclusion:** the quality limit is an absolute token count, not a percentage. Treat about 150–200k as the working ceiling for coding, and anything above 300k as risky on any window.

## 4. Cost

Cache read prices:

| Model | Cache read / MTok | Cache write / MTok |
|---|---|---|
| Opus 5.5 | $0.20 (5%) | $5 (5 min) / $8 (1 h) [A2] |
| Sonnet 5.5 | $0.10 (5%) | — |
| Fable 5.1 | $0.25 (2.5%) | — |
| GPT-6.1 Sol | $0.10 (0.05×) | 1.25× (30 min) / 2.5× (12 h) [O2] |

Worked example on Opus 5.5:
- **Carrying context:** keeping 500k warm costs $0.10 per request, and about 30 tool-calls per hour adds up to about $3/h. At 150k it is about $0.9/h.
- **Restarting:** a restart writes about 30–40k fresh tokens, about $0.20–0.30 once.
- **Cold resume:** resuming a 500k context after the TTL expires costs $2.50–4.

So a restart pays for itself within about 3 turns once context is above 300k. Cost and quality both point the same way: restart sooner, and never resume a big context cold.

## 5. Handoff size

No controlled study gives an optimal handoff size; this is **unverified**. What vendors do:
- Anthropic: subagents return condensed summaries of 1–2k tokens [A6].
- Claude Code: re-injects ≤25k of files and ≤25k of skills [A7].
- Codex: keeps about 20k of user messages [O4].
- Manus: keeps the last few tool calls raw and stores file paths rather than contents [M1].

I did not find Cognition, Factory or Letta guidance with numbers. The 16k budget in the table is my synthesis from these practices.

## Sources

- [A1] Claude models overview — https://platform.claude.com/docs/en/about-claude/models/overview
- [A2] Claude pricing — https://platform.claude.com/docs/en/about-claude/pricing
- [A3] Claude Code prompt caching — https://code.claude.com/docs/en/prompt-caching
- [A4] Context editing — https://platform.claude.com/docs/en/build-with-claude/context-editing
- [A5] Compaction overview / threshold — https://platform.claude.com/docs/en/build-with-claude/compaction , …/compaction-threshold
- [A6] Effective context engineering — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- [A7] Claude Code model config / context window — https://code.claude.com/docs/en/model-config , https://code.claude.com/docs/en/context-window
- [A8] Status line — https://code.claude.com/docs/en/statusline
- [A9] Hooks — https://code.claude.com/docs/en/hooks
- [O1] GPT-6.1 Sol model page — https://developers.openai.com/api/docs/models/gpt-6.1-sol
- [O2] OpenAI prompt caching — https://developers.openai.com/api/docs/guides/prompt-caching
- [O3] Codex window gap (third-party) — https://codex.danielvaughan.com/2026/07/20/context-window-gap-codex-cli-gpt56-advertised-vs-effective-budget-compaction-strategy/
- [O4] Codex compaction tuning (third-party) — https://codex.danielvaughan.com/2026/04/16/codex-cli-context-compaction-tuning-long-sessions/ ; sample config https://developers.openai.com/codex/config-sample
- [O5] Codex models — https://learn.chatgpt.com/docs/models
- [R1] Chroma, Context Rot — https://www.trychroma.com/research/context-rot
- [R2] Raju et al., Limits of Long-Context Reasoning in Automated Bug Fixing — https://arxiv.org/abs/2602.16069
- [R3] Morph, context rot (secondary) — https://morphllm.com/context-rot
- [R4] MRCR secondary reports — https://codingfleet.com/blog/context-window-lie-how-well-ai-models-use-1m-tokens-2026/ , https://benchlm.ai/benchmarks/mrcr-v2-128k-256k
- [M1] Manus context engineering (via Schmid) — https://www.philschmid.de/context-engineering-part-2
