# cortados-pulse — a Claude Code pane for the router

A mod: a pane beside the chat that polls the CortadOS provider's `GET /resources` every 20 seconds and shows the live
resource pool — routes logged, degraded rate, S1 latency, each subscription's scarcity and expiring credit, and the
online local devices with their GPU headroom and how many local models fit.

It reads the same data as the served dashboard (`GET /dashboard` in a browser, reachable from any device on the tailnet);
this is the in-terminal version that stays beside the chat.

## Load it

```bash
claude --plugin-dir /path/to/CortadOS-Router/integrations/claude-mod/cortados-pulse
```

Set the mod's config `providerUrl` (the provider base URL, e.g. the Air on the tailnet) and `providerToken`
(`CORTADOS_PROVIDER_TOKEN`, the same token the dashboard and the `route_task` MCP use). Open the pane with the
`/cortados-pulse` command; it also opens on session start.

No address or token is committed; both come from the mod's config.

## Install (Claude Code / Claude desktop app)

One step on Claude Code 2.1.275 or later:

```
/plugin install cortados-pulse --marketplace 4list233/CortadOS-Router
```

Then set the two config values when prompted: **Provider URL** (e.g. `http://100.103.11.9:8432`, the Air on the tailnet) and **Provider token** (`CORTADOS_PROVIDER_TOKEN`). The pane opens beside the chat and refreshes every few seconds.
