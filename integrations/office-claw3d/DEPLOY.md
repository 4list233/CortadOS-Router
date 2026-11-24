# Deploying the Office on the Air (runbook, NOT yet run)

Written for issue #77 on 2026-11-22 (review round 1 the same day). **Nothing here has been run.**
No user, token, LaunchDaemon, Node, Claw3D install or tailnet serve exists on any machine because
of this change. The owner runs `deploy-air.sh` on the Air when ready. Every mode is a dry run
(checks + the plan, nothing changed) unless given `--apply`.

```sh
cd ~/code/CortadOS-Router            # a clean checkout at the commit to deploy
integrations/office-claw3d/deploy-air.sh                        # dry run: checks + every command it would run
integrations/office-claw3d/deploy-air.sh --apply                # install, or upgrade to this commit
integrations/office-claw3d/deploy-air.sh --apply --rotate-office-token   # the same, with a new office:read token
integrations/office-claw3d/deploy-air.sh --publish-tailnet      # dry run of the separate tailnet step
integrations/office-claw3d/deploy-air.sh --publish-tailnet --apply
integrations/office-claw3d/deploy-air.sh --unpublish-tailnet --apply
```

The dry run may ask for sudo once: it reads, as root, whether the two LaunchDaemons are loaded
(`sudo launchctl print system/...`: as the owner, `launchctl print` cannot see the system domain and
answers "Could not find service" even for a running daemon) and, on an existing install, which secret
files are already in the role users' 0700 homes (`sudo test`). The script works from `/` (it `cd`s
there after resolving its own path), so the role users' commands never start in a directory they
cannot read, such as the owner's home.

## What runs, as whom

```
Pro / phone (tailnet) ──https://air.<tailnet>.ts.net:8446──▶ tailscale serve (Air, tailnet only; optional step)
                                                 │ http://127.0.0.1:3000
browser on the Air ──http://127.0.0.1:3000──▶ Claw3D Studio     (user _cortados-claw3d, LaunchDaemon)
                                                 │ access gate: studio_access cookie, set at /cortados-login
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
| Claw3D Studio (pinned, patched) | `_cortados-claw3d` (UID 481, hidden, no shell) | 127.0.0.1:3000 only | bridge secret (0600), Studio access token (0600) |
| `tailscale serve` (optional) | the Tailscale app | `https://<air>.ts.net:8446`, tailnet only | nothing |

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
  `/opt/cortados-office/claw3d/builds/<id>`. It is the [ESCALATE] in the report, because it pulls a large
  third-party dependency tree onto the Air.
- **`npm run build`** (Next.js production build). The runtime gateway settings come from
  `CLAW3D_GATEWAY_URL` / `CLAW3D_GATEWAY_TOKEN` / `CLAW3D_GATEWAY_ADAPTER_TYPE` at start, so no
  rebuild is needed when they change.
- **The 6-line patch** `claw3d/avatar-profile-hint.patch`, applied before the build. Without
  it, Studio still shows the pixel sprites in its 2D panels (`identity.avatarUrl` is stock).
  The 3D voxel people, however, get Claw3D's seed-random looks instead of the per-provider
  suits.
- **The login wiring** `claw3d/studio-login.patch` (3 added lines in `server/index.js`) and the
  module it loads, `claw3d/studio-login.cjs`, copied in as `server/cortados-studio-login.js`.
  Claw3D's access gate only checks a `studio_access` cookie and ships no way to set it; this adds
  a password form at `/cortados-login` that sets it (details under "Viewing it"). Not
  owner-approved yet: [ESCALATE] in the round-1 report.
- **The provider running on the Air** at 127.0.0.1:8432 (checked first: it must answer 401
  without a token).
- About 1 GB of disk for Node, `node_modules` and the build, twice during an upgrade (the
  previous build is kept for rollback). Ports 3000 and 18789 must be free.

## Steps the script performs

**Checks first, all of them, before the first change.** A refused run leaves the machine as it was.

- Host, architecture, not root, a clean `integrations/office-claw3d` in the checkout (the install
  is pinned to HEAD), the provider answering 401 on loopback.
- **Role users, exactly.** For each role the account name must hold exactly its UID (480 bridge,
  481 Studio; distinct, and no other account may share it). An account that already exists is
  reused only when it is exactly what step 1 creates: that UID, primary group 20, home under
  `/opt/cortados-office/home/`, shell `/usr/bin/false`, password `*`, no `AuthenticationAuthority`
  (no password or token login), hidden, not an admin. Anything else aborts and names the
  attribute. (The checks are `check_role` in `deploy-lib.sh`, tested with a fake `dscl`.)
- **A failed lookup is never a safe answer.** An account counts as missing only on `dscl`'s exact
  not-found answer (exit 56, `DS Error: -14136 (eDSRecordNotFound)`), and a UID as free only from a
  successful, fully parsed `dscl . -list /Users UniqueID`; root file checks (`root_test`) answer only
  from `test` itself, never a refused `sudo`; a daemon is "not loaded" only on `launchctl`'s
  `Could not find service` (exit 113), read as root (`launchd_state` / `launchd_running`; a refused
  `sudo` is "unknown", never "not loaded"); the rollback build and adapter the prune keeps are read with
  `link_target` (empty only when confirmed absent in a searchable directory); the health check's
  "loopback only" needs a successful `sudo lsof` seeing a loopback listener on each port
  (`listeners_loopback_only`). Anything else aborts before any change, or fails (and rolls back) the
  activation step that needed it.
- **An existing install.** `/opt/cortados-office` must be root:wheel 0755 with the layout this
  script makes: `adapter` and `node` links, `claw3d/current` a link into `claw3d/builds/`, homes
  0700 per role user, launchers and plists root-owned. Anything else (a plain directory where a
  link belongs, a leftover `claw3d/src`) aborts with "unknown layout". The provider client lives
  in one of two slots, `claw3d-office` or `claw3d-office-b`: at most one may be registered, with
  `office:read` alone (both registered means an interrupted rotation: remove the one the bridge
  does not use). A `.prev` file beside a launcher, plist or the office token means an earlier
  activation was killed before it could roll back: the run stops and names it.

Then:

1. **Role users.** `dscl` creates any that are missing (hidden, shell `/usr/bin/false`,
   password `*`), then re-runs the same check on the result.
2. **Node.** Kept when `/opt/cortados-office/node-v24.22.3` answers with that version. Otherwise
   it downloads the pinned tarball, checks its SHA-256, and extracts it root-owned into a
   `.partial` directory that is renamed into place.
3. **Adapter, staged.** Copies `adapter.mjs`, `bridge.mjs`, `sprites.mjs` and the two patches plus
   `studio-login.cjs`, root-owned and read-only, to `/opt/cortados-office/adapters/<commit>/`. The
   running bridge still uses the old copy.
4. **Claw3D, staged.** Builds into `claw3d/builds/<claw3d commit>-<checkout commit>/` as
   `_cortados-claw3d`: clone, check out the pinned commit, `git apply --check` and apply both
   patches, copy the login module, `npm ci --ignore-scripts && npm run build`, then write a
   `.cortados-build-ok` marker. A build dir with the marker is reused. One without it (a failed
   earlier run) is deleted and rebuilt. It is never the current one.
5. **Secrets.** Nothing is printed or passed on argv. Files that are already there are kept.
   - **office:read token.** When no slot is registered, the owner's
     `python3 -B -m cortados_router.provider.clients add claw3d-office --scope office:read --token-file <tmp>`
     mints it into a new 0600 file and registers only its hash. The script checks that the
     provider answers 200 to it (the header goes through a 0600 file, never argv), then stages it
     as `_cortados-office` (0600) in `/opt/cortados-office/stage/` and removes the temp copy with
     `rm -P`. When a slot is registered and the bridge has its file, it is kept. When one is
     registered but the file is missing, the run stops before any change and asks for
     `--rotate-office-token`. A rotation mints the new token into the **other** slot the same way;
     the running bridge's token stays registered and working until the new release is committed
     (step 8). A token registered by a run that then fails is revoked on exit.
   - **Bridge secret** (bridge + Studio) and **Studio access token** (Studio only): each is
     `openssl rand -hex 32` under `umask 077`, installed 0600 per role user. They are made only
     when missing.
6. **Launchers and LaunchDaemons, staged.** It writes the root-owned launchers `run-bridge.sh`
   and `run-studio.sh` and the plists `com.cortados.office-{bridge,studio}.plist` into
   `/opt/cortados-office/stage/` (root, 0700), not over the live ones. The launchers set paths and
   ports and read the secret files at start; Studio always gets `STUDIO_ACCESS_TOKEN`. Studio also
   gets `UPSTREAM_ALLOWLIST=localhost`, exactly the hostname of its `CLAW3D_GATEWAY_URL`
   (`ws://localhost:18789`, both built from one `GATEWAY_HOST` pin): Claw3D 51337f7 refuses **every**
   upstream when `NODE_ENV=production` and the allowlist is empty, so without it the browser's
   `connect` gets `studio.gateway_url_blocked` and the Office stays "CONNECTING" with 0 agents (#77,
   seen live on 2026-11-24). Only the loopback bridge is allowed. The plists
   set `UserName` = the role user, RunAtLoad, KeepAlive, logs under `/opt/cortados-office/log-*`.
7. **Activate, as one transaction** (`tx_run` in `deploy-lib.sh`). Only after steps 2–6 succeed:
   - it repoints `node`, `adapter` and `claw3d/current` to the staged versions (`ln -sfn` to
     `<link>.new`, then `mv -fh` over the link: a rename), remembering each old target;
   - it installs the staged launchers, plists and any new office token over the live ones (copy to
     `<file>.next`, then rename), each replaced file first copied to `<file>.prev`;
   - it restarts the daemons: one not loaded is bootstrapped, one loaded is booted out, waited for
     (up to 10 s), then bootstrapped again;
   - **health check**, for up to about 60 s, and again 10 s after it first passes: both daemons
     `state = running`, the bridge answering on 127.0.0.1:18789, Studio 401 on `/` without the
     cookie and 200 on `/cortados-login`, a `connect` through Studio's own proxy answered by the
     bridge, both listeners on loopback only. The connect is end to end, as a browser does it
     (`gateway-check.mjs`): a WebSocket to `127.0.0.1:3000/api/gateway/ws` with the `studio_access`
     cookie, then an OpenClaw `connect` without credentials; Studio's proxy injects its gateway token
     and forwards it, and only the bridge's `ok:true` hello passes. The access token is read as root
     and piped to the probe's stdin (never printed, never on a command line). Any error, refusal,
     close or its 5 s deadline fails the check and the release rolls back. Reason: the first live
     install passed every other check while Studio refused its upstream.

   **Any failure in it, or INT/TERM/HUP, rolls back:** both daemons are stopped, every step is
   undone in reverse (each file back from its `.prev`, each link to its old target, anything new
   removed), and the daemons that were loaded before are bootstrapped again with the previous
   release and the previous office token, which is still registered. The run then stops with
   "rolled back", or, if a rollback step itself failed, with "the rollback did not complete" and
   what failed. Tests inject a failure at each step (`deploy.test.mjs`, `fixtures/tx-harness.sh`).
8. **Commit.** The `.prev` copies are deleted, and on a rotation the old slot is revoked
   (`clients remove`); only now is the old credential gone. It shows the last lines of the
   bridge log (fixed messages, never a token).
9. **Prune.** It keeps the new build and adapter copy plus the previous ones (one rollback by
   hand later: point the links back and restart), and deletes older ones.

## Viewing it

Studio always runs with its access token, so every browser logs in once, including one on the Air.
The token is the Studio role user's 0600 file. To copy it to the Air's clipboard without
printing it, then store it in a password manager:

```sh
sudo cat /opt/cortados-office/home/studio/secrets/studio-access-token | pbcopy
```

- **On the Air:** open `http://127.0.0.1:3000/cortados-login`, paste the token, then **Log in**.
- **From the Pro or a phone (a private tailnet link).** This is a separate, explicit step, run
  on the Air after the install:

  ```sh
  tailscale serve status            # read first: what is already served (443 = Funnel /mcp, 8444 = provider)
  integrations/office-claw3d/deploy-air.sh --publish-tailnet          # dry run: checks + the one command
  integrations/office-claw3d/deploy-air.sh --publish-tailnet --apply
  ```

  It runs `tailscale serve --bg --https=8446 http://127.0.0.1:3000`. That is **Serve, never
  Funnel**: only devices on the owner's tailnet can reach it, over HTTPS with Tailscale's
  certificate. Studio stays bound to 127.0.0.1 and the bridge is never published. Before it
  serves anything, the script checks that Studio answers 401 without the cookie and 200 on the
  login form, so the gate is on. It also checks that 8446 is not already in `tailscale serve status`
  and that nothing there is funnelled. If `tailscale serve status --json` itself fails (tailscaled
  down, logged out), it stops and changes nothing: an unread config is never taken for a free port.
  Only a successful status with empty output counts as an empty config. Afterwards it re-reads the status. If 8446 is not exactly a
  tailnet-only proxy to `127.0.0.1:3000`, it takes the serve down and stops. It prints the
  link:

  `https://<air's MagicDNS name>:8446/cortados-login`, e.g. `https://air.tailb38028.ts.net:8446/cortados-login`

  On the Pro (any browser) or the phone (Safari/Chrome with the Tailscale app connected), open
  that link and enter the token. The password field lets the browser or iCloud Keychain keep
  it. The login sets the `studio_access` cookie (HttpOnly, SameSite=Strict, Secure over HTTPS,
  30 days), then redirects to the Office. After 30 days, or in a new browser, log in again.
  `--unpublish-tailnet --apply` takes the serve down (`tailscale serve --https=8446 off`).

What the login form does (`claw3d/studio-login.cjs`, tests in `studio-login.test.mjs`): it
serves only `/cortados-login`, and only when Studio has a token. It accepts a POST only from
its own origin (Origin equal to the request's scheme and Host or X-Forwarded-Host) and only
form-encoded, with the body capped at 4 KiB. Its pages send `Referrer-Policy: same-origin`, so a
browser's form POST carries the page's real Origin (a `no-referrer` page would make it send
`Origin: null`, which is refused). It compares the token in constant time. After 10
failures in a minute it answers 429. That limit is shared, because through Serve every request
reaches Studio from loopback; Claw3D's own gate counts the same way.

**Availability limit (accepted for a single-owner tailnet).** Because the lockout is shared, any
device on the tailnet can lock every device out of the login form for the lockout period by
sending wrong tokens (a one-minute window, renewed for as long as the wrong attempts continue).
It does not let anyone in. This form's limit blocks only new logins; browsers that already hold the
cookie are judged by Claw3D's own gate, whose shared counter was not checked live against a valid
cookie during a lockout. The owner accepts this while the tailnet has one
owner and only their devices; sharing the tailnet or the node with anyone else would need a
per-device limit (Serve's `Tailscale-User-Login` header) first. Every other request goes
to Claw3D's stock access gate unchanged.

Not checked live: whether Studio's browser WebSocket (`/api/gateway/ws`) works through Serve's
`wss://` (the cookie is sent same-origin, and Claw3D's proxy checks only the cookie, in
`verifyClient`).

In Studio, the backend shows as the URL `ws://localhost:18789` with adapter type `openclaw`.
Studio treats any adapter type it does not know as `openclaw`. The bridge reports
`cortados-office` in its hello, and Studio's proxy only uses the type to decide whether a token
is required, which it is. The proxy injects the token from `CLAW3D_GATEWAY_TOKEN` into `connect`. It opens
that upstream only because `UPSTREAM_ALLOWLIST` names its host (`localhost`, see step 6).

## Checks after install

- `sudo lsof -nP -iTCP:18789 -sTCP:LISTEN` shows only `127.0.0.1:18789`.
- `sudo lsof -nP -iTCP:3000 -sTCP:LISTEN` shows only `127.0.0.1:3000`.
- `python3 -B -m cortados_router.provider.clients list` shows one of `claw3d-office: office:read` or `claw3d-office-b: office:read` (after a rotation), never both.
- The login form works for a real browser, not only for curl. A browser does not let a page pick
  its POST's Origin: it derives it from the page's `Referrer-Policy`, and a `no-referrer` page makes
  every form POST carry `Origin: null`, which the form refuses with 403 "Log in from this page."
  (#77: the first install served `no-referrer` and passed every curl check). So check the header,
  then post the way a browser would. None of these needs the token:

  ```sh
  curl -sI http://127.0.0.1:3000/cortados-login | grep -i '^referrer-policy'
  # must print: Referrer-Policy: same-origin      (no-referrer here = no browser can log in)
  curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/x-www-form-urlencoded' --data 'token=not-the-token' \
    http://127.0.0.1:3000/cortados-login
  # must print 401: the same-origin Origin a same-origin page sends passes, the token is checked
  curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Origin: null' \
    -H 'Content-Type: application/x-www-form-urlencoded' --data 'token=not-the-token' \
    http://127.0.0.1:3000/cortados-login
  # must print 403: Origin: null stays refused (CSRF check)
  ```

  After `--publish-tailnet`, repeat the header check on `https://<air>.ts.net:8446/cortados-login`,
  then log in once from a real browser: that is the only end-to-end check.
- The Office reaches its data (no token needed beyond the login): after logging in, the page
  header's gateway status must leave "CONNECTING" within a few seconds and show it connected.
  "OPENCLAW • CONNECTING" that stays, with 0 agents, means Studio's proxy cannot reach the bridge:
  check that `/opt/cortados-office/bin/run-studio.sh` exports `UPSTREAM_ALLOWLIST=localhost`, and
  look in `/opt/cortados-office/log-studio/err.log` for `[gateway-proxy] refusing upstream`.
- `/opt/cortados-office/log-bridge/err.log` holds one `Office bridge listening on 127.0.0.1:18789 (read-only)`
  line. Repeated `Office poll failed` lines mean the token or the provider is wrong.
- In Studio's Office: one person per routed worker and one suited orchestrator per floor, wearing
  the sprites in `assets/preview.png`. People appear and leave within about 5 s of the provider
  (each poll pushes `presence`, and Studio's Office screen reloads the roster at most every 3 s).

## If `npm run build` fails

Lifecycle scripts are off (`--ignore-scripts`). If the build stops on a missing native binary
(for example `@next/swc-darwin-arm64` or `sharp`), the owner can decide to re-run
`npm ci` **with** scripts and then `npm run build`, as `_cortados-claw3d` in the failed
`/opt/cortados-office/claw3d/builds/<id>`. If both succeed, `touch .cortados-build-ok` there, and
the next `deploy-air.sh --apply` reuses that build and switches to it. The script never runs
lifecycle scripts by itself.

## Upgrading

Re-run `deploy-air.sh --apply` from a checkout at the new commit. It validates the existing
install, stages the new adapter copy and Claw3D build beside the running ones, and switches only
when they are complete. Secrets and the provider client are kept. If the build fails, the running
Office is untouched; the next run deletes the failed build dir and tries again.

To move Claw3D itself, change `CLAW3D_COMMIT` and re-check both patches against the new commit:
`git apply --check`. The avatar patch touches
`src/features/agents/operations/agentFleetHydrationDerivation.ts` and the login patch touches
`server/index.js`. Also re-check the gaps listed in PROTOCOL.md first.

Rollback: point `/opt/cortados-office/adapter` and `/opt/cortados-office/claw3d/current` back at
the previous entries in `adapters/` and `claw3d/builds/` (kept by the prune), then
`sudo launchctl kickstart -k system/com.cortados.office-{bridge,studio}`.

## Uninstall

```sh
integrations/office-claw3d/deploy-air.sh --unpublish-tailnet --apply   # if it was published
sudo launchctl bootout system/com.cortados.office-studio
sudo launchctl bootout system/com.cortados.office-bridge
sudo rm /Library/LaunchDaemons/com.cortados.office-{studio,bridge}.plist
python3 -B -m cortados_router.provider.clients remove claw3d-office     # revokes the office:read token
sudo dscl . -delete /Users/_cortados-claw3d
sudo dscl . -delete /Users/_cortados-office
sudo rm -rf /opt/cortados-office
```
