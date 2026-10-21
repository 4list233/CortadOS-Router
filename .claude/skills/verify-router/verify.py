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
S1_URL = os.environ.get("CORTADOS_S1_URL", "http://127.0.0.1:8431")


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
