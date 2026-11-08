# cortados-pulse — a Claude Code pane for the router

A mod: a pane beside the chat that polls the CortadOS provider's `GET /resources` every 20 seconds and shows the live
resource pool:

- **usage** — routes logged, degraded rate, S1 latency (median / p90 over the last scored turns), the top lanes;
- **subscriptions** — one row per lane as the provider's scarcity cache knows it: `claude` and `claude#<id>` (one per
  Claude account, labelled with the login's config folder), `codex`, `cursor`, `antigravity`; each with ok/SPENT,
  scarcity, expiring credit, the age of its usage sample, and every cached window's **% left** (weekly, 5-hourly, the
  Fable pool, …) — the same figures the S1 outcome loop's quota door reads; the `local` lane shows its verified
  deployments;
- **devices** — the online local nodes with their GPU headroom, utilisation and how many local models fit, plus the pool.

`/resources` is cache-only on the provider (it never opens a vendor CLI), so the poll is cheap. The served dashboard
(`GET /dashboard`) reads the richer `/v1/cortados/usage`; this pane is the in-terminal view that stays beside the chat.

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

The token is sent only as the `Authorization: Bearer` header of each poll. It is never logged, rendered or kept in the
mod's state, and every message the pane shows is scrubbed of it. A `401`/`403` is reported as "provider rejected the
token" naming the token source (file path or `providerToken`), never the token.

## Tests

Run the mod's tests in the native hook host (no model calls, no network):

```sh
CORTADOS_HOME=$(mktemp -d) CORTADOS_SCARCITY_REFRESH=off claude plugin test integrations/claude-mod/cortados-pulse
```

`claude plugin validate integrations/claude-mod/cortados-pulse` lists what the module touches (`$.env.get` of
`CORTADOS_PROVIDER_URL`, `CORTADOS_PROVIDER_URL_FILE`, `HOME`; `$.fs.read`; `$.http.fetch`). To type-check, load the mod
once with `--plugin-dir` (the engine lays its typings in `.claude-plugin/types/`, git-ignored) and run `tsc -p
tsconfig.json` from the mod folder. `tests/test_mod_manifest.py` in the repository suite checks that every setting the
hooks read is declared in `plugin.json` with the same default.
