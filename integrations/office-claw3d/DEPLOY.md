# Deploying the Office on the Air (runbook, NOT yet run)

Written for issue #77 on 2026-11-22. **Nothing here has been run.** No user, token, LaunchDaemon,
Node or Claw3D install exists on any machine because of this change. The owner runs
`deploy-air.sh` on the Air when ready. It is a dry run (prints the plan, changes nothing)
unless given `--apply`.

```sh
cd ~/code/CortadOS-Router            # a checkout at the commit to deploy
integrations/office-claw3d/deploy-air.sh            # dry run: preconditions + every command it would run
integrations/office-claw3d/deploy-air.sh --apply    # do it (asks for sudo per step)
```

## What runs, as whom

```
browser on the Air ──http://127.0.0.1:3000──▶ Claw3D Studio     (user _cortados-claw3d, LaunchDaemon)
                                                 │ Studio's own server-side proxy
                                                 ▼ ws://localhost:18789, Origin http://localhost:18789,
                                                   connect.params.auth.token = bridge secret
                                              office bridge     (user _cortados-office, LaunchDaemon)
                                                 │ GET every 5 s, Bearer office:read token
                                                 ▼
                                              provider  http://127.0.0.1:8432/v1/cortados/office
```

| Piece | User | Listens | Holds |
|---|---|---|---|
| provider (already running) | owner | 127.0.0.1:8432 | owner token, `clients.json` (hashes only) |
| office bridge (`bridge.mjs`) | `_cortados-office` (UID 480, hidden, no shell) | 127.0.0.1:18789 only | office:read token (0600), bridge secret (0600) |
| Claw3D Studio (pinned, patched) | `_cortados-claw3d` (UID 481, hidden, no shell) | 127.0.0.1:3000 only | bridge secret (0600) |

Two users, not one. Studio is a large npm application, and its README says it may load the
upstream token into the browser. So Studio's user only ever holds the **bridge secret**, which
opens nothing but the read-only bridge. The provider credential (office:read) stays with the
bridge user. The bridge refuses to start if the two secrets are equal.

## What Claw3D needs on the Air

- **Node 20+** (Claw3D README). The Air has no Node today. The script installs Node
  v24.22.3 LTS from the nodejs.org tarball and checks its SHA-256. It goes under
  `/opt/cortados-office/node`, with no Homebrew and nothing on the system PATH.
- **git** (Xcode command line tools) to clone Claw3D at the pinned commit
  `51337f7f250459af61a7cf92b7b0015d59faed5c` (MIT, iamlukethedev/Claw3D).
- **npm dependencies**: `npm ci` from Claw3D's lockfile (Next 16, React 19, three, Phaser,
  `ws`, and others). This is the only npm use in the whole integration, confined to
  `/opt/cortados-office/claw3d/src`. It is the [ESCALATE] in the report, because it pulls a large
  third-party dependency tree onto the Air.
- **`npm run build`** (Next.js production build). The runtime gateway settings come from
  `CLAW3D_GATEWAY_URL` / `CLAW3D_GATEWAY_TOKEN` / `CLAW3D_GATEWAY_ADAPTER_TYPE` at start, so no
  rebuild is needed when they change.
- **The 6-line patch** `claw3d/avatar-profile-hint.patch`, applied before the build. Without
  it, Studio still shows the pixel sprites in its 2D panels (`identity.avatarUrl` is stock).
  The 3D voxel people, however, get Claw3D's seed-random looks instead of the per-provider
  suits.
- **The provider running on the Air** at 127.0.0.1:8432 (checked first: it must answer 401
  without a token).
- About 1 GB of disk for Node, `node_modules` and the build. Ports 3000 and 18789 must be free.

## Steps the script performs

1. **Role users.** `dscl` creates `_cortados-office` and `_cortados-claw3d`: hidden, shell
   `/usr/bin/false`, password `*` (no login), homes under `/opt/cortados-office/home/`.
2. **Node.** Downloads the pinned tarball, checks its SHA-256, extracts it root-owned to
   `/opt/cortados-office/node-v24.22.3`, and symlinks `node`.
3. **Adapter.** Copies `adapter.mjs`, `bridge.mjs` and `sprites.mjs` root-owned and read-only to
   `/opt/cortados-office/adapter/`. The copy is pinned to the checkout's HEAD, which the script prints.
4. **Claw3D.** Clones Claw3D as `_cortados-claw3d`, checks out the pinned commit, runs
   `git apply --check` and then applies the patch, then runs `npm ci --ignore-scripts && npm run build`.
5. **Secrets.** Nothing is printed or passed on argv.
   - The owner runs `python3 -B -m cortados_router.provider.clients add claw3d-office --scope office:read --token-file <tmp>`.
     It mints the token into a new 0600 file and registers only its hash. The script then
     `install`s the file as `_cortados-office` (0600) and removes the temp copy with `rm -P`.
   - It runs `openssl rand -hex 32` under `umask 077` to make the bridge secret, installs one 0600
     copy per role user, and removes the temp file.
6. **Launchers and LaunchDaemons.** It writes the root-owned launchers `run-bridge.sh` and
   `run-studio.sh`. They set paths and ports and read the secret file at start. It also writes
   `/Library/LaunchDaemons/com.cortados.office-{bridge,studio}.plist` (`UserName` = the role user,
   RunAtLoad, KeepAlive, logs under `/opt/cortados-office/log-*`), then runs
   `launchctl bootstrap system`.
7. **Checks.** Both listeners must be on loopback only, or the script stops. Studio must answer
   on 127.0.0.1:3000. It shows the last lines of the bridge log (fixed messages, never a token).

## Viewing it

Studio is loopback-only and has no `STUDIO_ACCESS_TOKEN`. Open `http://127.0.0.1:3000` on the
Air, or from the Pro run `ssh -L 3000:127.0.0.1:3000 air` and open the same URL. Exposing it on
the tailnet (`HOST=<tailnet ip>` + `STUDIO_ACCESS_TOKEN`, or `tailscale serve`) is a separate
owner decision ([ESCALATE] in the report) and is not part of this script.

In Studio, the backend shows as the URL `ws://localhost:18789` with adapter type `openclaw`.
Studio treats any adapter type it does not know as `openclaw`. The bridge reports
`cortados-office` in its hello, and Studio's proxy only uses the type to decide whether a token
is required, which it is. The proxy injects the token from `CLAW3D_GATEWAY_TOKEN` into `connect`.

## Checks after install

- `sudo lsof -nP -iTCP:18789 -sTCP:LISTEN` shows only `127.0.0.1:18789`.
- `sudo lsof -nP -iTCP:3000 -sTCP:LISTEN` shows only `127.0.0.1:3000`.
- `python3 -B -m cortados_router.provider.clients list` shows `claw3d-office  office:read`.
- `/opt/cortados-office/log-bridge/err.log` holds one `Office bridge listening on 127.0.0.1:18789 (read-only)`
  line. Repeated `Office poll failed` lines mean the token or the provider is wrong.
- In Studio's Office: one person per routed worker and one suited orchestrator per floor, wearing
  the sprites in `assets/preview.png`. People appear and leave within about 5 s of the provider
  (each poll pushes `presence`, and Studio's Office screen reloads the roster at most every 3 s).

## If `npm run build` fails

Lifecycle scripts are off (`--ignore-scripts`). If the build stops on a missing native binary
(for example `@next/swc-darwin-arm64` or `sharp`), the owner can decide to re-run
`npm ci` **with** scripts, as `_cortados-claw3d` in `/opt/cortados-office/claw3d/src`. The script
does not do this by itself.

## Upgrading Claw3D

Change `CLAW3D_COMMIT` in `deploy-air.sh`, run `git apply --check` of the patch against the new
commit (the patch touches `src/features/agents/operations/agentFleetHydrationDerivation.ts`
only), and re-check the gaps listed in PROTOCOL.md before running the script again.

## Uninstall

```sh
sudo launchctl bootout system/com.cortados.office-studio
sudo launchctl bootout system/com.cortados.office-bridge
sudo rm /Library/LaunchDaemons/com.cortados.office-{studio,bridge}.plist
python3 -B -m cortados_router.provider.clients remove claw3d-office     # revokes the office:read token
sudo dscl . -delete /Users/_cortados-claw3d
sudo dscl . -delete /Users/_cortados-office
sudo rm -rf /opt/cortados-office
```
