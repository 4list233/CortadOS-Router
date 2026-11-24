# PC (RTX 3060) enrollment: options and recommendation

Status: **design only** (2026-11-24, branch `pc-rtx3060-enrollment`). No router code, router config or PC setting was
changed to write it. Every `[ESCALATE]` below is the owner's decision; nothing here is done without it.

Placeholders. The repository is public, so this doc names no address, account or key path:

| placeholder | meaning |
| --- | --- |
| `<pc>` | the PC's ssh alias in `~/.ssh/config` on the Pro/Air (the alias holds the host, user, `IdentityFile`, `IdentitiesOnly yes`) |
| `<pc-ts-ip>` | the PC's Tailscale IPv4 address |
| `<pc-user>` | the Windows account the owner uses today (an administrator: its key is in `administrators_authorized_keys`) |
| `<py>` | the absolute path of Python 3.12's `python.exe` on the PC |
| `<pc-gate-token>` | a new 64-hex token file for the PC's gate, on the PC and on the provider host only, never printed |

Read-only commands in this doc go through `ssh <pc> "powershell -NoProfile -Command ..."` because the PC's default ssh
shell is cmd. Ignore the post-quantum key-exchange warning OpenSSH prints.

## 1. Recommendation in short

1. **Do option A first: model server only.** Strata stays bound to `127.0.0.1` on the PC. The existing local gate runs
   on the PC as the only door from the tailnet, after a bounded Windows port plus a new `strata` backend. The PC
   becomes a second local node (`pc`) beside the Pro in the provider's `tables/resources.json`. Start it **pin-only**
   (`sub/qwen36-35b`). Turn on selection by a published proxy index (R0, later R1 at low D) only after a week of pinned
   use. Estimate: ~400–700 changed lines with tests; low risk to the router, because the provider side already handles
   several nodes.
2. **Do not make the PC an execution host now (option B).** Air children keep driving the PC over ssh with pre-approved
   commands, which worked overnight (B0 below). A real Windows exec host is a 2–4 week port of the most
   safety-reviewed code in the repo (process groups, workspace deletion), or a WSL2 shim the owner removed. Either way
   the PC would have to hold an owner-level provider token. Revisit B only for Windows-only or CUDA-only work.
3. **Do not do both together (option C)** until A has run for a while. With the 35B loaded, Strata keeps 18.75 GiB of
   experts in host RAM. Free RAM measured 0.4 GiB with the owner's apps open. Claude Code children and git on the
   same 32 GB machine would contend with it.

## 2. Facts

### 2.1 Machine and access (checked 2026-11-23/24)

- Windows 11 Home 10.0.26300; RTX 3060 12 GB (WDDM, driver 621.24, CUDA UMD 13.5); 32 GB DDR4; ~190 GB free on C:.
  WSL has been removed.
- On the owner's tailnet. **At the time of writing (2026-11-24 ~10:15 ET) the PC was offline on the tailnet: last
  seen 5 h earlier, so asleep or off.** ssh timed out, so this doc has no new live readings. Facts come from the
  overnight runs (`~/code/strata-pc-results/` on the Pro; Grounds issue #9) and Strata's own docs
  (`github.com/Niko1221/Strata`, commit `d46f5f2`, read 2026-11-24).
- ssh: Windows OpenSSH, key auth for an **administrator** account (`administrators_authorized_keys`), default shell
  cmd. Processes started in an ssh session die when the session ends: sshd puts them in a job object.
- Installed: git, Python 3.12, Claude Code (`claude.exe`, native), Strata 0.2.20 (engine 0.1.66, CUDA).
- It is the owner's everyday desktop and gaming PC. The GPU process list during the bench showed a game anti-cheat
  service, a remote-desktop host, a game recorder, an animated wallpaper, chat apps, two Claude apps and Chrome.
  1.36 GiB of VRAM was in use before any model was loaded.

### 2.2 Strata on this PC

- Its own engine (`strata.exe --serve --native <gguf> --max-context N --mtp 2 --vision`) behind a Python HTTP server.
  It is not llama.cpp's server and not Ollama.
- Engine and models live in `%LOCALAPPDATA%\Strata`. `STRATA_DATA_DIR` was ignored; `--data-dir`/`--move-data` exist
  for new downloads.
- Endpoints (Strata `docs/DETAILS.md` "Using it"): `POST /v1/messages` (Anthropic, stream + tools),
  `POST /v1/chat/completions`, `POST /v1/responses`, `GET /v1/models`, `GET /health`, `GET /props`, `GET /slots`,
  `GET /metrics`. It also has **control and side-effect endpoints**: `POST /load`, `/unload`, `/v1/load`, `/v1/unload`,
  `POST /v1/vram`, `POST /slots/0?action=save|restore` (writes or reads a session file), `GET /mcp` (MCP tools that run
  server-side) and an API request monitor.
- `/v1/models` lists only the loaded model (with `context_length`); "any other name is still served". So the server
  does not enforce the model name.
- Auth: an optional API key. Bound to loopback it needs none. `--access lan|tailscale` binds wider and creates a key.
  The bare engine server only warns when opened without a key.
- Concurrency: `--parallel` "fit to your card" is **1 on a 12 GB card**.
- VRAM sharing: `idle_unload_s`, `min_free_vram_mib` (answers 503 "the GPU is in use by another program" instead of
  loading), `before_load`.
- At startup: Strata documents Task Scheduler. With the defaults, an expert load ran ~25× slower (0.05 vs 1.42 GiB/s,
  measured by Strata on other hardware). The fix is **Priority Normal** and **Run with highest privileges**.

### 2.3 Measured (Strata `strata_bench.py --runs 3 --long`, medians)

| model | context | decode | TTFT | prompt read 4K / 16K / 32K | agent decode | notes |
| --- | --- | --- | --- | --- | --- | --- |
| Qwen3.6-35B-A3B UD-Q4_K_XL | 48K (49,152) | 30.8 tok/s | 0.54 s | 672 / 622 / 556 tok/s | 19.2 tok/s | 128K does not fit; at most 63,488 fit; 4.40 GiB of tensors on the GPU and **all 18.75 GiB of experts in host RAM**; load + warm-up ~195 s; VRAM 11.6 / 12.0 GiB used, **free RAM 0.4 GiB** with the owner's apps open |
| Qwen3.8-Flash-Next Coder IQ1_M (~125B MoE) | 32K | 5.8 tok/s | 4.8 s | 455 / 852 / skipped | 6.8 tok/s | experts streamed from SSD; one model at a time, so switching models means a restart and minutes of load |

What this means: a 32K prompt takes about a minute just to read. A Claude Code turn resends the whole conversation, and
51% of the agent cache was hit. At 48K, Claude Code's own system prompt and tool list (tens of thousands of tokens)
leave little room. The 35B fits short, self-contained work. It does not fit long agent sessions.

### 2.4 Published scores (tables/benchmarks/scores.csv)

| model | AA intelligence / coding | other sources |
| --- | --- | --- |
| `qwen3-6-35b-a3b` (reasoning) | 18.2 / 41.9 | Epoch ECI: GPQA 0.80, Terminal Bench 0.23, … |
| `qwen3-6-35b-a3b-non-reasoning` | 15.2 / 28.1 | |
| (Pro) `qwen3-8-27b-non-reasoning`, `qwen3-8-27b` | 20.2 / 44.6, 33.7 / 68.1 | |
| `qwen3-8-flash-next` (full precision) | 39.8 / 73.1 | 8 AA rows only |

The 35B with thinking on scores about the same as the Pro's 27B with thinking off. The Coder's base model scores high,
but its **IQ1_M** copy (about 1.75 bits per weight) is far from what was published.

### 2.5 How the router does it today (read for this doc)

- **Exec hosts**: `exec_hosts.py` reads `~/.config/cortados/exec-hosts.json`. A remote host is one `ssh` running
  `child_tasks.py --remote` with JSON in and out. On the host, a detached runner (`--run`) supervises `claude -p`, and
  `workspaces.py` gives each task a git worktree under `<root>/active/<task_id>`, settled to `archive/` or `held/`. Chrome
  and connector children on a `gui` host run as per-task LaunchAgents. The Studio is "server only": it has no entry,
  which is the precedent for a compute node that is not a task host.
- **Local lane**: the provider (`provider/local_lane.py`) reads every node with an `ollama` block in
  `CORTADOS_LOCAL_RESOURCES_CONFIG` (`tables/resources.json` on the Air), in config order. It probes `GET /api/tags`
  and relays `POST /v1/messages` to the node's **gate** (`local_gate.py`) with that node's bearer token. The gate is the
  node's only door, forwards only those two paths and serves the node's manual switch at `/cortados/local-switch`.
  `local_backend.py` adapts the gate per backend (`ollama`, `vllm-mlx`).
- **Selection**: `routing.index_local` offers a local variant where its proxy index (`tables/local-models.json` →
  `index_proxies`, built into `model-scores.generated.json` → `local_models`) clears S1's required index. Never for a
  degraded scorer, outward work, blast radius 3 or R3/R4. A failed probe is remembered for `AUTO_DOWN_S` (30 s). A node
  at `max_inflight` counts as busy.
- **Scarcity sampler**: `local_resources.py` samples nodes over ssh (`python3 - --collect`). The example config still
  shows a `pc3060` worker as `owner@wsl-host` with `memory_scope: wsl_vm`. That is stale now that WSL is gone.

## 3. Question 1: the PC as an execution host

### 3.1 What assumes macOS or a POSIX shell

Python on Windows has no `fcntl`, `os.killpg`, `os.setsid`/`start_new_session`, `preexec_fn`, `os.getuid` or
`signal.SIGKILL`. Its `dir_fd`/`src_dir_fd` support is empty (`os.supports_dir_fd == set()`), `os.getloadavg` does not
exist, and file mode bits do not describe access (ACLs do). Each item below fails or changes meaning there.

**Requesting side (the Pro/Air calling the PC):**

| where | assumption | on Windows |
| --- | --- | --- |
| `exec_hosts.py:136-138` `remote_command` | `shlex.quote(python) -B shlex.quote(path) --remote`, POSIX single quotes | cmd does not treat `'…'` as quoting. In PowerShell a quoted first token is a string, not a command, so it needs `& '…'`. Needs a per-host quoting mode (cmd double quotes, or `DefaultShell` set to PowerShell plus `&`). |
| `exec_hosts.py:211-215` `ssh_argv` | `StrictHostKeyChecking=yes`, `BatchMode` | works; the PC's host key must be in `known_hosts` on every requesting machine |
| `exec_hosts.py:71` `FIELDS` / `child_tasks.py:4001` | `path` is a `:`-joined PATH | Windows needs `;`; `root`/`router`/`python`/`claude` are Windows paths |
| `child_tasks.py:2162-2174` `watch_command` | built with `shlex.quote(sys.executable)` | runs on the requesting Mac, so it is fine; its remote polls go through `remote_command` (above) |

**Host side (the PC running `--remote`, `--run` and the workspace code):**

| where | assumption | on Windows |
| --- | --- | --- |
| `child_tasks.py:45`, `:1277-1278`, `:1756`, `:1838`, `:3084-3088`, `:3236`, `:3770-3777`, `:3832-3833` | `fcntl.flock` for the runner lock, chrome lock, export ledger and status reads | **import fails**: the module does not load. Needs `msvcrt.locking` / `LockFileEx` (byte-range and mandatory, not advisory; the semantics differ). |
| `child_tasks.py:1338-1341` | runner = `Popen(..., start_new_session=True, pass_fds=(lock_fd,))`, so it outlives the MCP server / ssh | `start_new_session` is ignored and `pass_fds` is unsupported. **sshd's job object kills the runner when the ssh call returns** (seen overnight). Needs `CREATE_BREAKAWAY_FROM_JOB` (only if sshd's job allows breakaway: unverified) or a pre-registered scheduled task / resident service that starts runners. |
| `child_tasks.py:3315-3328`, `:3858` | `preexec_fn=_child_record_writer(d)` writes claude's pgid after `setsid`, before exec | `preexec_fn` raises `ValueError` on Windows. Needs a Job object per child (`CreateJobObject` + `AssignProcessToJobObject` via ctypes) and a record written after spawn. |
| `child_tasks.py:3272-3290`, `:3365-3383`, `:2585` | `os.killpg(pgid, SIGTERM)`, then `SIGKILL`; `os.kill(pid, SIGTERM)` on the runner; `killpg(pgid, 0)` as a liveness probe | none exist. `TerminateJobObject` (or `taskkill /T /F`) has no graceful SIGTERM phase. Claude Code's own children (node, Git Bash) must be in the job. |
| `child_tasks.py:3283-3311` `PS = "/bin/ps"`, `_proc_start` (`ps -o lstart`) | process start time to rule out a reused pid before signalling | no `/bin/ps`. Needs `GetProcessTimes` via ctypes. The code treats a failure as "unknown, never signal", so it fails safe, but cancel never works. |
| `child_tasks.py:3830` `signal.signal(SIGTERM, on_term)` | the runner's cancel path | Windows delivers no SIGTERM to a console-less process. Needs a cancel file or event polled by the runner. |
| `child_tasks.py:50`, `:231-233`, `:1263-1269`, `:1415-1500` | `plistlib`, `LAUNCHCTL = "/bin/launchctl"`, `gui/<uid>` LaunchAgents for chrome/connector children | no launchd. A Windows host must be `gui: false`: no chrome tasks, no connector tasks on the login Keychain path. An equivalent is a per-task interactive scheduled task (`/IT`, user logged on). |
| `child_tasks.py:507-514`, `:1040`, `:1521`, `:1619-1632`, `:3100-3127`; `session.py:309-356` | private dirs and files proven by `st_uid == getuid()` and mode exactly 0700/0600 | `os.getuid` is missing, and `st_mode` on NTFS is 0o666/0o444 whatever the ACL. Every check would refuse or be meaningless. Needs an ACL check (owner SID plus no other ACE) via ctypes `GetNamedSecurityInfo`, or a parsed `icacls` (fragile). |
| `local_gate.py:403` (same pattern) | token file `st_mode & 0o077` must be 0 | always refuses on Windows (see 4.1) |
| `child_tasks.py:313` `LOGIN_ENV_ALLOW`; `provider/child_env.py` | env allowlist `HOME, PATH, USER, SHELL, TMPDIR, …` | Windows processes need `SystemRoot`, `COMSPEC`, `PATHEXT`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TEMP`/`TMP`, `WINDIR`. Without `SystemRoot`, Python's sockets and `subprocess` break. |
| `child_tasks.py:890`, `session.py:131` (`SYSTEM_PYTHONS = ("/usr/bin/python3",)`), `session.py:182-196`, `:295` | boundary-hook and orchestrator-gate hook commands are `python3 …`, shell-quoted | Claude Code on Windows runs hooks and its Bash tool through **Git Bash**. `python3` there may be the Microsoft Store alias stub. Needs `<py>` written as a Git Bash path, with quoting checked. |
| `workspaces.py:94`, `:320`, `:437-442`, `:1641` | `fcntl.flock` on the repo lock, `cleanup.lock` and `index.jsonl` | as above |
| `workspaces.py:1663-1694`, `:1719-1721`, `:1776-1779`, `:2003-2034`, `:2054-2059`, `:2125-2193`, `:2240-2255`, `:2423` | the archive cleanup's safety argument: `O_DIRECTORY|O_NOFOLLOW` directory fds, `os.stat/rmdir/unlink/rename(..., dir_fd=…)`, `(st_dev, st_ino)` identity across a rename into a quarantine | `dir_fd` raises `NotImplementedError`. **This is the deletion path that took several review rounds (#93, #102).** On Windows it must be re-derived: an open directory handle without `FILE_SHARE_DELETE` blocks renames (a different guarantee), and junctions and other reparse points replace symlinks as the threat. |
| `workspaces.py:1525`, `:1578` | `os.rename` of a whole worktree active → archive → held | fails with a sharing violation if anything still has a file open in the tree (an editor, an indexer, antivirus, a leftover node process). Today a failed move holds the task, so it fails safe, but it would hold often. |
| `workspaces.py:155-190` | `.git` aliases on HFS+/APFS (case, ignorable code points) | NTFS has its own: 8.3 short names (`GIT~1`), trailing dots and spaces, alternate data streams (`.git::$INDEX_ALLOCATION`), i.e. git's `is_ntfs_dotgit` |
| `workspaces.py:1220`, `:1337` | writer-quiescence fingerprint uses `st_ctime_ns` and `st_ino` | on Windows `st_ctime` is the **creation** time, not inode-change time, so metadata writes go unseen and quiescence is weaker |
| `child_tasks.py:949`, `exec_hosts.py` `short_hostname()` | `socket.gethostname()` matched against `hostnames` | works (the PC reports its NetBIOS name) |

The git calls themselves (`workspaces.py` `GIT = "git"`, `worktree add/repair`, `-c gc.auto=0`) work with Git for
Windows. Worktrees on NTFS work, and `core.longpaths` may be needed under `<root>\active\<task_id>\…`.

### 3.2 What Windows support would need

| route | what it is | rough size | risk | notes |
| --- | --- | --- | --- | --- |
| **B0: no exec host** (today's practice) | An Air child drives the PC with pre-approved `ssh <pc> …` commands in `dontAsk` mode | 0 code; a prompt pattern | low | Proven overnight. The child runs on the Air, so the PC holds no router credential. Nothing on the PC outlives one ssh call unless a scheduled task is used, and the auto-mode classifier refuses that ("Unverifiable Deletion Scope"). |
| **B1: native Windows Python** (cmd or PowerShell as the remote shell) | A platform layer (`hostos.py`: lock, detach, job-object tree kill, process start time, private-path ACL check, env allowlist) plus a Windows branch of the workspace mover and cleanup | **~1,500–2,500 changed lines with tests; 2–4 weeks including adversarial review rounds** | high | Touches the most reviewed invariants: process-group cancel, pid-reuse safety, quarantine-then-delete. Each needs its own Windows argument and Windows-only tests that the Macs cannot run, so CI or a test run on the PC is needed. |
| **B2: Git Bash as the remote shell** | Set OpenSSH `DefaultShell` to Git Bash so `shlex.quote` works | saves ~50 lines of B1 | high (same as B1) | **Does not help the Python code**: Python under Git Bash is still native Windows Python (`os.name == "nt"`). It only fixes quoting. MSYS2/Cygwin Python would give `fcntl` and `killpg` emulation, but Cygwin signals do not reach native `claude.exe`/node process trees and paths need converting both ways. ~300–600 brittle lines; not recommended. |
| **B3: WSL2 shim** | Reinstall WSL2. The exec host runs Linux Python and Linux Claude Code inside it, and the existing POSIX code runs almost unchanged (`gui: false`; `/bin/ps -o lstart` exists on Ubuntu with merged /usr) | **~50–150 lines** (remote command via `wsl.exe -e` or an sshd inside WSL, hostnames) plus ops | medium | The owner removed WSL; reinstalling is `[ESCALATE]`. Ops: keep the distro alive between ssh calls (systemd, `vmIdleTimeout`; unverified on this build), mirrored networking or Tailscale inside WSL. **RAM: the WSL VM's memory limit competes with Strata's 18.75 GiB of host-RAM experts on a 32 GB machine.** A second OS to patch. |

## 4. Question 2: the PC as a model server

### 4.1 How the provider reaches Strata

S1 does **not** need to reach the PC. It scores the request on the Studio. Only the provider on the Air relays turns to
a local node. Options for that relay:

| option | how | auth | what is exposed on the tailnet | effort | verdict |
| --- | --- | --- | --- | --- | --- |
| **G: `local_gate.py` on the PC** (as on the Pro) | Strata on `127.0.0.1:8080`; the gate on `<pc-ts-ip>:11435`, upstream `http://127.0.0.1:8080`, new backend `strata` | the gate's bearer token (constant-time compare; a fixed 401 before the body is read), unique to this node | **two paths only**: `GET /api/tags`, `POST /v1/messages`, plus the gate's own `/cortados/local-switch` | port ~200–350 lines + `strata` backend ~100–150 + tests | **recommended** |
| R: gate on the Air, Strata on the tailnet | `strata launch --access tailscale` (Strata's own key); the gate or the provider relays over the tailnet | Strata's API key, held on the Air | Strata's **whole surface**: load/unload, `/v1/vram`, `/slots` save/restore (file writes), `/mcp` server-side tools, metrics | ~100–150 lines (`strata` backend only), no Windows port | Fast, but it gives up the "one door per node" property. The switch and power state sit on the wrong machine, and the security rests on a 0.2.x project's key check. |
| S: `tailscale serve` | tailnet TCP/HTTPS → `127.0.0.1:8080` | tailnet identity (ACLs) and Strata's key | the whole surface | config only | Same exposure as R. No `/api/tags` adaptation, so it still needs a `strata` backend somewhere. |
| P: `netsh interface portproxy` | `<pc-ts-ip>:8080` → `127.0.0.1:8080` | Strata's key | the whole surface; Strata sees every caller as loopback, which **defeats its host check** | admin config | not recommended |

**The Windows port of the gate (option G)** is bounded. The gate is a stdlib `ThreadingHTTPServer` and needs no
fork, signals or worktrees:

- `local_gate.py:45` `import fcntl`. Used for the epoch file's flock (`next_epoch`, `local_gate.py:576`) and the
  Studio's S1-training node lock (`:986-1004`, via `studio_capacity.py:12`). On the PC: `msvcrt.locking` for the epoch
  file, and the Studio-gate lock **off** (the PC runs no S1 training). Today the CLI cannot turn it off: an empty
  `--studio-gate-dir` falls back to the default (`:1286`), so it needs an explicit flag.
- `local_gate.py:403` token-file mode check. On Windows it needs an ACL check: the owner SID only, no
  Everyone/Users/Authenticated Users ACE.
- `local_gate.py:912-917` → `local_resources.pro_local_allowed` → `pmset` (`local_resources.py:48`). On Windows that
  check fails, so it answers "power check failed" and **refuses every request**. It needs a node policy: switch file
  plus `GetSystemPowerStatus` (a desktop reports AC), plus the owner-activity rule in 4.4.
- `local_governor.py:7-9` (`ioreg`, `sysctl`). The governor is optional (only with a `governor` block). Unreadable
  inputs count as owner active and pressure unknown, so the count drops to 1, which is safe. With Strata at
  `--parallel 1` the PC does not need it at first.
- The vllm-mlx retirer and fence (#126) are vllm-mlx only and do not apply.

**New backend `strata` in `local_backend.py`** (`BACKENDS`, `UPSTREAM`, `LOADED`, `local_backend.py:52-56`):

| client route (unchanged) | strata upstream | adaptation |
| --- | --- | --- |
| `GET /api/tags` | `GET /v1/models` | `models_to_tags`, as for vllm-mlx. A model Strata lists as `unloaded` (idle unload) must still count as listed, but the probe should report it so the provider can set expectations for a ~3 min first load. |
| `POST /v1/messages` | `POST /v1/messages` | **Enforce the served model name** (Strata serves any name; return a clean 404 for another model, as vllm-mlx does). Pass `thinking` through (Strata's `anthropic_thinking: "on_request"` maps a request without thinking to thinking off). Rewrap non-Anthropic errors. Map Strata's 503 "GPU in use" and its 429 queue timeout to the gate's retryable 529. |
| switch status "models loaded" | `GET /v1/models` (the `unloaded` flag) or `GET /v1/status` (`loaded`) | |

Strata must be started with `"anthropic_thinking": "on_request"` (needed for Claude Code's short helper calls) and a
fixed `aliases` entry equal to the id in `resources.json`.

### 4.2 How the router registers it

In the Air's git-ignored `tables/resources.json`, add a node with an `ollama` block. The block name is historical: it
means "a gate-fronted Anthropic node". Shape (placeholders):

```json
{"id": "pc", "role": "worker", "target": null, "pool_eligible": false,
 "inventory": {"gpu": "RTX 3060", "vram_gib": 12, "host_ram_gib": 32, "memory_scope": "system"},
 "ollama": {"base_url_env": "CORTADOS_LOCAL_PC_GATE_URL", "token_file_env": "CORTADOS_LOCAL_PC_GATE_TOKEN_FILE",
            "label": "PC", "auto_route": false, "index_route": false, "max_inflight": 1,
            "probe_timeout_s": 2, "connect_timeout_s": 3, "read_timeout_s": 300,
            "models": [{"id": "Qwen3.6-35B-A3B", "alias": "qwen36-35b", "name": "Qwen3.6 35B-A3B (UD-Q4_K_XL, RTX 3060)",
                        "context_length": 49152, "thinking": "off-unless-asked"}]}}
```

- **Model id**: the alias Strata is started with (Strata's `aliases` config key) and exactly what the gate lets
  through. Pin names must be unique across nodes, so the id must differ from the Pro's tags.
- **Context**: `context_length` is the provider's input-fit gate. It must be ≤ the context Strata was launched with.
  Measured: 49,152 works. 63,488 is Strata's ceiling at Q4 with 12 GB and was not benchmarked. `[ESCALATE]` 48K
  (measured, recommended) or ~60K (more room, less VRAM headroom while gaming apps run).
- **Health probe**: the provider's existing `GET /api/tags` through the gate (2 s probe, 3 s connect; a failure is
  remembered 30 s, `routing.AUTO_DOWN_S`, `provider/routing.py:833`). No new code. The gate maps it to Strata's
  `/v1/models`.
- **`max_inflight: 1`**: Strata runs one request at a time on 12 GB. A second turn makes the node "busy" and S1's
  cloud route stands, which is the existing behaviour.
- **Capability scores**: a proxy-index variant in `tables/local-models.json` → `index_proxies.variants`, built by
  `tables/benchmarks/build_tiers.py` (never hand-edit `*.generated.json`):
  - `{"variant": "qwen36-35b-think", "id": "Qwen3.6-35B-A3B", "thinking": "on", "published": "qwen3-6-35b-a3b", "deduct": <d>}`
  - optionally `{"variant": "qwen36-35b", ..., "thinking": "off", "published": "qwen3-6-35b-a3b-non-reasoning", ...}`.
  The non-reasoning entry has only Artificial Analysis's rows, so the builder's ridge pulls it toward average.
  `[ESCALATE]` the deduction: the Pro's copy took −10% for Q4_K_M plus abliteration. Unsloth's UD-Q4_K_XL is close
  to lossless and not abliterated, so −5% is a defensible proxy. The owner sets it.
  - **The Coder gets no proxy.** IQ1_M is ~1.75 bits per weight. Borrowing full-precision `qwen3-8-flash-next`'s
    39.8/73.1 would route real work on a score this copy does not have, and the repo's rule is "do not guess". It
    goes in `not_mapped` with that reason, pin-only, until a measured index exists.
- **VRAM/RAM pressure**: feed admission at the node, not through the 60 s fleet sampler.
  1. Strata's `min_free_vram_mib` plus `idle_unload_s` refuse or unload when another program (a game) holds VRAM. The
     gate turns that refusal into a retryable 529, and the provider routes elsewhere.
  2. The gate's Windows node policy refuses while the owner is active (4.4) or while `GlobalMemoryStatusEx` reports
     memory load above a threshold. That is the Windows counterpart of the governor's pressure input.
  3. Optionally, a Windows branch of `local_resources.collect_node` for the dashboard. It needs `GlobalMemoryStatusEx`
     via ctypes and a guard for `os.getloadavg` (`local_resources.py:146-149`, an `AttributeError` that is not caught).
     `nvidia-smi` already works. Remote sampling would run `python3 - --collect` (`local_resources.py:220`) through
     cmd, where `python3` may be the Store stub, so it needs a per-node interpreter. The fleet sampler does not gate the
     local lane today (`deployments` is empty), so this is display only.

### 4.3 Which work should go to the PC

- **Qwen3.6-35B-A3B (30.8 tok/s, 48K):** short, self-contained, non-outward R0 work, and R1 at low D once the index
  route is on: titles, summaries, classification, capture/log, small code questions and `route_task`-sized analyses.
  Not long Claude Code agent sessions: 48K leaves little room after Claude Code's own prompt, and prompt reading is
  ~600 tok/s.
  - It is **not** a sensitive-work node: the model is not abliterated and refuses like any instruct model. The Pro
    stays the sensitive lane.
  - Its index with thinking on is about equal to the Pro's `qwen-27b` with thinking off. Where both qualify, the
    "lighter first" rule picks between them. The Pro is on battery or switched off much of the time, so the PC would
    take most local work.
- **Qwen3.8 Coder ~125B IQ1_M (5.8 tok/s, 4.8 s TTFT, 32K):** **batch only, or not at all.** Strata serves one model,
  and switching costs a restart plus minutes of load, so it cannot sit beside the 35B. A batch lane would mean a
  scheduled window (e.g. nights) in which the PC serves the Coder instead, for work nobody waits on. There is no proxy
  index (above) and no measured quality at IQ1_M. `[ESCALATE]` whether to keep it at all. Recommendation: leave it
  out of the router until a measured coding eval at IQ1_M exists.

### 4.4 Gaming and owner activity

This is the owner's daily desktop. A loaded 35B takes ~11.6 of 12 GiB of VRAM and ~19 GiB of host RAM. While the owner
games, the model either blocks the game or must be unloaded.

- **The PC is a desktop**, so the Pro's "AC power" rule means nothing here.
- The owner-activity signal that matters is "is the owner using the PC", which needs a policy.
- Windows's idle time (`GetLastInputInfo`) is **per session**. A gate running in session 0 (a service, or a task "run
  whether logged on or not") sees no user input. It needs `WTSQuerySessionInformation` for the console session, or a
  small helper in the user session.
- `[ESCALATE]` when the PC may serve:
  - (a) always, with Strata's `min_free_vram_mib` as the only guard;
  - (b) only while the owner is idle for N minutes;
  - (c) on a schedule (e.g. nights and weekdays 9–5).

  Recommendation: (b) plus the VRAM guard, with `idle_unload_s` ~600 so a game gets the card back.

## 5. Question 3: keeping it running

### 5.1 Across reboots and logins, with nobody there

| option | for | against |
| --- | --- | --- |
| **Scheduled task "At startup", "Run whether user is logged on or not", Priority Normal (task XML `<Priority>5</Priority>`; default 7), "Run with highest privileges", restart on failure every 1 min** | Strata documents it, including the throttling fix. Built in. Runs with nobody logged on. | Session 0: no idle-time signal (4.4). Registering it is a PC change for the owner to make **by hand** (the auto-mode classifier refuses an agent doing it). |
| NSSM Windows service | auto-restart, log rotation | a third-party binary as a service. Strata saw service-like contexts throttle expert loads. `SeLockMemoryPrivilege` must be granted to the service account. No gain over the task. |
| "At logon" task | sees the user's input | needs someone logged in, or auto-logon (a stored password: weaker). It dies at logoff. |
| `strata runtime start` | Strata's own background host | Strata's docs say Windows owner IPC/ACL is "still target", so not ready for unattended use |

**Recommended**: two startup tasks, `CortadOS-Strata` (Strata on loopback, fixed alias, 48K, `--parallel 1`) and
`CortadOS-Gate` (the gate on `<pc-ts-ip>:11435`). Both run as a **dedicated non-admin account** `[ESCALATE]`, Priority
Normal, restart on failure. Strata's "highest privileges" is only for large pages; check whether loading is still fast
without it before granting it. The owner also sets:

- `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0` (or a scheduled sleep window).
  The PC was asleep when this doc was written.
- Windows Update active hours, so reboots happen at known times.
- Tailscale "run unattended" (on Windows it otherwise logs out with the user).

The gate must start after Tailscale has its address. Either use a task trigger delay or bind and retry: the gate
refuses `0.0.0.0` without `--allow-any`, and that must stay.

### 5.2 How the router notices the PC is off, asleep or busy

Nothing new is needed on the provider. The Pro's path already covers it:

- **Asleep or off the tailnet**: the probe connect fails within `connect_timeout_s` (3 s). The node is marked down for
  30 s, so the index route skips it (logged `local index route: … not taken …`), and S1's cloud route stands. A pin
  gets a 503 that names the node. A first conversation after the PC sleeps pays up to ~3 s once per 30 s window.
- **Switch off / owner active / VRAM taken**: the gate answers the probe 503 (switch, activity policy) or relays
  Strata's refusal as a retryable 529. Either way the request goes to another lane.
- **Busy**: `max_inflight: 1`, as above.
- **Mid-conversation**: an auto-routed conversation stays bound to the node and fails with the routed-conversation 503
  ("start a new conversation or send `x-cortados-exclude-model`"). Whether to re-route mid-conversation is **already
  pending as an owner decision** for the Pro (docs/LOCAL-RESOURCES.md, "Local auto-route"). On a gaming PC that
  decision matters more. `[ESCALATE]` (the existing one).
- **The manual switch**: `local_switch` / `/cortados-local` with `node: "pc"` works as soon as the ported gate serves
  `/cortados/local-switch`. The provider side (`provider/local_switch.py`) already takes a node id.

## 6. Question 4: fit with the existing pieces

- **Yes: the PC is a second local node beside the Pro**, in the same `resources.json`, behind the same kind of gate,
  relayed by the same `local_lane`. The provider already loops over nodes in config order. Pins, the index route,
  `max_inflight`, `AUTO_DOWN_S` and the switch are all per node.
- **The Pro's vllm-mlx node** keeps everything it has: sensitive work, abliterated models, the governor, the #126 fence.
  The PC needs none of the vllm-mlx machinery.
- **Ollama**: not used on the PC. The gate's `ollama` default stays for any node without a backend file.
- **The Studio S1 node**: unchanged. The PC does not score: S1 is an MLX model on the Studio, and porting S1 to CUDA is
  out of scope. Like the Studio, the PC is a compute node that is not a task host (the 2026-11-13 precedent).
- **`local: sensitive|prefer`**: today `prefer` follows the **sensitive** rules, so it only reaches nodes with a
  `sensitive` section. The PC should not get one (its model refuses sensitive work), so `prefer` would never reach it.
  `[ESCALATE]` whether `prefer` should mean "any eligible local node" (a small `routing.py` change) or stay as it is.
- **Option C** (PC as both a node and an exec host): children would be routed by S1 and could land on the PC's own
  model through the provider. That loop works, but the PC's RAM is the limit (2.3).

## 7. Question 5: security

- **Tailnet-only exposure**: Strata stays on `127.0.0.1` (its default; never `--access lan|tailscale` in option G). The
  gate binds only `<pc-ts-ip>` and never `0.0.0.0`.
  - Add a **Tailscale ACL**: only the provider host (the Air) may reach `<pc>:11435`. Only the Pro and Air may reach
    `<pc>:22`. The PC may reach only the provider's port and the internet.
  - The tailnet includes a device shared in by another user. Today's default ACL lets every device reach every port,
    and the gate token is then the only control. `[ESCALATE]`: write ACLs (they also protect the Pro's gate).
- **Keys and tokens**:
  - A **new** gate token (`openssl rand -hex 32`) unique to the PC. **Never reuse the Pro's**: the provider presents
    the node's token to that node on every call, so a compromised PC learns its own token and nothing else.
  - The token is stored on the PC under an ACL that allows only the gate's account, and on the Air under 0600.
  - Never printed: acceptance commands below read it with `curl -H @file`, so it never appears in argv.
  - Nothing private in git: `resources.json` is git-ignored; the example uses placeholders.
- **The router's own provider token**:
  - Option A puts **no** provider token on the PC. The PC only answers; it never calls the provider.
  - Option B/C would put a provider token on the PC (`provider_url` + `~/.config/cortados/provider-token` there).
    Today that is the **owner token**: it opens every model endpoint, i.e. every subscription, plus the dashboards and
    the local switch of every node. `provider/clients.py` scopes cover read views and heartbeats only.
  - `[ESCALATE]` before any B: a new scoped client token (e.g. `models:use`, limited to `/v1/messages` and
    `/v1/responses`, with a per-client rate cap) for exec hosts that are not owner laptops.
- **What a compromised PC could reach**:
  - Under A: the **contents of every conversation the provider sends it**. Index routing sends any qualifying R0/R1
    conversation, and tool results in it may hold code or secrets.
  - Its replies drive the client's tools. The effect boundary (`provider/effect_boundary.py`, client
    `boundary_hook.py`) limits a local-routed turn: the cost profile allows Bash with best-effort outward patterns,
    and only the sensitive profile has no shell. A hostile node is therefore a prompt-injection source with Bash on
    the client. `[ESCALATE]`: run PC-routed turns under the stricter profile (no shell) at first.
  - Under B/C, it could also reach the provider token's whole scope, the git credentials used to push task branches,
    any connector OAuth tokens, and anything the PC's ssh keys reach. The PC must hold **no** key to the other Macs.
  - Inbound: the requesting machines hold a key that is **administrator** on the PC. A compromised Air would own the
    PC. Use a **separate non-admin account and key** for anything automated (the gate tasks; B's ssh), and keep the
    admin key for the owner.
  - It is a gaming PC with a kernel anti-cheat driver, a remote-desktop host and many third-party apps: a larger
    attack surface than the Macs. That is another reason to give it the least credential (A: none outbound).
- **Strata itself**: a fast-moving 0.2.x project. It was installed with `irm | iex`, downloads engines from GitHub, and
  has server-side MCP tools and file-writing slot endpoints. Option G keeps all of that off the network. Pin the
  version: the owner updates it by hand and re-runs the step 2 acceptance check.

## 8. Options compared

| | A: model server only | B: exec host only | C: both |
| --- | --- | --- | --- |
| **What it unlocks** | A free local lane that is on when the Pro is off (Pro on battery or travelling); short R0/R1 work off subscription quota; a second node for `local_switch`; later, a measured index | A third task host for parallel children; Windows- and CUDA-specific tasks (e.g. Strata benches) launched with `start_task` | Both, plus children that use the PC's own model |
| **Effort** | Gate port ~200–350 lines + `strata` backend ~100–150 + tests; config; two scheduled tasks; ~3–5 days incl. review | B1 native ~1.5–2.5k lines, 2–4 weeks; B3 WSL2 ~50–150 lines + WSL ops; B0 zero | A + B |
| **Risk** | Low to the router: provider code is unchanged, only gate/backend code that runs on the PC changes. Medium operationally (gaming contention, sleep). | High (B1: the cancel and deletion invariants) or medium (B3: RAM, a WSL the owner removed). A provider token on a gaming PC. | Highest: RAM contention between children and Strata's 19 GiB of experts on 32 GB |
| **Needs from the owner** | the 5.1 PC settings, the ACL, the index decisions | the token-scope decision, a non-admin account, WSL or not | all |

## 9. Recommended order, with an acceptance check per step

Each step is the owner's to approve. The orchestrator delegates the code steps. Steps marked **owner** change the PC or
the live config by hand.

**Step 0 (owner, PC): stop the PC sleeping on AC, and make Tailscale run unattended.**

```sh
ssh <pc> "powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE" | grep -i "AC Power Setting Index"   # 0x00000000
/Applications/Tailscale.app/Contents/MacOS/Tailscale status | grep -i windows                          # PC "active", not "offline"
```

**Step 1 (code, this repo): Windows port of the gate plus the `strata` backend** (stdlib only; fakes only in tests;
macOS behaviour unchanged).
- A `node_policy` seam in place of `pro_local_allowed` on Windows.
- `msvcrt` locking for the epoch file.
- An ACL token-file check on Windows.
- An explicit way to turn off the Studio-gate lock.
- `strata` in `BACKENDS`/`UPSTREAM`/`LOADED`, with model-name enforcement and error mapping.

```sh
python3 -B -m unittest discover -s tests -t .                                   # full suite green on the Pro
python3 -B -m unittest tests.test_local_gate tests.test_local_backend          # new strata/Windows-seam cases included
```

**Step 2 (owner, PC): Strata at startup on loopback**, as task `CortadOS-Strata` (5.1), with alias `Qwen3.6-35B-A3B`,
48K, `--parallel 1`, `anthropic_thinking: on_request`, `idle_unload_s`/`min_free_vram_mib` per 4.4. Reboot, wait
~4 minutes, then run (the second command checks the Anthropic tool-call path the relay depends on):

```sh
ssh <pc> "curl.exe -s http://127.0.0.1:8080/v1/models"                          # lists Qwen3.6-35B-A3B
printf '%s' '{"model":"Qwen3.6-35B-A3B","max_tokens":256,"stream":false,"tools":[{"name":"get_time","description":"Get the time","input_schema":{"type":"object","properties":{}}}],"messages":[{"role":"user","content":"Call get_time."}]}' \
  | ssh <pc> "curl.exe -s -H content-type:application/json --data-binary @- http://127.0.0.1:8080/v1/messages" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["stop_reason"], [b["type"] for b in r["content"]])'
# expect: tool_use [... 'tool_use']
ssh <pc> "powershell -NoProfile -Command \"(Get-NetTCPConnection -State Listen -LocalPort 8080).LocalAddress\""   # 127.0.0.1 only
```

**Step 3 (owner, PC): the gate at startup**, as task `CortadOS-Gate`, with a new token, the dedicated account and the
ACL (section 7). From the Air (the token is passed as a header file, never in argv):

```sh
H=$(mktemp); chmod 600 "$H"; printf 'Authorization: Bearer %s\n' "$(cat ~/.config/cortados/pc-gate-token)" > "$H"
curl -s -o /dev/null -w '%{http_code}\n' -H @"$H" http://<pc-ts-ip>:11435/api/tags          # 200
curl -s -o /dev/null -w '%{http_code}\n'            http://<pc-ts-ip>:11435/api/tags          # 401
curl -s -o /dev/null -w '%{http_code}\n' -H @"$H" -X POST http://<pc-ts-ip>:11435/v1/unload  # 404 (not forwarded)
curl -s -H @"$H" http://<pc-ts-ip>:11435/cortados/local-switch                               # switch/allowed/reason JSON
rm -f "$H"
```

**Step 4 (owner, Air): register the node pin-only** (`index_route: false`; block in 4.2; env vars in the provider's
LaunchAgent; provider restart by the owner). Then a pinned call from any session, e.g. `route_task` with
`model: "sub/qwen36-35b"`, and a read-only check on the Air:

```sh
sqlite3 -readonly ~/.cortados/router.db "select lane, provider, model from routes order by rowid desc limit 3"   # lane local-pc
```

**Step 5 (owner, PC/Air): offline behaviour.** Put the PC to sleep (or stop `CortadOS-Gate`), start a new routed
conversation, and check that it goes to the cloud with a logged skip rather than an error:

```sh
grep -E "local (index route|auto-route): .*pc.* not taken" <provider log on the Air> | tail -3
```

**Step 6 (code + owner): selection by proxy index**, after a week of pinned use. Add the variant(s) in 4.2 with the
owner's deduction, rebuild, review the placement table, then set `index_route: true`:

```sh
python3 -B tables/benchmarks/build_tiers.py --keep-sentiment
python3 -c 'import json; d=json.load(open("tables/model-scores.generated.json")); print([(m["family"], m["index"]["general"]) for m in d["local_models"]])'
python3 -B -m unittest discover -s tests -t .
```

**Step 7 (later, only if B is wanted): decide B3 (WSL2) or B1 (native) first**, then the scoped token. No acceptance
check until those decisions are made. Until then, B0: an Air child with pre-approved `ssh <pc> …` commands in `dontAsk`
mode.

## 10. `[ESCALATE]` decisions for the owner

1. **Which option**: A only (recommended), B, or C.
2. **When the PC may serve**: always, owner-idle only (recommended, with the VRAM guard), or a schedule. Also
   `idle_unload_s` (fast first answer vs a free GPU for games).
3. **Context for the 35B**: 48K (measured, recommended) or up to 63,488.
4. **Proxy-index deduction** for Qwen3.6-35B-A3B UD-Q4_K_XL (suggested −5%), whether to add the non-thinking
   variant, and which bands once on (start R0).
5. **The Coder IQ1_M**: leave it out (recommended), pin-only, or a scheduled batch window.
6. **Accounts and keys**: a dedicated non-admin Windows account for the gate (and any B), with its own key. The
   current administrator key stays the owner's own.
7. **Tailscale ACLs** restricting who reaches the PC's gate and ssh (and the Pro's gate). The tailnet has a shared
   device from another user.
8. **Effect profile for PC-routed turns**: the strict (no shell) profile at first, or the same cost profile as the
   Pro.
9. **`local: prefer`**: keep it on sensitive-section nodes only, or let it mean any eligible local node.
10. **Mid-conversation re-route** when a local node goes away (already pending for the Pro; matters more for a gaming
    PC).
11. **Before any B**: a scoped `models:use` provider token, and WSL2 (reinstall) vs a native port.
12. **Large pages / "highest privileges"** for the Strata task (Strata's documented fast-load setting) vs a plain
    non-admin task.

## 11. What this doc does not claim

- No live reading of the PC today: it was offline. Every PC number is from the 2026-11-23/24 runs.
- Not verified: whether Windows OpenSSH's job object allows breakaway; whether WSL2 on this build keeps a distro alive
  between ssh calls; Strata's `/v1/messages` with real Claude Code bodies (step 2 checks one tool call, not a session);
  load speed of a Task Scheduler start on this PC.
- Line numbers are from `main` at `69d4e2c`.
