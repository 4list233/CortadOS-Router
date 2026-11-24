#!/usr/bin/env python3
"""Offline replay for docs/SCARCITY-V2.md: the scarcity ranking of #143 (routing table v21) against the proposed v2.

Read-only and offline. It never opens a vendor CLI, never reads or writes a live ~/.cortados, and never sends a request:
it replays the S1 answers and token counts a COPY of a router.db holds, on a throwaway CORTADOS_HOME, with the clock,
the usage readings and the burn rates simulated in memory.

What it simulates
  * Time runs from the copied readings' fetch time (--start) to the API grant's expiry (--end). The demand is the copied
    7-day route log replayed one week later (and two weeks later for the tail), so every conversation keeps its
    weekday and hour, its S1 answers, its band and its token counts.
  * Pools: Claude account A (lane `claude`), account B (`claude#b`), Codex (`codex`) and the API credit grant
    (`anthropic-api`). Each Claude account also has its Fable week. A call's quota cost is its tokens in API-price
    units ("$-eq": weighted tokens x the model's blended $/1M / 2, i.e. Anthropic's input price for Claude models)
    divided by the pool's size in $-eq per 1 % (--k-claude, --k-codex; the grant: its stop line / 100).
  * The owner's own sessions on account A (not routed, not in the log) burn --owner-rate % per hour of A's week.
  * A usage "sample" is taken every 20 minutes, as the live sampler does; routing sees only the latest sample.
  * Windows reset 7 days after their previous reset; the used % at each reset is recorded.

Policies
  before  core.resolve of the checked-out repo as the provider calls it (execution, accounts aware, served routes plus
          the API lane's pairs while the grant has room, the conversation id as split key); pins on Claude by
          core.static_scarcity_rank then configured order (A first); degraded calls on codex/gpt-6.1-sol.
  v2      the proposal: band, difficulty and required index from core.resolve, then the equal-usage controller
          (water level, expiry by hours left, owner reserve), burn-up within the band window, pins ranked by the same
          controller, degraded calls still Codex only.
  v2-nodrain / v2-noburn  v2 without the drain queue / without burn-up, to separate the effects.

Usage
  scarcity_v2_replay.py --repo <checkout> --data <dir with router.db copy, scarcity.json, unit-cost.json,
                        scarcity-history.jsonl> --policy before|v2|v2-nodrain|v2-noburn --out <result.json>
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time

WEEK = 7 * 86400.0
TICK = 1200.0                      # the live sampler's ~20-minute interval
DB_NAME = "router-audit-143.db"
# blended $/1M (model-scores.generated.json; Haiku 4.5 has none there: $1/$5 blended 3:1 = $2)
PRICE = {"claude-opus-5-5": 8.0, "claude-sonnet-5": 4.0, "claude-opus-5": 10.0, "claude-haiku-4-5-20251001": 2.0,
         "claude-fable-5-1": 20.0, "gpt-6.1-sol": 4.0, "gpt-5.6-luna": 0.45, "gpt-5.6-sol": 8.0, "gpt-6-astra": 20.0,
         "gpt-5.6-terra": 4.5, "gpt-6-luna": 0.2, "gpt-6-sol": 4.0, "gpt-5.5": 11.25}
CLAUDE_LANES = ("claude", "claude#b")
POOLS = ("claude", "claude#b", "codex", "anthropic-api")
SPEND_DOWN = ("anthropic-api",)       # budgets.json lanes.anthropic-api.spend_down
BANDS = ("R0", "R1", "R2", "R3", "R4")


def weighted(i, o, cr, cw):
    return (i or 0) + 5 * (o or 0) + 0.1 * (cr or 0) + 1.25 * (cw or 0)


def eq_usd(model, w):
    """API-price units of a call: weighted tokens x blended price / 2 (= Anthropic's input price for Claude models)."""
    return w * PRICE.get(model, 8.0) / 2.0 / 1e6


def h(x):
    return int.from_bytes(hashlib.sha256(str(x).encode()).digest()[:8], "big") / 2.0 ** 64


def split_pick(key, weights):
    live = sorted((k, float(v)) for k, v in weights.items() if v and v > 0)
    tot = sum(v for _, v in live)
    if not live:
        return None
    u, acc = h(key), 0.0
    for k, v in live:
        acc += v / tot
        if u < acc:
            return k
    return live[-1][0]


class Sim:
    def __init__(self, a):
        self.a = a
        self.now = a.start
        self.data = a.data
        src = json.load(open(os.path.join(a.data, "scarcity.json")))
        self.src = src
        lanes = src["lanes"]
        self.cfg_dir = {"claude": "/sim/acctA", "claude#b": "/sim/acctB"}
        st = {}
        for lane in ("claude", "claude#b", "codex"):
            wins = {w["window"]: w for w in lanes[lane]["windows"]}
            allw = wins.get("week (all models)") or wins.get("week")
            st[lane] = {"used": float(allw["used_pct"]), "reset": float(allw["reset_epoch"])}
            fw = wins.get("week (Fable)")
            if fw:
                st[lane]["fable"] = float(fw["used_pct"])
                st[lane]["fable_reset"] = float(fw["reset_epoch"])
        g = lanes["anthropic-api"]["windows"][0]
        self.grant_end = float(g["reset_epoch"])                       # the window ends where the grant stops counting
        self.grant_window_s = g["resets_in_h"] * 3600.0 / max(1e-6, 1.0 - g["elapsed_pct"] / 100.0)
        self.stop_usd = a.stop_usd
        st["anthropic-api"] = {"used": 0.0, "spent": 0.0}
        self.st = st
        self.hist = collections.defaultdict(list)                     # (lane, window) -> [(ts, used)]
        for line in open(os.path.join(a.data, "scarcity-history.jsonl")):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r["ts"] > a.start or r["ts"] < a.start - 2 * 86400:
                continue
            for lane, v in (r.get("lanes") or {}).items():
                for w in v.get("windows") or []:
                    self.hist[(lane, w[0])].append((r["ts"], float(w[1])))
        self.window_ends = []                                          # (lane, window, ended_at, used_pct)
        self.reading = None
        self.served_log = collections.Counter()
        self.eq_by_pool = collections.Counter()
        self.eq_by_pool_kind = collections.Counter()
        self.calls_by_pool = collections.Counter()
        self.mix = collections.Counter()
        self.drain_eq = collections.Counter()
        self.drain_hours = collections.Counter()
        self.recent = collections.deque()                              # (ts, kind, pool, base_eq) for the demand forecast
        self.k = {"claude": a.k_claude, "claude#b": a.k_claude, "codex": a.k_codex,
                  "anthropic-api": self.stop_usd / 100.0}
        self.trace = []
        self.ctrl = {}

    # ------------------------------------------------------------ quota accounting
    def charge(self, pool, model, w, kind):
        e = eq_usd(model, w)
        if pool == "anthropic-api":
            s = self.st[pool]
            s["spent"] += e
            s["used"] = 100.0 * s["spent"] / self.stop_usd
        elif pool in self.st:
            s = self.st[pool]
            s["used"] = min(100.0, s["used"] + e / self.k[pool])
            if "fable" in model and "fable" in s:
                s["fable"] = min(100.0, s["fable"] + e / (self.k[pool] * 0.5))
        self.eq_by_pool[pool] += e
        self.eq_by_pool_kind[(pool, kind)] += e
        self.calls_by_pool[pool] += 1
        return e

    def advance(self, t):
        """Move the clock to t: owner burn on A, window resets, samples every TICK."""
        while self.now < t:
            nxt = min(t, (math.floor(self.now / TICK) + 1) * TICK)
            dt_h = (nxt - self.now) / 3600.0
            self.st["claude"]["used"] = min(100.0, self.st["claude"]["used"] + self.a.owner_rate * dt_h)
            self.drain_step(nxt, dt_h)
            self.now = nxt
            for lane in ("claude", "claude#b", "codex"):
                s = self.st[lane]
                if self.now >= s["reset"]:
                    self.window_ends.append((lane, "all", s["reset"], round(s["used"], 1)))
                    s["used"], s["reset"] = 0.0, s["reset"] + WEEK
                    self.hist = {k: (v if k[0] != lane else []) for k, v in self.hist.items()}
                    self.hist = collections.defaultdict(list, self.hist)
                if "fable_reset" in s and self.now >= s["fable_reset"]:
                    self.window_ends.append((lane, "fable", s["fable_reset"], round(s["fable"], 1)))
                    s["fable"], s["fable_reset"] = 0.0, s["fable_reset"] + WEEK
            if abs(self.now / TICK - round(self.now / TICK)) < 1e-9:
                self.sample()

    def drain_step(self, t, dt_h):
        if self.a.policy != "v2" or not self.ctrl:
            return
        for p, d in self.ctrl.get("drain", {}).items():
            if d <= 0:
                continue
            e = d * dt_h
            if p == "anthropic-api":
                s = self.st[p]
                e = min(e, max(0.0, self.stop_usd - s["spent"]))
                s["spent"] += e
                s["used"] = 100.0 * s["spent"] / self.stop_usd
            else:
                self.st[p]["used"] = min(100.0, self.st[p]["used"] + e / self.k[p])
            self.drain_eq[p] += e
            self.drain_hours[p] += dt_h
            self.eq_by_pool_kind[(p, "drain")] += e

    # ------------------------------------------------------------ readings as the router sees them
    def sample(self):
        import scarcity_mod as sc  # noqa: the module object is injected below
        now = self.now
        lanes = {}
        for lane in ("claude", "claude#b", "codex"):
            s = self.st[lane]
            wins = []
            name = "week (all models)" if lane != "codex" else "week"
            w = sc.window_view(name, s["used"], s["reset"], WEEK, now)
            w["pool"] = "all"
            wins.append(w)
            self.hist[(lane, name)].append((now, s["used"]))
            if "fable" in s:
                fw = sc.window_view("week (Fable)", s["fable"], s["fable_reset"], WEEK, now)
                fw["pool"] = "fable"
                wins.append(fw)
                self.hist[(lane, "week (Fable)")].append((now, s["fable"]))
            rec = {"windows": wins, "fetched_at": now, "tried_at": now, "error": None}
            if lane in self.cfg_dir:
                rec["config_dir"] = rec["login"] = self.cfg_dir[lane]
            lanes[lane] = sc.finish(lane, rec)
        api = self.st["anthropic-api"]
        gw = sc.window_view("grant", api["used"], self.grant_end, self.grant_window_s, now)
        lanes["anthropic-api"] = sc.finish("anthropic-api", {
            "windows": [gw], "fetched_at": now, "tried_at": now, "error": None, "stop_line_usd": self.stop_usd,
            "budget_usd": self.stop_usd / 0.95, "spent_usd": api["spent"], "lane_on": self.api_on()})
        for lane in ("cursor", "local", "antigravity"):
            v = dict(self.src["lanes"].get(lane) or {})
            if lane == "cursor":
                v["fetched_at"] = v["tried_at"] = now
            lanes[lane] = v
        self.reading = {"fetched_at": now, "lanes": lanes}
        self.control()

    def api_on(self):
        return self.now < self.grant_end and self.st["anthropic-api"]["spent"] < self.stop_usd - 0.5

    def burns(self):
        out = {}
        for k, pts in self.hist.items():
            seg = [p for p in pts if p[0] >= self.now - 24 * 3600]
            if len(seg) > 1 and (seg[-1][0] - seg[0][0]) / 3600 >= 6.0:
                out[k] = max(0.0, seg[-1][1] - seg[0][1]) / ((seg[-1][0] - seg[0][0]) / 3600)
        return out

    # ------------------------------------------------------------ v2 controller (docs/SCARCITY-V2.md §3)
    def hours_left(self, p):
        if p == "anthropic-api":
            return (self.grant_end - self.now) / 3600.0
        return (self.st[p]["reset"] - self.now) / 3600.0

    def control(self):
        a = self.a
        if not a.policy.startswith("v2"):
            return
        # demand forecast: the last 24 h of routable work in base-model $-eq per hour, by kind
        while self.recent and self.recent[0][0] < self.now - 24 * 3600:
            self.recent.popleft()
        span = max(6.0, min(24.0, (self.now - a.start) / 3600.0)) if self.recent else 24.0
        flex = sum(x[3] for x in self.recent if x[1] in ("routed", "pin")) / span
        forced = collections.Counter()
        for x in self.recent:
            if x[1] == "degraded":
                forced[x[2]] += x[3] / span
        if not self.recent:
            flex, forced = a.prior_flex, collections.Counter({"codex": a.prior_forced_codex})
        pools = [p for p in POOLS if p != "anthropic-api" or self.api_on()]
        f, cap, H = {}, {}, {}
        for p in pools:
            hl = max(0.25, self.hours_left(p) - 1.0)                  # never plan the last hour before a reset (floor.py)
            H[p] = hl
            used = self.st[p]["used"]
            owner = a.owner_rate * hl if p == "claude" else 0.0
            f[p] = used + owner + forced[p] * hl / self.k[p]
            # owner reserve: routed work may bring a pool to 95 % less half the owner's own forecast use (§6)
            cap[p] = 100.0 if p == "anthropic-api" else max(0.0, 95.0 - 0.5 * owner)

        # spend-down pools (budgets.json "spend_down": the API grant never refills) pace to their stop line first
        sd = [p for p in pools if p in SPEND_DOWN and a.spend_down]
        rate = {p: self.k[p] * max(0.0, cap[p] - f[p]) / H[p] for p in sd}
        rest = [p for p in pools if p not in sd]
        flex_rest = max(0.0, flex - sum(rate.values()))

        def absorb(L):
            return sum(self.k[p] * max(0.0, min(L, cap[p]) - f[p]) / H[p] for p in rest)
        lo, hi = 0.0, 100.0
        if absorb(hi) <= flex_rest:
            L = hi
        else:
            for _ in range(60):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if absorb(mid) < flex_rest else (lo, mid)
            L = (lo + hi) / 2
        rate.update({p: self.k[p] * max(0.0, min(L, cap[p]) - f[p]) / H[p] for p in rest})
        end = {p: max(f[p], min(L, cap[p])) for p in rest}
        # a spend-down pool's forecast end: what it is actually absorbing (last 24 h, real models) carried to its end
        got = collections.Counter()
        for x in self.recent:
            got[x[2]] += x[4] / span
        for p in sd:
            end[p] = min(cap[p], self.st[p]["used"] + got[p] * H[p] / self.k[p])
        # burn-up (§4): a pool whose forecast end, at what it is ACTUALLY absorbing now (real models, last 24 h), stays
        # below the burn line runs its real work on the strongest model of the band window. Closed loop: the upgraded
        # calls raise the measured rate, the forecast crosses the line and burn-up switches off.
        burn = {}
        proj = {}
        for p in pools:
            proj[p] = min(100.0, f[p] + got[p] * H[p] / self.k[p]) if p not in sd else end[p]
            if a.policy == "v2-noburn":
                continue
            line = cap[p] - 1.0 if p in sd else min(a.burn_line, cap[p])
            if proj[p] < line and (p in sd or end[p] < line):
                burn[p] = 1.0
        # a Claude account's Fable week is its own quota: when it is forecast to lapse, Fable goes first in burn-up
        fable_burn = {}
        for p in CLAUDE_LANES:
            if p in pools and "fable" in self.st[p]:
                fg = sum(x[4] for x in self.recent if x[2] == p and x[5]) / span
                fproj = self.st[p]["fable"] + fg * H[p] / (self.k[p] * 0.5)
                fable_burn[p] = fproj < a.burn_line
        # drain: pools forecast to end below the drain line, inside the drain horizon, take queued backlog work
        drain = {}
        if a.policy == "v2":
            for p in pools:
                hl = self.hours_left(p)
                if p in sd:
                    pj, line, horizon = end[p], a.drain_line_sd, a.drain_horizon_sd_h
                else:
                    pj, line, horizon = max(proj[p], end[p]), a.drain_line, a.drain_horizon_h
                if p == "claude" and a.owner_rate > 0:
                    continue                                  # never drain the account the owner is using (§6)
                if hl <= horizon and hl > 1.0 and pj < line and self.st[p]["used"] < cap[p]:
                    drain[p] = min(a.drain_max_eq_h, self.k[p] * (min(line, cap[p]) - pj) / H[p])
        self.ctrl = {"L": L, "rate": rate, "f": f, "cap": cap, "end": end, "burn": burn, "drain": drain, "flex": flex,
                     "fable_burn": fable_burn, "proj": proj}
        if int(self.now) % (6 * 3600) < TICK:
            self.trace.append({"t": time.strftime("%m-%d %H:%M", time.localtime(self.now)), "L": round(L, 1),
                               "flex_eq_h": round(flex, 1),
                               "used": {p: round(self.st[p]["used"], 1) for p in POOLS},
                               "rate": {p: round(v, 1) for p, v in rate.items()},
                               "burn": sorted(burn), "proj": {p: round(v, 1) for p, v in proj.items()},
                               "fable_burn": sorted(k for k, v in fable_burn.items() if v),
                               "drain": {p: round(v, 1) for p, v in drain.items()}})


def setup(a):
    home = tempfile.mkdtemp(prefix="sv2-home-")
    for f in ("scarcity.json", "unit-cost.json"):
        shutil.copy(os.path.join(a.data, f), home)
    tables = tempfile.mkdtemp(prefix="sv2-tables-")
    shutil.copytree(os.path.join(a.repo, "tables"), tables, dirs_exist_ok=True)
    with open(os.path.join(tables, "accounts.json"), "w") as fh:
        json.dump({"claude": [{"id": "a", "config_dir": "/sim/acctA"}, {"id": "b", "config_dir": "/sim/acctB"}]}, fh)
    for k in [k for k in os.environ if k.startswith("CORTADOS_")]:
        del os.environ[k]
    os.environ.update(CORTADOS_HOME=home, CORTADOS_ROUTER_TABLES=tables, CORTADOS_SCARCITY_REFRESH="off",
                      CORTADOS_API_CREDIT_CONFIG=os.path.join(home, "no-api-credit.json"))
    sys.path.insert(0, a.repo)


def tier_of(c, ref, use_case):
    t = 0
    for i, b in enumerate(BANDS[1:], start=1):
        r = (ref.get(b) or {}).get(use_case)
        if r is not None and c["index"] >= r:
            t = i
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--policy", required=True, choices=("before", "v2", "v2-nodrain", "v2-noburn"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=float, default=None, help="epoch; default: the copied readings' fetch time")
    ap.add_argument("--end", type=float, default=None, help="epoch; default: the grant's last counting hour")
    ap.add_argument("--demand-from", type=float, default=1794890212.0)
    ap.add_argument("--k-claude", type=float, default=12.5, help="$-eq per 1 %% of a Claude Max 20x week")
    ap.add_argument("--k-codex", type=float, default=11.0, help="$-eq per 1 %% of the Codex week")
    ap.add_argument("--owner-rate", type=float, default=0.39, help="%% of account A's week per hour, owner sessions")
    ap.add_argument("--stop-usd", type=float, default=190.0)
    ap.add_argument("--burn-line", type=float, default=85.0)
    ap.add_argument("--drain-line", type=float, default=90.0)
    ap.add_argument("--drain-horizon-h", type=float, default=48.0)
    ap.add_argument("--drain-max-eq-h", type=float, default=15.0, help="$-eq per hour a pool's drain queue can run")
    ap.add_argument("--drain-line-sd", type=float, default=99.0, help="drain line of a spend-down pool (%% of its stop line)")
    ap.add_argument("--drain-horizon-sd-h", type=float, default=96.0)
    ap.add_argument("--no-spend-down", dest="spend_down", action="store_false")
    ap.add_argument("--gap", type=float, nargs=2, default=(1795212516.0, 1795377471.0),
                    help="epochs of a hole in the copied route log to fill from two days earlier; --gap 0 0 for none")
    ap.add_argument("--prior-flex", type=float, default=20.0)
    ap.add_argument("--prior-forced-codex", type=float, default=2.0)
    a = ap.parse_args()
    if a.gap and not any(a.gap):
        a.gap = None
    setup(a)
    src = json.load(open(os.path.join(a.data, "scarcity.json")))
    a.start = a.start or float(src["fetched_at"])
    g = src["lanes"]["anthropic-api"]["windows"][0]
    a.end = a.end or float(g["reset_epoch"])

    real_time = time.time
    sim = Sim(a)
    time.time = lambda: sim.now
    from cortados_router import core
    from cortados_router.provider import routing as prouting
    sc = core._scarcity_module()
    sys.modules["scarcity_mod"] = sc
    sc.current = lambda *x, **k: sim.reading
    sc.recent_burn_rates = lambda *x, **k: sim.burns()
    served = prouting.served_routes(core)
    claude_models = sorted({m for p, m in served if p.split("#", 1)[0] == "claude-cli"})
    api_pairs = tuple(("anthropic-api", m) for m in claude_models)
    t = core.table()
    ref = core.model_scores()["reference_index"]
    sim.sample()

    con = sqlite3.connect(os.path.join(a.data, DB_NAME))
    rows = con.execute("select turn_id, ts, band, degraded, lane, model, answers, tok_input, tok_output, "
                       "tok_cache_read, tok_cache_write from routes where ts >= ? order by ts", (a.demand_from,)).fetchall()
    week1 = a.demand_from + WEEK
    if a.gap:
        # The copied log has no rows at all from gap[0] to gap[1] (#143 audit). Fill it with the same hours two days
        # earlier, so the replayed week is not 27 % short; each copy is a distinct conversation (its own split key).
        g0, g1 = a.gap
        fill = [(r[0] + ":gapfill",) + (r[1] + 2 * 86400,) + tuple(r[2:]) for r in rows if g0 - 2 * 86400 <= r[1] < g1 - 2 * 86400]
        rows = sorted(rows + fill, key=lambda r: r[1])
    med = {}
    for r in rows:
        w = weighted(*r[7:11])
        if w:
            med.setdefault(r[2], []).append(w)
    med = {b: sorted(v)[len(v) // 2] for b, v in med.items()}
    stream = []
    for shift in (WEEK, 2 * WEEK):
        for r in rows:
            ts = r[1] + shift
            if a.start <= ts < a.end and (shift == WEEK or ts >= week1 + WEEK):
                stream.append((ts, shift, r))
    stream.sort(key=lambda x: x[0])
    t0 = real_time()
    out_calls = []
    for ts, shift, r in stream:
        sim.advance(ts)
        tid, _, band, degraded, lane, model, ans, *tok = r
        key = f"{tid}:{int(shift)}"
        w = weighted(*tok) or med.get(band, 0)
        if lane == "local-pro":
            out_calls.append({"kind": "local", "band": band, "pool": "local", "model": model})
            continue
        if band == "pin":
            fam = model or ""
            if fam.startswith("claude"):
                pool = pick_pin_account(sim, core, a, fam, key)
                if pool is None:
                    out_calls.append({"kind": "pin", "band": "pin", "pool": None, "model": fam})
                    continue
            else:
                pool = "codex"
            sim.charge(pool, fam, w, "pin")
            sim.recent.append((sim.now, "pin", pool, eq_usd(fam, w), eq_usd(fam, w), "fable" in fam))
            sim.mix[("pin", pool, fam)] += 1
            out_calls.append({"kind": "pin", "band": "pin", "pool": pool, "model": fam})
            continue
        if degraded:
            sim.charge("codex", "gpt-6.1-sol", w, "degraded")
            sim.recent.append((sim.now, "degraded", "codex", eq_usd("gpt-6.1-sol", w), eq_usd("gpt-6.1-sol", w), False))
            sim.mix[("R2*", "codex", "gpt-6.1-sol")] += 1
            out_calls.append({"kind": "degraded", "band": "R2", "pool": "codex", "model": "gpt-6.1-sol"})
            continue
        answers = json.loads(ans)
        allowed = served + (api_pairs if sim.api_on() else ())
        if a.policy == "before":
            res = core.resolve({"answers": answers, "degraded": False}, "interactive", input_fit={"input_tokens": 40000},
                               allowed_routes=allowed, execution=True, accounts_aware=True, split_key=key)
            ch = res["chosen"] or {}
            pool, m = ch.get("lane"), ch.get("model")
            base_m = m
            rb = res["band"]
        else:
            res = core.resolve({"answers": answers, "degraded": False}, "interactive", input_fit={"input_tokens": 40000},
                               allowed_routes=allowed, execution=True, accounts_aware=True)
            pool, m, base_m, rb = v2_choose(sim, core, t, ref, a, res, answers, allowed, key)
        if pool is None:
            out_calls.append({"kind": "routed", "band": rb, "pool": None, "model": None})
            continue
        sim.charge(pool, m, w, "routed")
        sim.recent.append((sim.now, "routed", pool, eq_usd(base_m or m, w), eq_usd(m, w), "fable" in (m or "")))
        sim.mix[(rb, pool, m)] += 1
        out_calls.append({"kind": "routed", "band": rb, "pool": pool, "model": m, "base": base_m,
                          "D": res.get("D"), "work_kind": answers.get("work_kind")})
    sim.advance(a.end)
    for lane in ("claude", "claude#b", "codex"):
        s = sim.st[lane]
        sim.window_ends.append((lane, "all (open at end)", sim.now, round(s["used"], 1)))
    result = {
        "policy": a.policy, "start": a.start, "end": a.end, "calls": len(stream), "elapsed_s": round(real_time() - t0),
        "params": {k: v for k, v in vars(a).items() if k not in ("repo", "data", "out")},
        "window_ends": [{"lane": l, "window": w, "at": time.strftime("%m-%d %H:%M", time.localtime(t_)), "used_pct": u}
                        for l, w, t_, u in sim.window_ends],
        "api_spent_usd": round(sim.st["anthropic-api"]["spent"], 2),
        "eq_by_pool_kind": {f"{p}|{k}": round(v, 1) for (p, k), v in sim.eq_by_pool_kind.items()},
        "calls_by_pool": dict(sim.calls_by_pool),
        "drain_eq": {p: round(v, 1) for p, v in sim.drain_eq.items()},
        "drain_hours": {p: round(v, 1) for p, v in sim.drain_hours.items()},
        "mix": [{"band": b, "pool": p, "model": m, "calls": n} for (b, p, m), n in sorted(sim.mix.items(), key=str)],
        "trace": sim.trace,
    }
    json.dump(result, open(a.out, "w"), indent=1)
    print(f"{a.policy}: {len(stream)} calls in {result['elapsed_s']} s -> {a.out}", file=sys.stderr)


def pick_pin_account(sim, core, a, model, key):
    health = core.lane_health()
    fable = "fable" in model
    cands = []
    for i, lane in enumerate(CLAUDE_LANES):
        hrow = health.get(lane) or {}
        if fable and "fable" in (hrow.get("pools") or {}):
            hrow = hrow["pools"]["fable"]
        if not hrow.get("ok", True):
            continue
        cands.append((lane, i, hrow))
    if not cands:
        return None
    if a.policy == "before":
        demote, expiring = core.static_thresholds()
        cands.sort(key=lambda x: core.static_scarcity_rank(x[2], demote, expiring) + (x[1],))
        return cands[0][0]
    # v2 (§5): the same controller as routed work; A gets a pin only by the share its rate earns
    rate = sim.ctrl.get("rate") or {}
    weights = {lane: rate.get(lane, 0.0) / PRICE.get(model, 8.0) for lane, _, _ in cands}
    pick = split_pick(key, weights)
    if pick:
        return pick
    end, cap = sim.ctrl.get("end") or {}, sim.ctrl.get("cap") or {}
    return min(cands, key=lambda x: (end.get(x[0], 0) - cap.get(x[0], 95), x[1]))[0]


def v2_choose(sim, core, t, ref, a, res, answers, allowed, key):
    band, D, req = res["band"], res.get("D"), res.get("required_index")
    use_case = None
    cands = core.scored_candidates(t, band, answers.get("work_kind"), True, api_lane=True)
    if not cands:
        ch = res["chosen"] or {}
        return ch.get("lane"), ch.get("model"), ch.get("model"), band
    use_case = cands[0]["use_case"]
    rows = []
    for c in cands:
        prov = "codex-cli" if c["provider"] == "openai-codex" else c["provider"]
        if (prov, c["model"]) not in allowed or c["lane"] not in POOLS:
            continue
        rows.append(dict(c, provider=prov))
    seen, uniq = set(), []
    for c in rows:
        if (c["lane"], c["model"]) not in seen:
            seen.add((c["lane"], c["model"]))
            uniq.append(c)
    rows = uniq
    health = core.lane_health()
    def ok(c):
        hrow = health.get(c["lane"]) or {}
        if "fable" in c["model"] and "fable" in (hrow.get("pools") or {}):
            hrow = hrow["pools"]["fable"]
        return hrow.get("ok", True)
    rows = [c for c in rows if ok(c)]
    if core.api_lane_refusal({"answers": answers, "degraded": False},
                             (t.get("selection") or {}).get("api_credit_lane") or {}):
        rows = [c for c in rows if c["lane"] != "anthropic-api"]
    floor_g = float(t["selection"].get("interactive_min_general_index", 0))
    if any(c["general"] >= floor_g for c in rows):
        rows = [c for c in rows if c["general"] >= floor_g]
    if req is None:
        req = 0.0
    target = req if any(c["index"] >= req for c in rows) else max((c["index"] for c in rows), default=0)
    q = [c for c in rows if c["index"] >= target]
    cw = t["selection"].get("ceiling_work") or {}
    if cw.get("require_frontier") and core.ceiling_work_applies(cw, use_case, D) and any(c.get("ceiling") for c in q):
        q = [c for c in q if c.get("ceiling")]
    if not q:
        return None, None, None, band
    by_pool = collections.defaultdict(list)
    for c in q:
        by_pool[c["lane"]].append(c)
    base = {p: min(cs, key=lambda c: (c["price"] or 99, -c["index"])) for p, cs in by_pool.items()}
    rate = sim.ctrl.get("rate") or {}
    weights = {p: rate.get(p, 0.0) / max(0.1, base[p]["price"] or 1.0) for p in by_pool}
    pick = split_pick(key, weights)
    if pick is None:
        end, cap = sim.ctrl.get("end") or {}, sim.ctrl.get("cap") or {}
        pick = min(by_pool, key=lambda p: (end.get(p, 0) - cap.get(p, 95), POOLS.index(p)))
    b0 = base[pick]
    m = b0
    if (sim.ctrl.get("burn") or {}).get(pick):
        # burn-up (§4): the strongest model of the band window (every candidate here already clears the requirement);
        # a model on a separately metered pool forecast to lapse (the account's Fable week) first
        bi = BANDS.index(band) if band in BANDS else 2
        fb = (sim.ctrl.get("fable_burn") or {}).get(pick, False)
        window = [c for c in by_pool[pick] if tier_of(c, ref, use_case) <= max(bi + 1, tier_of(b0, ref, use_case))]
        if window:
            m = max(window, key=lambda c: (fb and "fable" in c["model"], c["general"], c["price"] or 0))
    return pick, m["model"], b0["model"], band


if __name__ == "__main__":
    main()
