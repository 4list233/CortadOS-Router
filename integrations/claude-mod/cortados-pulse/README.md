# cortados-pulse — a Claude Code pane for the router

A mod: a pane beside the chat that shows what CortadOS is running and the resource pool behind it. It reads two of the
provider's views with the same token: `GET /v1/cortados/live` every 10 seconds and `GET /resources` every 20.

- **This session** (at the top) — a live tree of THIS session's own work, in the style of claude-flightdeck
  (`docs/PULSE-PER-SESSION.md`): a count line (running / waiting / done / failed / unknown) and `! N needs attention`
  when a task failed or its state is unknown; each running or queued `start_task` child opened (title and elapsed time
  counting between polls, then model, host, tool and fresh tokens, then its sub-sessions: the child's main loop and
  routed subagents, `├ ● main loop · <model> · model turn · 4.9k tok · 4m 50s`); nested tasks indented under their
  parent (`↳`); the last three finished folded to one line; tasks this machine started on another host (`?`, state
  there); and the subagents the session routed directly. Glyphs: `●` running, `◷` waiting, `✓` done, `✕` failed, `?`
  unknown, `⊘` cancelled; attention lines are bold. The pane names its session by its Office tag: sha256 of the
  machine's shared salt (`~/.config/cortados/office-salt`, made by the `cortados-route` MCP server;
  `CORTADOS_OFFICE_SALT_FILE` overrides) and the session id, cut to 16 hex, exactly as `cortados-routed-subagents`
  names its floor and the MCP server names a task's origin. It never sends or shows the raw session id. With the tag it
  reads ONLY `GET /v1/cortados/live/session?session=<tag>`, which the provider builds filtered: that one subtree plus
  S1 and host health, nothing of another session (no fleet Running section is drawn then). Without the salt file, or
  against a provider from before the per-session view (404), it falls back to the 0.4 behaviour below: the fleet view
  `/v1/cortados/live?session=<tag>` filtered for this session, and the Running section. A dim line recalls that after
  `/clear` earlier tasks stay under the previous session (the dashboard has them);
  Under the heading, **this session's work split** (#127, from the same session view): one line,
  `work split 24h: routed 66% fresh (1.4M tok: codex/gpt-6.1-sol 0.8M, claude#b/claude-opus-5-5 0.5M, …) · native 0.7M
  (claude-opus-5-5, claude-haiku-5) · 15% counting cache reads` — routed is what the provider served for this session
  (its child tasks, nested ones included, its routed subagents and its route_task calls), native is the session's own
  model (main loop and direct subagents), fresh = input + output + cache writes. The `w` button (or key `w` while the
  pane has the focus) opens the breakdown: every route, by hand-over kind, native by model, the four token kinds and the
  7-day figures. The provider's notes on partial data are drawn dim under it (no native report from this machine's
  heartbeat, a stale heartbeat, unreadable transcripts, routed tokens not recorded). A `start_task` of this session
  whose result names another origin tag than the pane's (the cortados-route server keeps the session id it started
  with, so after a `/clear` its tasks are under the old tag) makes the pane read that tag's view too and draw it under
  a dim "tasks this session started under its cortados-route server's tag" line. That view is read by the same helper
  as the session's own (`readSessionView`) and says its last problem with the held data's age; when a read of it is still
  outstanding 30 s (2× the 15 s timeout) after it began (a host that ignored the abort), the view is marked "stale since
  …" and its tasks are dropped, so no elapsed time keeps counting on data that is not coming;
- **Running** (fallback only: from `/v1/cortados/live`) — one summary line (children running and queued, conversations and how many
  are working), then each running or queued `start_task` child (title, host, model, current tool, elapsed time counting
  between polls) and every conversation the provider holds open (main sessions, subagents, untagged clients, and a
  child task's when it has no task line here: model, tool, elapsed). A section
  the provider could not read says so in its place, never "nothing running"; a provider from before the live view
  (404) is named as such and the rest of the pane still draws;
- **usage** — routes logged, degraded rate, S1 latency (median / p90 over the last scored turns), the top lanes;
- **subscriptions** — one row per lane as the provider's scarcity cache knows it: `claude` and `claude#<id>` (one per
  Claude account, labelled with the reading's config folder when present), `codex`, `cursor`, `antigravity`; each with
  ok/SPENT or explicit unknown quota, scarcity, expiring credit, the age of its usage sample, and every cached window's
  **% left** (weekly, 5-hourly, the Fable pool, …) — the same figures the S1 outcome loop's quota door reads. The `local`
  lane shows routing eligibility and its measured-resource reason; `/resources` does not expose deployment counts;
- **Local nodes** (#127, `GET /v1/cortados/local` every 30 s, the owner token) — each local node as its gate reports
  it: switch, allowed or not and why, power, backend (ollama / vllm-mlx) and whether it answers, the loaded models, then
  the gate's admission: in flight / limit, owner activity, memory pressure, free memory. This is the local lane's real
  state. The subscriptions' `local` row is the scarcity cache's resource-pool deployment check and is labelled so
  (`local (resource-pool deployments): …`);
- **devices** — the resource-pool probe over the fleet config's SSH targets (not the local gate): each probed node's
  GPU headroom, utilisation and how many local models fit, plus the pool over the nodes it reached; a node without an
  SSH target in that config is `not probed`, never `free · fits 0`;
- **Fleet** (from `/v1/cortados/live`) — S1 up/down and latency, the lowest fresh subscription window, and each host's job
  heartbeat (`studio: s1-scorer running, s1-continuous running 38% · cpu 30% · disk 62% free`, and on the S1 host
  `S1 up: <model> · promotion held · candidate promotion held`) or `no heartbeat yet`;
- the last line links the browser dashboard, `<provider>/dashboard`, which shows the same live view in full.

**Refresh timing.** The pane polls on its own 10 s timer (not at the end of a turn): the session view every tick,
`/resources` every 20 s, the local nodes and this machine's own task log every 30 s, and once more right after a
`start_task` of this session returns. Each provider read is given up after 15 s, so a request that never answers
cannot stop the polling (the polls run one at a time).

Both views are cache-only on the provider (they never open a vendor CLI or ssh), so the polls are cheap. The served
dashboard (`GET /dashboard`) has a Live tab over the same `/v1/cortados/live` and a Usage tab over the richer
`/v1/cortados/usage`; this pane is the in-terminal view that stays beside the chat. Section headers are bold; problems
and the snapshot's age are dim.

## Install

```
/plugin marketplace add 4list233/CortadOS-Router
/plugin install cortados-pulse@cortados-router
```

or, from a checkout, `claude --plugin-dir /path/to/CortadOS-Router/integrations/claude-mod/cortados-pulse`.

Then put the provider's address and token where the mod looks (below), restart Claude Code, and the pane opens beside
the chat on session start; `/cortados-pulse` opens it again.

## Configuration

| Field | Default | Meaning |
| --- | --- | --- |
| `providerUrl` | *(unset)* | Explicit override. Otherwise `CORTADOS_PROVIDER_URL`, then the URL file: `CORTADOS_PROVIDER_URL_FILE` or `~/.config/cortados/provider-url`. One `http://` or `https://` URL with a host; anything else is refused and named in the pane — there is no guessed address, because the provider runs on another host. |
| `providerTokenFile` | `~/.config/cortados/provider-token` | File holding `CORTADOS_PROVIDER_TOKEN`, read fresh on every poll (a rotated token is picked up without a restart). `~` expands to `$HOME`. |
| `providerToken` | *(unset)* | Optional inline override (stored as a sensitive setting). Prefer the file. |

The same files serve `cortados-routed-subagents` and the `cortados-route` MCP server, so one machine is configured once:

```sh
mkdir -p ~/.config/cortados
printf '%s\n' 'http://<provider-host>:8432' > ~/.config/cortados/provider-url
printf '%s\n' "$CORTADOS_PROVIDER_TOKEN" > ~/.config/cortados/provider-token
chmod 600 ~/.config/cortados/provider-token
```

The pane needs the owner token (`/resources` has no scoped reader); a `live:read` client token opens only the live
views, and a `live:session` client token only this session's tree (`/v1/cortados/live/session`; the resource sections
then say the token was rejected).
The token is sent only as the `Authorization: Bearer` header of each poll. It is never logged, rendered or kept in the
mod's state, and every message the pane shows is scrubbed of it. A `401`/`403` is reported as "provider rejected the
token" naming the token source (file path or `providerToken`), never the token.

## Tests

Run the mod's tests in the native hook host (no model calls, no network):

```sh
CORTADOS_HOME=$(mktemp -d) CORTADOS_SCARCITY_REFRESH=off claude plugin test integrations/claude-mod/cortados-pulse
```

`hooks/resources.fixture.ts`, `hooks/stale-resources.fixture.ts`, `hooks/live.fixture.ts` and `hooks/session.fixture.ts`
are generated by the provider's own code (`tests/test_pulse_fixture.py` and `tests/test_orchestrator_tree.py` check each
still matches; after a deliberate change to the live view,
`python3 -B -c "from tests import test_pulse_fixture as t; t.write_live_fixture()"` and
`python3 -B -c "from tests import test_orchestrator_tree as t; t.write_session_fixture()"` regenerate them).

`claude plugin validate integrations/claude-mod/cortados-pulse` lists what the module touches (`$.env.get` of
`CORTADOS_PROVIDER_URL`, `CORTADOS_PROVIDER_URL_FILE`, `HOME`; `$.fs.read`; `$.http.fetch`). To type-check, load the mod
once with `--plugin-dir` (the engine lays its typings in `.claude-plugin/types/`, git-ignored) and run `tsc -p
tsconfig.json` from the mod folder. `tests/test_mod_manifest.py` in the repository suite checks that every setting the
hooks read is declared in `plugin.json` with the same default.
