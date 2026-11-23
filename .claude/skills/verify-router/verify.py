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

# API credit lane (#114, 2026-11-22): S1-routed work a COST rule moved to local goes to the API credit lane only while a
# grant expires within prefer_within_days (3), the lane is on (key, grants file, ledger, under the 95 % stop line) and the
# request is low-risk; outward or degraded work never does, and a request already past the stop line stays local. Runs
# the real routing.choose tail (sensitive_local, auto_local, the preference) and the real api_credits on a throwaway
# home, a throwaway grants file and a fake key; S1's select is replaced by a fixed S1-shaped route, the node probe is
# faked (no Ollama, no socket) and nothing is sent anywhere.
API_CREDIT_SCENARIO = """
import json, os, sys, tempfile, time
from datetime import datetime, timezone
from cortados_router import api_credits, core, execution_routing
from cortados_router.provider import local_lane, routing
d = tempfile.mkdtemp()
cfg = os.path.join(d, "resources.json")
json.dump({"nodes": [{"id": "pro", "ollama": {"base_url": "http://127.0.0.1:9",
           "auto_route": {"model": "verify-local", "bands": ["R0", "R1", "R2"]},
           "models": [{"id": "verify/local:tag", "alias": "verify-local", "context_length": 65536}]}}]}, open(cfg, "w"))
os.environ[local_lane.CONFIG_ENV] = cfg
os.environ["CORTADOS_API_CREDIT_CONFIG"] = credit = os.path.join(d, "api-credit.json")
os.environ["CORTADOS_ANTHROPIC_API_KEY"] = "verify-fake-key"
local_lane.probe = lambda node: {"verify/local:tag"}
core.log_route = lambda *a, **k: True
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
def grant(days, amount=200):
    json.dump({"org_monthly_limit_usd": 200, "prefer_within_days": 3, "grants": [{"name": "verify",
               "amount_usd": amount, "granted": iso(time.time() - 20 * 86400), "expires": iso(time.time() + days * 86400)}]},
              open(credit, "w"))
def route(band="R2", outward=False, degraded=False, **extra):
    routing._auto_down.clear()
    br = 3 if outward else 0
    r = {"router": "verify", "provider": "codex-cli", "model": "gpt-5.6-terra", "band": band, "route_id": "verify:api",
         "degraded": degraded, "decision": {"answers": {"outward": outward, "blast_radius": br}, "degraded": degraded},
         "route": {"band": band, "chosen": {"lane": "codex", "provider": "codex-cli", "model": "gpt-5.6-terra"}}}
    execution_routing.select = lambda *a, **k: dict(r)
    body = {"model": "cortados", "max_tokens": 256, "messages": [{"role": "user", "content": "rename a variable"}],
            **extra}
    out = routing.choose(core, body, client="verify", conversation_id="verify-api", claude_bin=sys.executable,
                         codex_bin=sys.executable)
    return [out.get("provider"), out.get("model"), out.get("bind_reason")]
grant(2)
res = {"expiring": route(), "expiring_r1": route("R1"), "outward": route("R3", outward=True),
       "degraded": route(degraded=True),
       "fallbacks": route(fallbacks=[{"model": "claude-opus-5", "max_tokens": 128000, "speed": "fast"}])}
# #114 review round 1: every continuation of such a conversation passes the gate before it is forwarded.
cbody = {"model": "cortados", "messages": [{"role": "user", "content": "rename a variable"}]}
ok_check = lambda b, r: (True, "admitted")
out_check = lambda b, r: (False, "outward effect (S1 outward=true): never local")
gate = lambda **kw: routing.api_continuation_admission(cbody, {"route_id": "verify:api", "band": "R2"}, **kw)[0]
res["cont"] = {"clean": gate(local=None, recheck=ok_check), "sensitive": gate(local="sensitive", recheck=ok_check),
               "prefer": gate(local="prefer", recheck=ok_check), "outward": gate(local=None, recheck=out_check),
               "no_check": gate(local=None, recheck=None)}
grant(20)
res["not_expiring"] = route()
res["cont"]["not_expiring"] = gate(local=None, recheck=ok_check)
grant(2, amount=1)
api_credits.reserve(0.95, time.time())       # $0.95 of a $1 grant committed: the 95 % stop line
res["past_stop"] = route()
st = api_credits.state(time.time())
res["stop_state"] = [st["on"], (st["reason"] or "")[:40]]
print(json.dumps(res))
"""

# Local models in selection by a published proxy index (local-published-index, owner 2026-11-13): the REAL generated
# scores (tables/model-scores.generated.json → local_models) place Qwen27b_heretic like a cloud model. Two parts:
#  * S1-free (always runs): the real index_local on S1-shaped routes with the real generated indexes: a requirement the
#    non-thinking variant clears goes local as qwen-27b; one only the think variant clears goes local as qwen-27b-think
#    with max_tokens 8192 and stays on the cloud route with max_tokens 1024; outward work and a degraded decision never
#    go local; a node on battery keeps the cloud route.
#  * S1 (BLOCKED while S1 is down): the real core.decide → the route goes local exactly when a variant's index for the
#    route's use case clears its required index; the outward request never goes local.
# Throwaway local config and a faked probe in the child (no Ollama, no socket); codex_bin/claude_bin are the interpreter.
INDEX_SCENARIO = """
import json, os, sys, tempfile
from cortados_router import core
from cortados_router.provider import local_lane, routing
cfg = os.path.join(tempfile.mkdtemp(), "resources.json")
json.dump({"nodes": [{"id": "pro", "ollama": {"base_url": "http://127.0.0.1:9",
           "models": [{"id": "Qwen27b_heretic:latest", "alias": "qwen-27b", "context_length": 131072}]}}]}, open(cfg, "w"))
os.environ[local_lane.CONFIG_ENV] = cfg
state = {"down": None}
def probe(node):
    if state["down"]:
        raise local_lane.LocalUnavailable(state["down"])
    return {"Qwen27b_heretic:latest"}
local_lane.probe = probe
core.log_route = lambda *a, **k: True
local = {m["family"]: m for m in core.model_scores()["local_models"]}
def synthetic(req, max_tokens, *, outward=False, degraded=False, down=None):
    state["down"] = down
    routing._auto_down.clear()
    br = 3 if outward else 1
    r = {"provider": "codex-cli", "model": "gpt-5.6-terra", "band": "R1", "route_id": "verify:index", "degraded": degraded,
         "decision": {"answers": {"outward": outward, "blast_radius": br}, "degraded": degraded},
         "route": {"band": "R1", "required_index": req, "answers": {"outward": outward}, "features": {"blast_radius": 3 if outward else 1},
                   "chosen": {"lane": "codex", "provider": "codex-cli", "model": "gpt-5.6-terra", "use_case": "code", "cost": 10}}}
    body = {"model": "cortados", "max_tokens": max_tokens, "messages": [{"role": "user", "content": "x"}]}
    out = routing.auto_local(core, body, r, client="verify", conversation_id="verify-index")
    return [out.get("provider"), (out.get("local") or {}).get("model", {}).get("variant")]
non, think = local["qwen-27b"]["index"]["code"], local["qwen-27b-think"]["index"]["code"]
mid = round((non + think) / 2, 1)
syn = {"indexes": {"qwen-27b": non, "qwen-27b-think": think}, "mid": mid,
       "non": synthetic(non, 64), "think": synthetic(mid, 8192), "think_small": synthetic(mid, 1024),
       "outward": synthetic(0.0, 64, outward=True), "degraded": synthetic(0.0, 64, degraded=True),
       "battery": synthetic(non, 64, down="node pro: local gate: local disabled: on battery")}
def real(text, max_tokens):
    state["down"] = None
    routing._auto_down.clear()
    body = {"model": "cortados", "max_tokens": max_tokens, "messages": [{"role": "user", "content": text}]}
    seen = {}
    keep = routing.index_local
    def spy(core_, body_, r, **kw):
        rt = r.get("route") or {}
        seen.update(req=rt.get("required_index"), use_case=(rt.get("chosen") or {}).get("use_case"))
        return keep(core_, body_, r, **kw)
    routing.index_local = spy
    try:
        r = routing.choose(core, body, client="verify", conversation_id="verify-index-real", claude_bin=sys.executable,
                           codex_bin=sys.executable)
    finally:
        routing.index_local = keep
    return {**{k: r.get(k) for k in ("provider", "model", "band", "degraded", "error")}, **seen,
            "variant": (r.get("local") or {}).get("model", {}).get("variant")}
print(json.dumps({"syn": syn, "easy": real("rename the variable in utils.py", 64),
                  "outward": real("email the signed invoice to the client and mark it paid", 64), "local": local}))
"""
# Sensitive routes pick the published-index variant (merge of main's #109 into local-published-index): the REAL
# sensitive_local / refusal_route on the REAL generated scores. An R3 route (above both variants' index) flagged
# sensitive runs local on the strongest eligible variant: qwen-27b-think with max_tokens 8192, qwen-27b with 1024 (no
# room to think); one the non-thinking variant clears takes qwen-27b; the refusal fallback chooses the same way and its
# node body carries the variant's thinking. Review P2: the reviewer's repro (thinking asked, budget 1900, max_tokens 2048)
# qualifies for neither variant, yet a sensitive route must stay local: flag and refusal both serve qwen-27b with
# thinking disabled and record why, never the client's thinking unchanged; round 2: a `thinking: passthrough` entry
# (no variant can be set) is still sent thinking disabled, the reason naming the override. S1-free (synthetic S1-shaped routes); faked probe, throwaway config.
SENSITIVE_VARIANT_SCENARIO = """
import json, os, tempfile
from cortados_router import core
from cortados_router.provider import local_lane, routing
d = tempfile.mkdtemp()
cfg = os.path.join(d, "resources.json")
json.dump({"nodes": [{"id": "pro", "ollama": {"base_url": "http://127.0.0.1:9",
           "sensitive": {"model": "qwen-27b", "refusal_fallback": True},
           "models": [{"id": "Qwen27b_heretic:latest", "alias": "qwen-27b", "context_length": 131072}]}}]}, open(cfg, "w"))
os.environ[local_lane.CONFIG_ENV] = cfg
local_lane.probe = lambda node: {"Qwen27b_heretic:latest"}
core.log_route = lambda *a, **k: True
idx = {m["family"]: m["index"]["code"] for m in core.model_scores()["local_models"]}
def r(req):
    return {"provider": "claude-cli", "model": "claude-opus-test", "band": "R3", "route_id": "verify:sens-variant",
            "degraded": False, "decision": {"answers": {"outward": False, "blast_radius": 1}, "degraded": False},
            "route": {"band": "R3", "required_index": req, "answers": {"outward": False},
                      "features": {"blast_radius": 1},
                      "chosen": {"lane": "claude", "provider": "claude-cli", "model": "claude-opus-test", "use_case": "code"}}}
def b(mt):
    return {"model": "cortados", "max_tokens": mt, "messages": [{"role": "user", "content": "x"}]}
def flag(req, mt):
    routing._auto_down.clear()
    m = routing.sensitive_local(core, b(mt), r(req), client="verify", conversation_id="verify-sv", local="sensitive")
    lm = (m.get("local") or {}).get("model") or {}
    return [m.get("provider"), lm.get("variant"), local_lane._node_body(b(mt), lm).get("thinking") if lm else None]
routing._auto_down.clear()
marked = routing.sensitive_local(core, b(8192), r(99.0), client="verify", conversation_id="verify-sv")
sent = []
mv = routing.refusal_route(core, b(8192), marked, lambda m: sent.append(local_lane._node_body(b(8192), m["local"]["model"])) or True)
ask = {"model": "cortados", "max_tokens": 2048, "thinking": {"type": "enabled", "budget_tokens": 1900},
       "messages": [{"role": "user", "content": "x"}]}
def forced(m, out):
    lm = (m or {}).get("local", {}).get("model") or {}
    return [(m or {}).get("provider"), lm.get("variant"), out.get("thinking") if out else None,
            (m or {}).get("route", {}).get("chosen", {}).get("variant_why")]
routing._auto_down.clear()
fm = routing.sensitive_local(core, ask, r(10.0), client="verify", conversation_id="verify-sv", local="sensitive")
asked_flag = forced(fm, local_lane._node_body(ask, fm["local"]["model"]) if fm.get("local") else None)
routing._auto_down.clear()
marked2 = routing.sensitive_local(core, ask, r(10.0), client="verify", conversation_id="verify-sv")
sent2 = []
mv2 = routing.refusal_route(core, ask, marked2, lambda m: sent2.append(local_lane._node_body(ask, m["local"]["model"])) or True)
asked_refusal = forced(mv2, sent2[0] if sent2 else None)
flags = {"high_8192": flag(99.0, 8192), "high_1024": flag(99.0, 1024), "clears_non": flag(idx["qwen-27b"], 32000)}
pcfg = json.load(open(cfg))                        # from here on the model entry is thinking: passthrough
pcfg["nodes"][0]["ollama"]["models"][0]["thinking"] = "passthrough"
json.dump(pcfg, open(cfg, "w"))
routing._auto_down.clear()
pm = routing.sensitive_local(core, ask, r(10.0), client="verify", conversation_id="verify-sv", local="sensitive")
asked_passthrough = forced(pm, local_lane._node_body(ask, pm["local"]["model"]) if pm.get("local") else None)
print(json.dumps({"asked_flag": asked_flag, "asked_refusal": asked_refusal, "asked_passthrough": asked_passthrough, "idx": idx, **flags,
                  "refusal": [(mv or {}).get("provider"), ((mv or {}).get("local") or {}).get("model", {}).get("variant"),
                              sent[0].get("thinking") if sent else None]}))
"""
# Sensitive requests (2026-11-13, owner): a request flagged x-cortados-local: sensitive, or matching the owner's keyword
# list, moves to the node's sensitive model in ANY band, but only when S1 says it has no effect outside the machine
# (outward false, blast_radius <= 1); an outward request never goes local however it is marked (core.resolve's outward
# floor is untouched); an unavailable node keeps S1's route; a degraded decision is never local. Same fakes as above
# (probe faked in the child, throwaway home and config); the keyword is a placeholder, never a real term.
SENSITIVE_SCENARIO = """
import json, os, sys, tempfile
from cortados_router import core
from cortados_router.provider import local_lane, routing
d = tempfile.mkdtemp()
kw = os.path.join(d, "keywords.txt")
open(kw, "w").write("verify placeholder term\\n")
cfg = os.path.join(d, "resources.json")
json.dump({"nodes": [{"id": "pro", "ollama": {"base_url": "http://127.0.0.1:9",
           "sensitive": {"model": "verify-local", "keywords_file": kw, "refusal_fallback": True},
           "models": [{"id": "verify/local:tag", "alias": "verify-local", "context_length": 65536}]}}]}, open(cfg, "w"))
os.environ[local_lane.CONFIG_ENV] = cfg
state = {"down": None}
def probe(node):
    if state["down"]:
        raise local_lane.LocalUnavailable(state["down"])
    return {"verify/local:tag"}
local_lane.probe = probe
def route(text, local=None, down=None):
    state["down"] = down
    routing._auto_down.clear()
    body = {"model": "cortados", "messages": [{"role": "user", "content": text}]}
    r = routing.choose(core, body, client="verify", conversation_id="verify-sensitive", claude_bin=sys.executable,
                       codex_bin=sys.executable, local=local)
    rules = (r.get("route") or {}).get("hard_rules") or []
    return {**{k: r.get(k) for k in ("provider", "model", "band", "degraded", "error", "bind_reason")},
            "cloud_ok": r.get(local_lane.CLOUD_REROUTE_OK),
            "rule": next((h.split(":")[0] + ":" + h.split(":")[1] for h in rules if h.startswith("sensitive")), None)}
easy = "explain how this parser handles nested quotes"
print(json.dumps({"flag": route(easy, "sensitive"), "keyword": route("explain the Verify Placeholder Term"),
                  "plain": route(easy), "down": route(easy, "sensitive", "node pro: local gate: local switch off"),
                  "outward": route("email the signed invoice about the verify placeholder term to the client and mark "
                                   "it paid", "sensitive"),
                  "keyword_prefer": route("explain the Verify Placeholder Term", "prefer"),
                  "prefer": route(easy, "prefer")}))
"""

# Sensitive requests answered in the cloud (#124, owner 2026-11-15: warn, then use cloud). S1-free: the real
# routing.choose on a synthetic S1-shaped R3 route (select stubbed, no route log), a faked probe and throwaway config.
# A flagged or keyword request the Pro cannot take (switched off, on battery, the /v1/responses wire) keeps the cloud route
# and carries the mark (detector + reason categories); a request no detector marks, or one only `prefer`s, carries none.
# Then the real wires: the notice is content block 0 / output item 0, the model's blocks follow with their own indices and
# stop reason. History (review round 5, the delivered form is the only form): the session's entry is the reply as
# delivered and matching is plain equality, so a merged or dropped notice is a different entry (a rewrite, never exact);
# the Responses wire's echoed items fingerprint to the delivered entry, and a rebuilt transcript replays client text as
# sent (no label, nothing removed).
NOTICE_SCENARIO = """
import json, os, sys, tempfile
from cortados_router import core
from cortados_router.provider import anthropic_wire, local_lane, responses_wire, routing, sensitive_notice
d = tempfile.mkdtemp()
kw = os.path.join(d, "keywords.txt")
open(kw, "w").write("verify placeholder term\\n")
cfg = os.path.join(d, "resources.json")
json.dump({"nodes": [{"id": "pro", "ollama": {"base_url": "http://127.0.0.1:9",
           "sensitive": {"model": "verify-local", "keywords_file": kw, "refusal_fallback": True},
           "models": [{"id": "verify/local:tag", "alias": "verify-local", "context_length": 65536}]}}]}, open(cfg, "w"))
os.environ[local_lane.CONFIG_ENV] = cfg
state = {"down": None}
def probe(node):
    if state["down"]:
        raise local_lane.LocalUnavailable(state["down"])
    return {"verify/local:tag"}
local_lane.probe = probe
core.log_route = lambda *a, **k: True
answers = {"outward": False, "blast_radius": 0}
s1 = {"router": "cortados", "provider": "claude-cli", "model": "claude-verify", "band": "R3", "route_id": "verify:notice",
      "degraded": False, "decision": {"answers": answers, "degraded": False, "engine": "s1", "engine_ms": 1},
      "route": {"band": "R3", "chosen": {"lane": "claude", "provider": "claude-cli", "model": "claude-verify"},
                "hard_rules": []}}
routing.execution_routing.select = lambda core_, **k: json.loads(json.dumps(s1))
def mark(text, local=None, down=None, **extra):
    state["down"] = down
    routing._auto_down.clear()
    body = {"model": "cortados", "messages": [{"role": "user", "content": text}], **extra}
    r = routing.choose(core, body, client="verify", conversation_id="verify-notice", claude_bin=sys.executable,
                       codex_bin=sys.executable, local=local)
    return [r.get("provider"), r.get(sensitive_notice.ROUTE_KEY)]
off, battery = "node pro: local gate: local switch off", "node pro: local gate: local disabled: on battery"
easy = "explain how this parser handles nested quotes"
routes = {"flag_off": mark(easy, "sensitive", off), "keyword_battery": mark("explain the Verify Placeholder Term", None, battery),
          "responses": mark(easy, "sensitive", **{routing.NO_LOCAL_KEY: True}), "plain_off": mark(easy, None, off),
          "prefer_off": mark(easy, "prefer", off), "flag_up": mark(easy, "sensitive")}
model = [{"type": "text", "text": "Let me look."}, {"type": "tool_use", "id": "toolu_v", "name": "Read", "input": {"p": 1}}]
note = sensitive_notice.block("flag", "switched_off", "claude-verify")
shown = sensitive_notice.prepend(model, note)
evs = [json.loads(l[6:]) for c in anthropic_wire.sse_body(shown, "tool_use", {}) for l in c.decode().splitlines()
       if l.startswith("data: ")]
msg = anthropic_wire.message_json("m", "cortados", shown, "tool_use", {})
resp = responses_wire.message_json("r", "cortados", shown, "tool_use", {})
fp = anthropic_wire.reply_fingerprint
merged = [{"type": "text", "text": note["text"] + "\\n\\n" + model[0]["text"]}, model[1]]
def rebuilt(content):
    return "".join(b.get("text", "") for b in anthropic_wire.rebuild_transcript(
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": content}]))
rin = {"model": "cortados", "input": [{"type": "message", "role": "user", "content": "q"}, *resp["output"],
       {"type": "message", "role": "user", "content": "next"}]}
delivered = fp(shown)
history = (type(delivered) is str and delivered not in (fp(merged), fp(model))
           and responses_wire.parse_request(rin).history == [delivered])
rebuild = note["text"] in rebuilt(shown) and model[0]["text"] in rebuilt(shown) and "[CortadOS" not in rebuilt(shown)
print(json.dumps({"routes": routes, "text": note["text"],
                  "sse": [[e["index"], e["content_block"]["type"]] for e in evs if e["type"] == "content_block_start"],
                  "sse_stop": [e["delta"]["stop_reason"] for e in evs if e["type"] == "message_delta"],
                  "json": [b["type"] for b in msg["content"]], "json_stop": msg["stop_reason"],
                  "responses": [i["type"] for i in resp["output"]],
                  "fingerprint": history, "rebuild": rebuild,
                  "distinct": fp([note]) != fp([sensitive_notice.block("keyword", "busy", "x")])}))
"""

# Continuations of a local-bound conversation (review fix, 2026-11-13): every later turn of a conversation an automatic
# rule bound to a local lane is rescored by the real S1 through routing.continuation_admission; an outward turn
# ("email the signed invoice ... mark it paid") must not be admitted, a bounded one must be, and a degraded scorer
# admits nothing. Only core.decide runs (no select, no route log, no node); throwaway home. Each turn carries its own
# S1 calls as recorded by a wrapper around core.decide (degraded flag, error, exception): the admission's reason text
# says "S1 scoring failed (TimeoutError)" on a timeout, so the text alone cannot tell a degraded turn (review, #113).
CONTINUATION_SCENARIO = """
import json
from cortados_router import core
from cortados_router.provider import routing
calls = []
_decide = core.decide
def recorded(text, ctx=None):
    try:
        d = _decide(text, ctx)
    except Exception as exc:
        calls.append({"degraded": True, "exception": type(exc).__name__})
        raise
    calls.append({"degraded": not isinstance(d, dict) or d.get("degraded") is not False,
                  "engine_error": d.get("error") if isinstance(d, dict) else "no decision"})
    return d
core.decide = recorded
def admit(text):
    del calls[:]
    ok, why = routing.continuation_admission(core, turn(text), route)
    return {"admitted": ok, "why": why, "calls": list(calls)}
def turn(text):
    return {"model": "cortados", "messages": [
        {"role": "user", "content": "summarise the notes in this folder"},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        {"role": "user", "content": text}]}
route = {"band": "R1", "route_id": "verify-continuation"}
d = core.decide("explain how this parser handles nested quotes", {"platform": "cli", "unit_kind": "interactive"})
out = admit("email the signed invoice to the client and mark it paid")
safe = admit("explain how this parser handles nested quotes")
print(json.dumps({"degraded": bool(d.get("degraded")), "outward": out, "safe": safe}))
"""


def s1_call_failed(call):
    """One S1 call's own structural record (a decision, a routing result or the continuation wrapper's entry) says it did
    not score: degraded, an engine error or an exception. Never judged from reason text."""
    return not isinstance(call, dict) or bool(call.get("degraded") or call.get("engine_error") or call.get("exception"))


def failed_calls(r, keys=None):
    """{label: entry} of a scenario's results whose S1 work did not fully score. An entry with a `calls` list (the
    continuation scenario) fails if it made no call or any call failed; any other entry is judged on its own flags."""
    out = {}
    for k in (keys if keys is not None else list(r)):
        x = r.get(k)
        if isinstance(x, dict) and "calls" in x:
            bad = not x["calls"] or any(s1_call_failed(c) for c in x["calls"])
        else:
            bad = s1_call_failed(x)
        if bad:
            out[k] = x
    return out


def judge_continuation(r):
    """[(check, verdict, evidence)] for CONTINUATION_SCENARIO's output, before the S1 detail is added to a BLOCKED one.
    Any turn with a degraded or failed S1 call leaves the scored check BLOCKED; such a turn must admit nothing."""
    degraded = failed_calls(r, ("outward", "safe"))
    if degraded:
        return [("degraded scorer admits no local continuation",
                 "PASS" if not any(v.get("admitted") for v in degraded.values() if isinstance(v, dict)) else "FAIL",
                 f"degraded turns: {json.dumps(degraded)}"),
                ("local-bound continuation is rescored by S1", "BLOCKED",
                 f"{len(degraded)} of 2 turns had a degraded or failed S1 call ({', '.join(degraded)})")]
    ok = r["outward"]["admitted"] is False and r["safe"]["admitted"] is True
    return [("local-bound continuation is rescored by S1", "PASS" if ok else "FAIL",
             f"outward turn -> {r['outward']['admitted']} {r['outward']['why']} | "
             f"bounded turn -> {r['safe']['admitted']} {r['safe']['why']}")]


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
        bad = failed_calls(r, ("up", "battery"))     # each call on its own flag: one may time out, the other score
        if bad:
            # No band of its own: never local, and the degraded rule still keeps it off Claude.
            record("degraded decision is never auto-routed local (nor to Claude)",
                   "PASS" if all(x.get("provider") == "codex-cli" for x in bad.values()) else "FAIL",
                   json.dumps(bad))
            record("local auto-route only when the Pro is available", "BLOCKED",
                   f"no band to auto-route on ({', '.join(bad)}): {degraded_why}")
        else:
            ok = up_r.get("provider") == "local-pro" and up_r.get("model") == "verify/local:tag" and served(bat)
            record("local auto-route only when the Pro is available", "PASS" if ok else "FAIL",
                   f"available -> {up_r.get('provider')}/{up_r.get('model')} band {up_r.get('band')} | "
                   f"on battery -> {bat.get('provider')}/{bat.get('model')}")
        if failed_calls(r, ("outward",)):
            record("outward (R3) request never goes local", "BLOCKED", degraded_why)
        else:
            record("outward (R3) request never goes local",
                   "PASS" if out.get("band") == "R3" and not str(out.get("provider")).startswith("local-") else "FAIL",
                   f"band={out.get('band')} -> {out.get('provider')}/{out.get('model')}")

    p = runner.run(["-c", SENSITIVE_SCENARIO], prefix="verify-router-sensitive-", timeout=300)
    try:
        r = _last_json(p)
    except (ValueError, IndexError):
        record("sensitive request goes local only with no outward effect", "FAIL", (p.stderr or p.stdout).strip()[-300:])
    else:
        local = lambda x: str(x.get("provider")).startswith("local-")
        served = lambda x: x.get("provider") in ("claude-cli", "codex-cli") and not x.get("error")
        degraded_why = unreachable if not up else f"S1 listening at {s1_where} but the decision is degraded"
        # S1 can time out on single calls (#109 merge, 2026-11-14): each call is judged on its own. A degraded call must
        # stay off local and off Claude; any degraded call leaves the scored check without answers (BLOCKED).
        degraded = failed_calls(r)
        if degraded:
            record("degraded decision is never sensitive-routed local (nor to Claude)",
                   "PASS" if all(x.get("provider") == "codex-cli" for x in degraded.values()) else "FAIL",
                   json.dumps(degraded))
            record("sensitive request goes local only with no outward effect", "BLOCKED",
                   f"{len(degraded)} of {len(r)} calls degraded ({', '.join(degraded)}): {degraded_why}")
        else:
            flag, key, plain, down, out = r["flag"], r["keyword"], r["plain"], r["down"], r["outward"]
            ok = (local(flag) and flag.get("rule") == "sensitive: flag" and local(key)
                  and key.get("rule") == "sensitive: keyword" and served(plain) and served(down)
                  and out.get("band") == "R3" and not local(out))
            record("sensitive request goes local only with no outward effect", "PASS" if ok else "FAIL",
                   f"flag -> {flag.get('provider')} band {flag.get('band')} | keyword -> {key.get('provider')} "
                   f"band {key.get('band')} | unflagged -> {plain.get('provider')} | node off -> {down.get('provider')} | "
                   f"outward+flag+keyword -> band {out.get('band')} {out.get('provider')}/{out.get('model')}")
            # #122 round 2: sensitivity wins over the cost preference. A keyword request carrying `prefer` is bound as
            # keyword (sensitive profile) and never cleared for the node-error cloud re-route; plain `prefer` keeps its
            # cost bind and is cleared (its healthy-node routing unchanged).
            kp, pf = r["keyword_prefer"], r["prefer"]
            ok = (local(kp) and kp.get("rule") == "sensitive: keyword" and kp.get("bind_reason") == "keyword"
                  and kp.get("cloud_ok") is None and local(pf) and pf.get("bind_reason") == "prefer"
                  and pf.get("cloud_ok") is True)
            record("sensitivity wins over prefer; only a cleared request may leave on a node error",
                   "PASS" if ok else "FAIL",
                   f"keyword+prefer -> {kp.get('provider')} bind {kp.get('bind_reason')} cloud_ok {kp.get('cloud_ok')} | "
                   f"prefer -> {pf.get('provider')} bind {pf.get('bind_reason')} cloud_ok {pf.get('cloud_ok')}")

    p = runner.run(["-c", API_CREDIT_SCENARIO], prefix="verify-router-api-credit-", timeout=300)
    try:
        r = _last_json(p)
    except (ValueError, IndexError):
        record("expiring API credit takes cost-bound local work, nothing else (S1-free)", "FAIL",
               (p.stderr or p.stdout).strip()[-300:])
    else:
        api = lambda x: x[0] == "anthropic-api" and x[2] == "api_credit_expiry"
        ok = (api(r["expiring"]) and r["expiring"][1] == "claude-sonnet-5" and api(r["expiring_r1"])
              and r["not_expiring"][0] == "local-pro" and r["past_stop"][0] == "local-pro"
              and r["outward"][0] == "codex-cli" and r["degraded"][0] == "codex-cli" and r["stop_state"][0] is False
              and r["fallbacks"][0] == "local-pro")
        record("expiring API credit takes cost-bound local work, nothing else (S1-free)", "PASS" if ok else "FAIL",
               f"grant in 2 d: R2 -> {r['expiring'][:2]}, R1 -> {r['expiring_r1'][:2]} | in 20 d -> {r['not_expiring'][:2]} | "
               f"past the stop line -> {r['past_stop'][:2]} ({r['stop_state'][1]}) | outward -> {r['outward'][:2]} | "
               f"degraded -> {r['degraded'][:2]} | with fallbacks -> {r['fallbacks'][:2]}")
        c = r["cont"]
        ok = c == {"clean": True, "sensitive": False, "prefer": False, "outward": False, "no_check": False,
                   "not_expiring": False}
        record("API credit continuations re-pass the gate before forwarding (S1-free)", "PASS" if ok else "FAIL",
               " | ".join(f"{k} -> {'forward' if v else 'refuse'}" for k, v in c.items()))

    p = runner.run(["-c", INDEX_SCENARIO], prefix="verify-router-index-", timeout=300)
    try:
        r = _last_json(p)
    except (ValueError, IndexError):
        record("local proxy index places Qwen27b_heretic (S1-free)", "FAIL", (p.stderr or p.stdout).strip()[-300:])
    else:
        syn = r["syn"]
        ok = (syn["non"] == ["local-pro", "qwen-27b"] and syn["think"] == ["local-pro", "qwen-27b-think"]
              and syn["think_small"][0] == "codex-cli" and syn["outward"][0] == "codex-cli" and syn["degraded"][0] == "codex-cli"
              and syn["battery"][0] == "codex-cli")
        record("local proxy index places Qwen27b_heretic (S1-free)", "PASS" if ok else "FAIL",
               f"code index {syn['indexes']} | req {syn['indexes']['qwen-27b']} -> {syn['non']} | req {syn['mid']} max_tokens 8192 -> "
               f"{syn['think']}, 1024 -> {syn['think_small']} | outward -> {syn['outward']} | degraded -> {syn['degraded']} | "
               f"battery -> {syn['battery']}")
        easy, out = r["easy"], r["outward"]
        degraded_why = unreachable if not up else f"S1 listening at {s1_where} but the decision is degraded"
        # Each S1 call judged on its own structural flags (failed_calls, #113): one may time out, the other score.
        bad = failed_calls(r, ("easy", "outward"))
        if bad:
            record("degraded decision never goes local by the index (nor to Claude)",
                   "PASS" if all(x.get("provider") == "codex-cli" for x in bad.values()) else "FAIL", json.dumps(bad))
        if "easy" in bad:
            record("S1 route goes local exactly when a variant's index clears it", "BLOCKED",
                   f"easy call degraded: {degraded_why}")
        else:
            idx = (r["local"]["qwen-27b"]["index"].get(easy.get("use_case") or "general") if easy.get("req") is not None else None)
            expect_local = idx is not None and easy.get("band") in ("R0", "R1", "R2") and idx >= easy["req"]
            ok = (easy.get("provider") == "local-pro") == expect_local
            record("S1 route goes local exactly when a variant's index clears it", "PASS" if ok else "FAIL",
                   f"band={easy.get('band')} use_case={easy.get('use_case')} req={easy.get('req')} qwen-27b={idx} -> "
                   f"{easy.get('provider')}/{easy.get('model')} {easy.get('variant') or ''}")
        if "outward" in bad:
            record("outward request never goes local by the index", "BLOCKED", f"outward call degraded: {degraded_why}")
        else:
            record("outward request never goes local by the index",
                   "PASS" if not str(out.get("provider")).startswith("local-") else "FAIL",
                   f"band={out.get('band')} -> {out.get('provider')}/{out.get('model')}")

    p = runner.run(["-c", SENSITIVE_VARIANT_SCENARIO], prefix="verify-router-sens-variant-", timeout=300)
    try:
        r = _last_json(p)
    except (ValueError, IndexError):
        record("sensitive route picks the published-index variant (S1-free)", "FAIL", (p.stderr or p.stdout).strip()[-300:])
    else:
        on = {"type": "enabled", "budget_tokens": 4096}
        off = {"type": "disabled"}
        ok = (r["high_8192"] == ["local-pro", "qwen-27b-think", on] and r["high_1024"] == ["local-pro", "qwen-27b", off]
              and r["clears_non"] == ["local-pro", "qwen-27b", off] and r["refusal"] == ["local-pro", "qwen-27b-think", on])
        why = "thinking requested but max_tokens < 4096: served without thinking"
        ok = ok and r["asked_flag"] == r["asked_refusal"] == ["local-pro", "qwen-27b", off, why]
        ok = ok and r["asked_passthrough"] == ["local-pro", None, off, why + " (its node entry is thinking: "
                                               "passthrough, overridden so the answer is not starved)"]
        record("sensitive route picks the published-index variant (S1-free)", "PASS" if ok else "FAIL",
               f"code index {r['idx']} | R3 flag 8192 -> {r['high_8192']} | 1024 -> {r['high_1024']} | "
               f"req {r['idx'].get('qwen-27b')} -> {r['clears_non']} | refusal 8192 -> {r['refusal']} | "
               f"thinking asked 2048: flag -> {r['asked_flag'][:3]}, refusal -> {r['asked_refusal'][:3]}, "
               f"passthrough entry -> {r['asked_passthrough'][:3]}")

    p = runner.run(["-c", NOTICE_SCENARIO], prefix="verify-router-notice-", timeout=300)
    try:
        r = _last_json(p)
    except (ValueError, IndexError):
        record("sensitive request the Pro cannot take is answered in the cloud with a notice (S1-free)", "FAIL",
               (p.stderr or p.stdout).strip()[-300:])
    else:
        rt = r["routes"]
        cloud = lambda x, det, why: x[0] == "claude-cli" and x[1] == {"detector": det, "reason": why}
        ok = (cloud(rt["flag_off"], "flag", "switched_off") and cloud(rt["keyword_battery"], "keyword", "on_battery")
              and cloud(rt["responses"], "flag", "responses_wire") and rt["plain_off"] == ["claude-cli", None]
              and rt["prefer_off"] == ["claude-cli", None] and rt["flag_up"] == ["local-pro", None]
              and r["sse"] == [[0, "text"], [1, "text"], [2, "tool_use"]] and r["sse_stop"] == ["tool_use"]
              and r["json"] == ["text", "text", "tool_use"] and r["json_stop"] == "tool_use"
              and r["responses"] == ["message", "message", "function_call"] and r["fingerprint"] is True
              and r["rebuild"] is True and r["distinct"] is True and "verify placeholder" not in r["text"].lower())
        record("sensitive request the Pro cannot take is answered in the cloud with a notice (S1-free)",
               "PASS" if ok else "FAIL",
               f"flag+off -> {rt['flag_off']} | keyword+battery -> {rt['keyword_battery']} | responses -> "
               f"{rt['responses'][1]} | plain/prefer+off -> {rt['plain_off'][1]}/{rt['prefer_off'][1]} | flag+up -> "
               f"{rt['flag_up'][0]} | sse {r['sse']} stop {r['sse_stop']} | responses {r['responses']} | "
               f"history exact-only {r['fingerprint']} | rebuild replays as sent {r['rebuild']} | notice-only distinct "
               f"{r['distinct']} | \"{r['text']}\"")

    p = runner.run(["-c", CONTINUATION_SCENARIO], prefix="verify-router-continuation-", timeout=300)
    try:
        r = _last_json(p)
    except (ValueError, IndexError):
        record("local-bound continuation is rescored by S1", "FAIL", (p.stderr or p.stdout).strip()[-300:])
    else:
        # Per turn, on each S1 call's own flags (judge_continuation): never on the admission's reason text.
        for name, verdict, evidence in judge_continuation(r):
            if verdict == "BLOCKED":
                evidence += f": {unreachable if not up else f'S1 listening at {s1_where}'}"
            record(name, verdict, evidence)

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
