"""PURPOSE: Drive the real router entrypoint (core.py) on a throwaway CORTADOS_HOME and print an
evidence block. Each scenario reports PASS, FAIL or BLOCKED; a scenario that needs S1 is BLOCKED
(never PASS) while S1 is unreachable, because a degraded decision cannot prove S1-driven routing.
CEILING: Read-only with respect to ~/.cortados and the user's subscriptions. It never runs
scarcity.py, which would open the vendor CLIs, and every child runs with CORTADOS_SCARCITY_REFRESH=off
so a fresh throwaway home cannot schedule a usage sample either.
Usage: python3 -B .claude/skills/verify-router/verify.py
"""
import json, os, subprocess, sys, tempfile, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CORE = REPO / "cortados_router" / "core.py"
LIVE_HOME = Path.home() / ".cortados"
sys.path.insert(0, str(REPO))
from cortados_router.s1_endpoint import resolve_url   # noqa: E402  same resolver core uses, so health probe and decision hit one scorer
S1_URL = resolve_url()


PROBE_DIRS = []


def run(args):
    home = tempfile.mkdtemp(prefix="verify-router-")
    # A fresh home is always "due" for a usage sample, and that sample opens the real vendor CLIs: the switch keeps
    # the child from scheduling one. The probe folder is checked afterwards as evidence that none was opened.
    PROBE_DIRS.append(Path(home) / "usage-probe")
    env = {**os.environ, "CORTADOS_HOME": home, "CORTADOS_SCARCITY_REFRESH": "off"}
    p = subprocess.run([sys.executable, "-B", str(CORE), *args], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=180)
    return p.returncode, p.stdout, p.stderr


def decide(request):
    code, out, err = run(["decide", request])
    body = out[: out.rindex("}") + 1] if "}" in out else ""
    return code, (json.loads(body) if body else None), out.strip().splitlines()[-1:] or [err]


def s1_up():
    try:
        urllib.request.urlopen(S1_URL, timeout=3)
        return True
    except Exception as e:  # any HTTP response means the server is listening
        return hasattr(e, "code")


def live_state_stamp():
    if not LIVE_HOME.exists():
        return None
    return sorted((str(p), p.stat().st_mtime_ns) for p in LIVE_HOME.rglob("*") if p.is_file())


results = []
def record(name, verdict, evidence):
    results.append((name, verdict, evidence))

before = live_state_stamp()
up = s1_up()

code, out, err = run(["table"])
record("table loads and validates", "PASS" if code == 0 and out.strip().endswith("ok") else "FAIL",
       out.strip() or err.strip())

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
        record("outward request floors blast radius (R3)", "BLOCKED", f"S1 listening at {S1_URL} but the decision is degraded")
    else:
        record("outward request floors blast radius (R3)", "PASS" if band == "R3" else "FAIL",
               f"band={band} | {tail[0] if tail else ''}")
else:
    record("outward request floors blast radius (R3)", "BLOCKED", f"S1 unreachable at {S1_URL}")

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
home = tempfile.mkdtemp(prefix="verify-router-")
PROBE_DIRS.append(Path(home) / "usage-probe")
p = subprocess.run([sys.executable, "-B", "-c", PROVIDER_SCENARIO], cwd=REPO, capture_output=True, text=True, timeout=180,
                   env={**os.environ, "CORTADOS_HOME": home, "CORTADOS_SCARCITY_REFRESH": "off"})
try:
    r = json.loads(p.stdout.strip().splitlines()[-1])
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
home = tempfile.mkdtemp(prefix="verify-router-pin-")
PROBE_DIRS.append(Path(home) / "usage-probe")
p = subprocess.run([sys.executable, "-B", "-c", PIN_SCENARIO], cwd=REPO, capture_output=True, text=True, timeout=180,
                   env={**os.environ, "CORTADOS_HOME": home, "CORTADOS_SCARCITY_REFRESH": "off"})
try:
    r = json.loads(p.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    record("friendly pin resolves to one model, ambiguity is refused", "FAIL", (p.stderr or p.stdout).strip()[-300:])
else:
    one, many = r["one"], r["many"]
    ok_one = (one.get("provider"), one.get("model"), one.get("pinned")) == ("codex-cli", "gpt-6.1-sol", True)
    ok_many = many.get("client_error") and all(c in (many.get("error") or "") for c in ("gpt-6-astra", "gpt-6-luna", "gpt-6-sol")) \
        and not many.get("model")
    record("friendly pin resolves to one model, ambiguity is refused", "PASS" if ok_one and ok_many else "FAIL",
           f"sub/GPT 6.1 -> {one.get('provider')}/{one.get('model')} | sub/GPT 6 -> {(many.get('error') or '')[:120]}")

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
home = tempfile.mkdtemp(prefix="verify-router-local-")
PROBE_DIRS.append(Path(home) / "usage-probe")
p = subprocess.run([sys.executable, "-B", "-c", LOCAL_SCENARIO], cwd=REPO, capture_output=True, text=True, timeout=300,
                   env={**os.environ, "CORTADOS_HOME": home, "CORTADOS_SCARCITY_REFRESH": "off"})
try:
    r = json.loads(p.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    record("local auto-route only when the Pro is available", "FAIL", (p.stderr or p.stdout).strip()[-300:])
else:
    up_r, bat, out = r["up"], r["battery"], r["outward"]
    served = lambda x: x.get("provider") in ("claude-cli", "codex-cli") and not x.get("error")
    if up_r.get("degraded"):
        # No band of its own: never local, and the degraded rule still keeps it off Claude.
        record("degraded decision is never auto-routed local (nor to Claude)",
               "PASS" if up_r.get("provider") == "codex-cli" and bat.get("provider") == "codex-cli" else "FAIL",
               json.dumps(up_r))
        record("local auto-route only when the Pro is available", "BLOCKED", "S1 degraded: no band to auto-route on")
    else:
        ok = up_r.get("provider") == "local-pro" and up_r.get("model") == "verify/local:tag" and served(bat)
        record("local auto-route only when the Pro is available", "PASS" if ok else "FAIL",
               f"available -> {up_r.get('provider')}/{up_r.get('model')} band {up_r.get('band')} | "
               f"on battery -> {bat.get('provider')}/{bat.get('model')}")
    if out.get("degraded"):
        record("outward (R3) request never goes local", "BLOCKED", "S1 degraded")
    else:
        record("outward (R3) request never goes local",
               "PASS" if out.get("band") == "R3" and not str(out.get("provider")).startswith("local-") else "FAIL",
               f"band={out.get('band')} -> {out.get('provider')}/{out.get('model')}")

record("live ~/.cortados untouched", "PASS" if live_state_stamp() == before else "FAIL",
       str(LIVE_HOME))
record("no vendor CLI probe opened", "PASS" if all(not p.exists() or not any(p.iterdir()) for p in PROBE_DIRS) else "FAIL",
       f"{len(PROBE_DIRS)} throwaway homes checked for usage-probe folders")

print("## Router verification evidence")
print(f"repo: {REPO}  python: {sys.version.split()[0]}  S1: {'up' if up else 'down'} ({S1_URL})")
for name, verdict, evidence in results:
    print(f"- [{verdict}] {name} — {evidence}")
verdicts = {v for _, v, _ in results}
overall = "FAIL" if "FAIL" in verdicts else "BLOCKED" if "BLOCKED" in verdicts else "PASS"
print(f"overall: {overall}")
sys.exit(1 if overall == "FAIL" else 0)
