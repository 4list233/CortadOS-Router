# The client-side boundary hook

Owner decision 2026-11-14, from the review of `0961c80` (gpt-6.1-sol, finding 2, HIGH): **client-side symlinks escape
the project.** The effect boundary (`cortados_router/provider/effect_boundary.py`, docs/LOCAL-RESOURCES.md "Effect
boundary") checks a local-routed turn's file writes with `os.path.realpath` on the **provider's** filesystem (the Air).
The tools run on the **client** (often the MacBook Pro). A link that exists only there (`/repo/escape -> /outside`)
passes the provider's check. The fix is a check on the client machine, with the provider kept as the authority.

## Parts

| Part | Where | What it does |
|---|---|---|
| Minted ids + ledger | `effect_boundary.Boundary.mark`, `BoundaryLedger`, `LEDGER` | Every tool call a boundary turn lets through reaches the client under an id the provider mints (`toolu_cxb_` + 24 hex), never the node's own id. The provider records it per conversation: tool, profile, the project folder it assumed. Bounded: 256 per conversation, 4096 in all, oldest first. Each record expires after 1 h. Metadata only. Applies to JSON replies, streams (the held `content_block_start` is rewritten) and the refusal fallback (`local_lane.complete`). |
| Check endpoint | `POST /v1/cortados/boundary/check` (`server._boundary_check`, `effect_boundary.check_answer`) | Owner token. Body `{"tool_use_id": "<id>"}` (1-128 of `[A-Za-z0-9_-]`, else 400). Answers `{"known": false, "marked": <id has the prefix>}` or `{"known": true, "tool", "profile", "project", "bash": <shell allowed>, "write_tools": {tool: input key}, "protected": <PROTECTED_PATHS for the profile>, "expires_in"}`. Never a tool input. |
| The hook | `cortados_router/boundary_hook.py` (stdlib, standalone) | Claude Code PreToolUse command hook, matcher `Write\|Edit\|MultiEdit\|NotebookEdit\|Bash`. A denial is exit 2 with the reason on stderr. |
| Hook header | `x-cortados-boundary-hook: 1` (`effect_boundary.HOOK_HEADER`) | Sent by every client that installs the hook, together with it. |
| No hook, no edits | `effect_boundary.Boundary(hook=False)` | A **sensitive** turn without the header is read-only: Edit, MultiEdit, Write and NotebookEdit are not offered, and one the node emits anyway blocks the reply (rule `write-no-hook`). A **cost** turn without it keeps its edits under the provider-side rules only. |

## The hook's decision, per call

1. A tool outside the matcher: allowed (exit 0, no output).
2. `POST <provider>/v1/cortados/boundary/check` with the call's `tool_use_id`. The provider is `CORTADOS_BOUNDARY_URL`
   or else the session's `ANTHROPIC_BASE_URL`. The token is `CORTADOS_BOUNDARY_TOKEN` or else `ANTHROPIC_AUTH_TOKEN` /
   `ANTHROPIC_API_KEY`. Timeout 5 s.
3. `known: false`: allowed when the id is not marked (a cloud-routed turn's call, which is not the boundary's).
   **Denied** when it is marked (the record expired or was never made).
4. `known: true`:
   - the recorded tool must equal the called tool, else **denied**;
   - Bash: allowed only when `bash` is true (cost). On a sensitive profile it is **denied**;
   - a file write: the target (`file_path`, `notebook_path` for NotebookEdit; a relative path is taken from the
     recorded project) and the recorded project are resolved with `os.path.realpath` **on the client**. The real
     target must lie strictly inside the real project folder and match no protected rule the provider sent
     (dotfiles at the project root, `.git` / `.husky` / `.claude` / `.ssh`, CI and launchd pairs, startup files, and on
     sensitive profiles `package.json` / `pyproject.toml` / `Makefile`). Otherwise **denied**. A `~` or `$` path, an
     empty path, or a record with no project folder: **denied**.

## Fail-closed cases

A **marked** id (`toolu_cxb_…`) is denied when:

- the provider is unreachable, times out, answers non-200 (401 included), or sends an unreadable or oversized answer;
- the session has no provider URL or token to ask with;
- the provider has no live record (expired after 1 h, evicted by the bounds, or never minted);
- the record does not match the call (another tool);
- the hook itself crashes (caught at the top level). Claude Code would treat an uncaught exit 1 as a non-blocking error
  and run the tool.

An id **without** the prefix passes on every one of these errors. The provider mints marked ids only on boundary turns,
so its silence about any other id changes nothing. On the provider side, a sensitive turn from a client without the
header fails closed to read-only. The provider's own realpath check (`blocked_write`) stays as defence in depth.

## Isolation

Review finding 2026-11-14 (gpt-6.1-sol, HIGH, reproduced on `f87bd99`): **the hook ran Python from the
model-writable project.** Claude Code runs a command hook with the project as its working directory, and a sensitive
turn may write files there. The mod's `python3 -B -c '<source>'` put the working directory on `sys.path`, so a
`<project>/json.py` the model wrote ran on the hook's next `import json`, with the client's privileges, and could exit
0. `python3` itself came from `PATH`, where a project `bin/`, an activated venv or `node_modules/.bin` can come first.
The rules now:

1. **Isolated mode, no site.** Every installer runs the hook as `python3 -I -S -B`. `-I` keeps the working directory
   and the script's folder off `sys.path` and ignores `PYTHON*` variables and user site-packages. `-S` skips the `site`
   import altogether (review finding 2026-11-14, gpt-6.1-sol, HIGH, reproduced on `ad5ab4c`): `-I` still runs `site`
   for the interpreter's own site-packages, so a `.pth` file there (an editable install pointing at the project root,
   say) put the project on `sys.path`, and `site` then imported a project `sitecustomize.py` the model may write,
   before the hook ran. The hook is stdlib only and needs nothing from site-packages.
2. **No PATH lookup.** The interpreter is an absolute path fixed when the settings are written. Child tasks
   (`session.boundary_hook_python`): the real path of the interpreter running the launcher, else `/usr/bin/python3`.
   The mod: its `hookPython` option, default `/usr/bin/python3`, which must be absolute with no `.`/`..` segment. One
   inside the project folder (lexically or after following links) is refused: the child task is not prepared
   (`SessionError`), the routed subagent is denied. Neither falls back to `PATH`. The mod also refuses a `hookPython`
   that does not exist or is not a file when its host can stat it (`$.fs.stat` reports the file missing); where the host
   has no stat, or the stat fails for another reason (permissions, a sandboxed host), it cannot check and relies on
   the guard (rule 3).
3. **From `/`, inside a shell guard.** Claude Code blocks a call only on hook exit 2; any other non-zero exit is a
   non-blocking error and the call runs. So an interpreter that cannot start failed OPEN (same review, HIGH): a missing
   `hookPython` exits 126/127, and a broken `/usr/bin/python3` shim (the macOS xcrun stub with an invalid inherited
   `DEVELOPER_DIR`) fails before Python starts. The command is now
   `<guard head><python> -I -S -B <script or -c source><guard tail>` (`session.BOUNDARY_HOOK_GUARD_HEAD`/`_TAIL`,
   the same two strings in the mod's `register.ts`, kept equal by `tests/test_boundary_hook.py`). The guard:
   - buffers the event first (`ev=$(/bin/cat)`; a failed read denies every call), then feeds it to
     `(cd / && exec <python> …)` in a **subshell**, so a failed `exec` ends the subshell, not the guard;
   - exit 0 allows, exit 2 (the hook's own denial) is kept;
   - any other status (126, 127, a signal, a crash, a failed `cd /`) is decided from the event's `tool_use_id`
     **field**, never from the prefix appearing elsewhere: the event is flattened (`tr`), and when it holds exactly one
     `"tool_use_id":` key with a plain `[A-Za-z0-9_-]+` value (`grep`, `sed`), an id starting with `toolu_cxb_` is
     denied (reason on stderr, exit 2) and any other allowed (exit 0). Inside a JSON string every quote is escaped, so
     the key pattern only matches real keys, at any depth.
   - when the id cannot be read that way (a second `tool_use_id` key, e.g. inside `tool_input`; a non-string value; a
     missing `grep`/`sed`), it denies if the raw event holds `"toolu_cxb_` anywhere (a shell `case`, no tool needed)
     and allows otherwise. That errs toward a false block, with one known gap: both checks read the id as literal
     text, so an envelope `tool_use_id` written JSON-escaped (e.g. `"toolu_cxb_…"`, which a JSON reader decodes
     to `toolu_cxb_…`) is recognised by neither and is allowed when the interpreter is missing or fails. Claude Code
     writes the envelope's `tool_use_id` itself, from ids it generates as plain ASCII, so no model-controlled path to
     such an escaped id is known; the gap is open only if that ever changes.
   Every helper is named by absolute path (`/bin/cat`, `/usr/bin/tr`, `/usr/bin/grep`, `/usr/bin/sed`), never looked
   up on `PATH`; a helper missing at that path only ever moves the decision to the raw check. The hook never runs in
   the project folder.
4. **Hook location.** The hook's code lives outside any model-writable path: the mod passes the source inline (`-c`, no
   file on disk), child tasks run `boundary_hook.py` from the router checkout running the launcher. `session.py`
   refuses a checkout inside the child's project folder (`--project-dir`, passed by `child_tasks` when the task has a
   `cwd`). At run time the hook checks itself: a known call whose recorded project folder holds the hook's interpreter,
   its file or the file's folder (lexically or real) is **denied**, Bash included.
5. **Imports.** The hook is stdlib only and imports nothing from the project. Run as a program without `-I` (an older
   settings file), it deletes the first `sys.path` entry (the working directory or its own folder) and empty entries
   before importing anything beyond `sys`, and **denies** every marked call: a missing `-I` is a misconfiguration,
   never a quiet pass. Without `-S` (an `ad5ab4c` settings file) it denies every marked call too: by the time it runs,
   `site` may already have run project code.

Tests (`tests/test_boundary_hook.py`, subprocess, fakes only): `IsolationTests` run the child-task command and the mod's
command through `sh` with the project as cwd, a planted `json.py`, `urllib/`, `http/`, `sitecustomize.py` and
`usercustomize.py` there and on `PYTHONPATH`, and a planted `python3` first on `PATH`; none runs, and the marked write
outside the project is still denied. A copy of the script beside planted modules is not hijacked (with `-I -S`; with
`-I` alone and without `-I`, marked calls are denied). A copy inside the project denies even an in-project write.
`SitePthTests` build a throwaway venv outside the project whose site-packages holds a `.pth` naming the project, plus a
project `sitecustomize.py` payload: the `ad5ab4c` command (`-I -B`) runs it and allows the marked write; both current
commands skip it and deny (exit 2). `GuardTests` run both builders through `sh`: a missing interpreter (and a folder,
126) denies a marked call and allows an unmarked one; a marked id only inside `tool_input` with an unmarked
`tool_use_id` is allowed; pretty-printed events with any key order; an unreadable id fails closed only when the prefix
is present; a fake hook exiting 2 keeps 2, exiting 0 allows; a crash (exit 1/3, SIGSEGV, SIGKILL) denies marked and
allows unmarked; a fake broken shim exiting 126 likewise; the real hook's own answers pass through unchanged.
`SelfLocationTests` cover the interpreter and file checks in process; `test_the_mod_builds_its_command_as_mod_command`
keeps the test's command, the guard strings and register.ts in step. `tests/test_session.py` checks the interpreter
choice, the command shape and the project refusals; the mod's `register.test.ts` checks `boundaryHookPython` (including
a missing file) and the command.

## Where the hook is installed

- **CortadOS child tasks, on any host:** `cortados_router/session.py` `settings_content`. The launcher's 0600
  settings file always carries the boundary hook as the first PreToolUse entry
  (`<absolute python3> -I -S -B <checkout>/cortados_router/boundary_hook.py` from `/` inside the shell guard, timeout 30 s;
  "Isolation"), plus `disableAllHooks: false`, so a user
  settings file cannot switch it off. `custom_headers` adds the header to `ANTHROPIC_CUSTOM_HEADERS`. Both are written
  together, so the header never goes out without the hook.
- **The owner's interactive sessions:** the `cortados-routed-subagents` mod (0.4.0 and later). Each routed subagent child gets
  `--settings` from `routedChildSettings()`: the hook as `'<hookPython>' -I -S -B -c '<BOUNDARY_HOOK_PY>'` inside the guard (the file embedded
  verbatim, kept identical by `tests/test_boundary_hook.py`), `disableAllHooks: false`, and this mod and the pane
  disabled. These replace `disableAllHooks: true`, which would also have switched the hook off. The child env carries
  the header and `CORTADOS_ROUTED_SUBAGENT=1`, which keeps the mod's Agent router, gate and directive inert there. The
  main session talks to Claude directly; its own tool calls never come from the provider, so it has no hook.

## Honest limits

- The header is the client's word. If a client sends it and the hook then does not run at all (hooks disabled by a
  managed policy, a Claude Code that ignores the hook, a missing `/bin/sh`), the provider cannot tell. Edits then pass
  under the provider-side rules alone. An interpreter that is missing or cannot start (no `/usr/bin/python3`, the
  macOS developer-tools stub not installed or a bad `DEVELOPER_DIR`) is no longer such a case: the shell guard denies
  every provider-marked call then ("Isolation", rule 3), so sensitive and cost turns lose their edits and Bash until
  the interpreter is fixed.
- A time-of-check/time-of-use race: a link created between the hook's check and the write is not seen.
- A **cost** turn from a client without the hook (e.g. Codex on `/v1/responses`, or an older mod) keeps its edits and
  Bash under the provider-side checks only, as before this change.
- The hook adds one request per Write/Edit/MultiEdit/NotebookEdit/Bash call to the provider in every hooked session,
  cloud-routed turns included. When the provider is unreachable, an unmarked call passes after the 5 s timeout.

## Tests

`tests/test_boundary_hook.py`: a loopback fake check server, plus the hook run through `sh` exactly as the mod runs
it. `tests/test_provider_effect_boundary.py`: `SensitiveWithoutHookTests`, `LedgerUnitTests`,
`BoundaryCheckEndpointTests` (including the provider and the hook end to end against a link the provider never saw),
and `prefer` → cost. `tests/test_session.py`: child settings and header. The mod's own tests:
`claude plugin test integrations/claude-mod/cortados-routed-subagents`.
