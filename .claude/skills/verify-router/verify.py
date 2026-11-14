"""PURPOSE: Drive the real router entrypoint (core.py) on a throwaway CORTADOS_HOME and print an
evidence block. Each scenario reports PASS, FAIL or BLOCKED; a scenario that needs S1 is BLOCKED
(never PASS) while S1 is unreachable, because a degraded decision cannot prove S1-driven routing.
CEILING: Read-only with respect to ~/.cortados and the user's subscriptions. It never runs
scarcity.py, which would open the vendor CLIs, and every child runs with CORTADOS_SCARCITY_REFRESH=off
so a fresh throwaway home cannot schedule a usage sample either.

S1 (#112): verification reaches S1 the way the live router on this host does. CORTADOS_S1_* in this process's
environment win (an operator's deliberate choice); otherwise the S1 names in ~/.cortados/provider.env (the file the live
provider's LaunchAgent loads) are read as data, never executed, and handed to the children; otherwise the router's own
resolver (endpoints file, then loopback). The evidence names the source and never prints the token.

Live home (#112): the live provider writes router.db, provider.out, child-tasks.jsonl, codex-home/ and the usage files
all the time, so a whole-tree timestamp diff fails on that host whatever verification does. Instead: every child runs
with HOME and CORTADOS_HOME both in a throwaway folder, and verification checks only what it could have written: no
`.cortados` appeared under the throwaway HOME, the live router.db holds no new rows tagged by verification's client
(`provider:verify`), and no other file verification wrote into its own homes changed in the live home.
Usage: python3 -B .claude/skills/verify-router/verify.py
"""
import importlib.util, json, os, sqlite3, subprocess, sys, tempfile, urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CORE = REPO / "cortados_router" / "core.py"
LIVE_HOME = Path.home() / ".cortados"
PROVIDER_ENV = LIVE_HOME / "provider.env"
sys.path.insert(0, str(REPO))
from cortados_router.compat import getenv            # noqa: E402
from cortados_router.router_db import reader         # noqa: E402  rw + query_only, the repo's one safe router.db reader (#99)
from cortados_router.s1_endpoint import resolve_url  # noqa: E402  same resolver core uses, so health probe and decision hit one scorer

S1_KEYS = ("CORTADOS_S1_URL", "CORTADOS_S1_ENDPOINT", "CORTADOS_S1_ENDPOINTS", "CORTADOS_S1_TOKEN")
VERIFY_PLATFORM = "provider:verify"   # every routing.choose below passes client="verify"; core logs it as this platform
ROUTER_DB = "router.db"


def _shown(path):
    """`~/...` for a path under HOME, so the evidence block carries no user name."""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def shown_url(url):
    """The scorer URL for the evidence block: loopback as is, any other host masked. The block is pasted into PRs on a
    public repo, and a tailnet address is not something it should carry (tables/s1-endpoints.json is git-ignored for
    the same reason)."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname in ("127.0.0.1", "localhost", "::1") or not parts.hostname:
        return url
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://<private host>{port}{parts.path}"


def _env_parser():
    """The bot gateway's provider.env reader (integrations/bot-gateway/launch.py): data only, never executed, refuses
    shell syntax with a line number and never a name or value. One grammar for that file in this repo."""
    spec = importlib.util.spec_from_file_location("_cortados_launch", REPO / "integrations" / "bot-gateway" / "launch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def s1_config(environ, provider_env=PROVIDER_ENV):
    """(settings, source): the CORTADOS_S1_* names and values to add to every child's environment, and where they came
    from. Empty settings means the children resolve S1 from the inherited environment as is."""
    if any(getenv(k, environ=environ) for k in S1_KEYS):
        return {}, "this process's environment"
    if not Path(provider_env).exists():
        return {}, f"router default (no CORTADOS_S1_* in the environment, no {_shown(provider_env)})"
    launch = _env_parser()
    try:
        values = launch.parse_env(launch.read_secret_file(str(provider_env), "provider env file", 0o022))
    except launch.ConfigError as exc:
        return {}, f"router default ({_shown(provider_env)} not used: {exc})"
    settings = {k: values[k] for k in S1_KEYS if values.get(k)}
    if not settings:
        return {}, f"router default ({_shown(provider_env)} sets no CORTADOS_S1_*)"
    return settings, f"{_shown(provider_env)}, the live provider's config on this host"


def s1_probe(url, token, urlopen=urllib.request.urlopen):
    """(listening, detail) for GET <url>/health. Any HTTP status means a server answered; the decisions below still
    report BLOCKED if it does not score. The detail never carries the token."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urlopen(urllib.request.Request(f"{url}/health", headers=headers), timeout=3) as r:
            return True, f"/health {getattr(r, 'status', 200)}"
    except urllib.error.HTTPError as exc:
        return True, f"/health HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(getattr(exc, 'reason', exc))[:120]}"


def file_stamps(root):
    """{relative path: (mtime_ns, size)} for every file under root ({} when root is missing)."""
    root = Path(root)
    out = {}
    if root.is_dir():
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    st = p.stat()
                    out[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size)
            except OSError:      # a live service removed it between listing and stat
                pass
    return out


def is_router_db(rel):
    return rel == ROUTER_DB or rel.startswith(ROUTER_DB + "-")


def verify_rows(db, connect=reader):
    """Rows in a router.db logged by verification's own client; 0 when the file is missing. Raises sqlite3.Error when the
    database cannot be read."""
    if not Path(db).exists():
        return 0
    c = connect(db, 10.0)
    try:
        return c.execute("SELECT count(*) FROM routes WHERE platform = ?", (VERIFY_PLATFORM,)).fetchone()[0]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
    finally:
        c.close()


def live_home_check(before, after, written, rows_before, rows_after):
    """(verdict, evidence) for "verification left the live home alone". before/after: file_stamps of the live home;
    written: relative paths verification wrote into its own throwaway homes; rows_*: verify_rows of the live router.db
    (None when it could not be read). Only paths verification writes are compared; router.db is shared with the live
    provider, so it is checked by verification's own rows instead of its timestamp."""
    if rows_before is None or rows_after is None:
        return "BLOCKED", "could not read the live router.db to count verification rows"
    changed = sorted(r for r in written if not is_router_db(r) and before.get(r) != after.get(r))
    if changed:
        return "FAIL", f"changed in the live home: {', '.join(changed)}"
    if rows_after != rows_before:
        return "FAIL", f"live router.db gained {rows_after - rows_before} row(s) from platform {VERIFY_PLATFORM}"
    others = sum(1 for r in set(before) | set(after) if before.get(r) != after.get(r))
    files = sorted(r for r in written if not is_router_db(r))
    return "PASS", (f"{_shown(LIVE_HOME)}: no new {VERIFY_PLATFORM} rows in router.db; "
                    f"{len(files)} other path(s) verification writes unchanged{': ' + ', '.join(files) if files else ''}; "
                    f"{others} file(s) changed by the live services, not checked")


def escaped_homes(sandboxes):
    """Throwaway HOMEs in which a `.cortados` appeared: a child that ignored CORTADOS_HOME and fell back to ~/.cortados."""
    return [s for s in sandboxes if (Path(s) / "home" / ".cortados").exists()]


class Runner:
    """Starts every child in its own throwaway folder: <tmp>/home is HOME, <tmp>/state is CORTADOS_HOME."""

    def __init__(self, s1_settings):
        self.s1 = dict(s1_settings)
        self.sandboxes = []

    def env(self, prefix):
        tmp = Path(tempfile.mkdtemp(prefix=prefix))
        (tmp / "home").mkdir()
        (tmp / "state").mkdir()
        self.sandboxes.append(tmp)
        # A fresh home is always "due" for a usage sample, and that sample opens the real vendor CLIs: the switch keeps
        # the child from scheduling one. The probe folder is checked afterwards as evidence that none was opened.
        return {**os.environ, **self.s1, "HOME": str(tmp / "home"), "CORTADOS_HOME": str(tmp / "state"),
                "CORTADOS_SCARCITY_REFRESH": "off"}

    def run(self, argv, prefix="verify-router-", timeout=180):
        return subprocess.run([sys.executable, "-B", *argv], cwd=REPO, env=self.env(prefix), capture_output=True,
                              text=True, timeout=timeout)

    def states(self):
        return [s / "state" for s in self.sandboxes]

    def written(self):
        return set().union(*(file_stamps(s) for s in self.states())) if self.sandboxes else set()


# P2 (2026-10-21): the provider serves Codex lanes (openai-codex rows as codex-cli). The provider's degraded path must
# land on a Codex lane because of the hard rule that a degraded scorer never defaults to Claude; a scored decision
# must come back on a served provider, never the native openai-codex row. Runs the real provider routing on the
# throwaway home with the real core.decide (S1 if up, degraded otherwise); codex_bin is the interpreter so the lane's
# PATH check passes without the real CLI, and no CLI is ever started: routing only chooses.
PROVIDER_SCENARIO = """
import json, sys
from cortados_router import core
from cortados_router.provider import routing
body = {"model": "cortados", "messages": [{"role": "user", "content": "rename the variable in utils.py"}]}
r = routing.choose(core, body, client="verify", conversation_id="verify-router", claude_bin=sys.executable, codex_bin=sys.executable)
print(json.dumps({k: r.get(k) for k in ("provider", "model", "band", "degraded", "error")}))
"""

# Friendly pins (2026-11-08): `sub/<friendly name>` must resolve to the one pool model it means and never guess among
# several. A pin skips S1, so this runs the same whether S1 is up or down. `GPT 6.1` has exactly one served model
# (gpt-6.1-sol, codex-cli); `GPT 6` fits three (astra, luna, sol) and must come back as a client error naming them.
PIN_SCENARIO = """
import json, sys
from cortados_router import core
from cortados_router.provider import routing
def pin(name):
    body = {"model": name, "tools": [{"name": "Read"}], "messages": [{"role": "user", "content": "plan the work"}]}
    r = routing.choose(core, body, client="verify", conversation_id="verify-pin-" + name, claude_bin=sys.executable,
                       codex_bin=sys.executable)
    return {k: r.get(k) for k in ("provider", "model", "pinned", "client_error", "error")}
print(json.dumps({"one": pin("sub/GPT 6.1"), "many": pin("sub/GPT 6")}))
"""

# Local auto-route on the Pro (#75, 2026-11-13): after S1, a conversation in a band the node lists moves to its local model
# only while the node is available; on battery / switch off / gate down it keeps S1's route; an outward (R3) request
# never goes local whatever the config says; a degraded decision never goes local and never to Claude. Runs the real
# provider routing and the real core.decide on a throwaway home and a throwaway local config. The probe is faked in the
# child (no Ollama, no socket); availability is what the Pro's gate would answer.
LOCAL_SCENARIO = """
import json, os, sys, tempfile
from cortados_router import core
from cortados_router.provider import local_lane, routing
cfg = os.path.join(tempfile.mkdtemp(), "resources.json")
json.dump({"nodes": [{"id": "pro", "ollama": {"base_url": "http://127.0.0.1:9",
           "auto_route": {"model": "verify-local", "bands": ["R0", "R1", "R2"]},
           "models": [{"id": "verify/local:tag", "alias": "verify-local", "context_length": 65536}]}}]}, open(cfg, "w"))
os.environ[local_lane.CONFIG_ENV] = cfg
state = {"down": None}
def probe(node):
    if state["down"]:
        raise local_lane.LocalUnavailable(state["down"])
    return {"verify/local:tag"}
local_lane.probe = probe
def route(text, down=None):
    state["down"] = down
    routing._auto_down.clear()
    body = {"model": "cortados", "messages": [{"role": "user", "content": text}]}
    r = routing.choose(core, body, client="verify", conversation_id="verify-local", claude_bin=sys.executable,
                       codex_bin=sys.executable)
    return {k: r.get(k) for k in ("provider", "model", "band", "degraded", "error")}
easy = "rename the variable in utils.py"
print(json.dumps({"up": route(easy), "battery": route(easy, "node pro: local gate: local disabled: on battery"),
                  "outward": route("email the signed invoice to the client and mark it paid")}))
"""


def _last_json(p):
    return json.loads(p.stdout.strip().splitlines()[-1])


def main():
    results = []

    def record(name, verdict, evidence):
        results.append((name, verdict, evidence))

    settings, s1_source = s1_config(os.environ)
    child_env = {**os.environ, **settings}
    try:
        s1_url = resolve_url(environ=child_env)
        up, s1_detail = s1_probe(s1_url, getenv("CORTADOS_S1_TOKEN", environ=child_env))
    except ValueError as exc:            # a broken endpoints config: the children would fail the same way
        s1_url, up, s1_detail = "(unresolved)", False, str(exc)[:200]
    s1_where = f"{shown_url(s1_url)} via {s1_source}"
    unreachable = f"S1 unreachable at {s1_where} ({s1_detail})"

    runner = Runner(settings)
    live_before = file_stamps(LIVE_HOME)
    try:
        rows_before = verify_rows(LIVE_HOME / ROUTER_DB)
    except sqlite3.Error:
        rows_before = None

    def decide(request):
        p = runner.run([str(CORE), "decide", request])
        out = p.stdout
        body = out[: out.rindex("}") + 1] if "}" in out else ""
        return p.returncode, (json.loads(body) if body else None), out.strip().splitlines()[-1:] or [p.stderr]

    p = runner.run([str(CORE), "table"])
    record("table loads and validates", "PASS" if p.returncode == 0 and p.stdout.strip().endswith("ok") else "FAIL",
           p.stdout.strip() or p.stderr.strip())

    code, j, tail = decide("rename the variable in utils.py")
    if j is None:
        record("decision returns", "FAIL", tail)
    else:
        route, dec = j["route"], j["decision"]
        chosen = json.dumps(route.get("chosen"))
        record("decision returns a route", "PASS" if route.get("band") and route.get("chosen") else "FAIL",
               f"band={route.get('band')} chosen={chosen} degraded={dec.get('degraded')}")
        if dec.get("degraded"):
            # Hard rule: a degraded scorer never defaults to Claude (README, core.resolve).
            record("degraded scorer does not default to Claude",
                   "FAIL" if "claude" in chosen.lower() else "PASS", tail[0] if tail else "")

    if up:
        code, j, tail = decide("email the signed invoice to the client and mark it paid")
        band = j and j["route"].get("band")
        if j and j["decision"].get("degraded"):
            # S1 answered HTTP but did not score: a degraded fallback band proves nothing about the outward rule.
            record("outward request floors blast radius (R3)", "BLOCKED",
                   f"S1 listening at {s1_where} but the decision is degraded: {j['decision'].get('error')}")
        else:
            record("outward request floors blast radius (R3)", "PASS" if band == "R3" else "FAIL",
                   f"band={band} | {tail[0] if tail else ''}")
    else:
        record("outward request floors blast radius (R3)", "BLOCKED", unreachable)

    p = runner.run(["-c", PROVIDER_SCENARIO])
    try:
        r = _last_json(p)
    except (ValueError, IndexError):
        record("provider routes a conversation to a served lane", "FAIL", (p.stderr or p.stdout).strip()[-300:])
    else:
        served = r.get("provider") in ("claude-cli", "codex-cli") and not r.get("error")
        if r.get("degraded"):
            record("provider routes a degraded conversation to a Codex lane, never Claude",
                   "PASS" if served and r["provider"] == "codex-cli" else "FAIL", json.dumps(r))
        else:
            record("provider routes a scored conversation to a served lane (never openai-codex itself)",
                   "PASS" if served else "FAIL", json.dumps(r))

    p = runner.run(["-c", PIN_SCENARIO], prefix="verify-router-pin-")
    try:
        r = _last_json(p)
    except (ValueError, IndexError):
        record("friendly pin resolves to one model, ambiguity is refused", "FAIL", (p.stderr or p.stdout).strip()[-300:])
    else:
        one, many = r["one"], r["many"]
        ok_one = (one.get("provider"), one.get("model"), one.get("pinned")) == ("codex-cli", "gpt-6.1-sol", True)
        ok_many = many.get("client_error") and all(c in (many.get("error") or "") for c in ("gpt-6-astra", "gpt-6-luna", "gpt-6-sol")) \
            and not many.get("model")
        record("friendly pin resolves to one model, ambiguity is refused", "PASS" if ok_one and ok_many else "FAIL",
               f"sub/GPT 6.1 -> {one.get('provider')}/{one.get('model')} | sub/GPT 6 -> {(many.get('error') or '')[:120]}")

    p = runner.run(["-c", LOCAL_SCENARIO], prefix="verify-router-local-", timeout=300)
    try:
        r = _last_json(p)
    except (ValueError, IndexError):
        record("local auto-route only when the Pro is available", "FAIL", (p.stderr or p.stdout).strip()[-300:])
    else:
        up_r, bat, out = r["up"], r["battery"], r["outward"]
        served = lambda x: x.get("provider") in ("claude-cli", "codex-cli") and not x.get("error")
        degraded_why = unreachable if not up else f"S1 listening at {s1_where} but the decision is degraded"
        if up_r.get("degraded"):
            # No band of its own: never local, and the degraded rule still keeps it off Claude.
            record("degraded decision is never auto-routed local (nor to Claude)",
                   "PASS" if up_r.get("provider") == "codex-cli" and bat.get("provider") == "codex-cli" else "FAIL",
                   json.dumps(up_r))
            record("local auto-route only when the Pro is available", "BLOCKED", f"no band to auto-route on: {degraded_why}")
        else:
            ok = up_r.get("provider") == "local-pro" and up_r.get("model") == "verify/local:tag" and served(bat)
            record("local auto-route only when the Pro is available", "PASS" if ok else "FAIL",
                   f"available -> {up_r.get('provider')}/{up_r.get('model')} band {up_r.get('band')} | "
                   f"on battery -> {bat.get('provider')}/{bat.get('model')}")
        if out.get("degraded"):
            record("outward (R3) request never goes local", "BLOCKED", degraded_why)
        else:
            record("outward (R3) request never goes local",
                   "PASS" if out.get("band") == "R3" and not str(out.get("provider")).startswith("local-") else "FAIL",
                   f"band={out.get('band')} -> {out.get('provider')}/{out.get('model')}")

    try:
        rows_after = verify_rows(LIVE_HOME / ROUTER_DB)
    except sqlite3.Error:
        rows_after = None
    verdict, evidence = live_home_check(live_before, file_stamps(LIVE_HOME), runner.written(), rows_before, rows_after)
    record("live ~/.cortados untouched by verification", verdict, evidence)
    escaped = escaped_homes(runner.sandboxes)
    record("no child wrote outside its throwaway CORTADOS_HOME", "FAIL" if escaped else "PASS",
           f"{len(escaped)} of {len(runner.sandboxes)} throwaway HOMEs gained a .cortados")
    probes = [s / "usage-probe" for s in runner.states()]
    record("no vendor CLI probe opened", "PASS" if all(not d.exists() or not any(d.iterdir()) for d in probes) else "FAIL",
           f"{len(probes)} throwaway homes checked for usage-probe folders")

    print("## Router verification evidence")
    print(f"repo: {REPO}  python: {sys.version.split()[0]}  S1: {'up' if up else 'down'} ({s1_where}; {s1_detail})")
    for name, verdict, evidence in results:
        print(f"- [{verdict}] {name} — {evidence}")
    verdicts = {v for _, v, _ in results}
    overall = "FAIL" if "FAIL" in verdicts else "BLOCKED" if "BLOCKED" in verdicts else "PASS"
    print(f"overall: {overall}")
    return 1 if overall == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
