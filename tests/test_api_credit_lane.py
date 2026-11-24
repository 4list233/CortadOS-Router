"""#114 API credit lane, end to end through the provider: api/ pins and S1-routed conversations the expiry preference
moved to the lane, against a FAKE Anthropic API on loopback (the provider's pinned-origin opener is swapped for one that
delivers to it; nothing reaches api.anthropic.com), a throwaway ledger and grants file, and a fake key. Plus the routing
rule itself (routing.prefer_api_credit) and the live view's section."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import api_credits
from cortados_router.provider import anthropic_proxy, effect_boundary, live, routing, server
from tests.test_provider import EndpointHarness, TOKEN
from tests.provider_fakes import setUpModule, tearDownModule  # noqa: F401

KEY = "sk-ant-fake-credit-key-for-tests"
DAY = 86400.0


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def config(expires_in_days: float, amount: float = 200.0, limit: float = 200.0, **kw) -> dict:
    now = time.time()
    return {"org_monthly_limit_usd": limit, "prefer_within_days": 3, **kw,
            "grants": [{"name": "API credit (test)", "amount_usd": amount, "granted": iso(now - 20 * DAY),
                        "expires": iso(now + expires_in_days * DAY)}]}


class FakeAPI(BaseHTTPRequestHandler):
    """api.anthropic.com as far as the lane can tell: records every request, answers a message with fixed usage."""
    seen: list = []
    usage = {"input_tokens": 1000, "output_tokens": 100}
    status = 200
    gate = None                                # a threading.Event: set: every request waits for it (concurrency tests)
    inflight = 0
    peak = 0
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        cls = type(self)
        with cls.lock:
            cls.seen.append({"path": self.path, "key": self.headers.get("x-api-key"), "body": body})
            cls.inflight += 1
            cls.peak = max(cls.peak, cls.inflight)
        try:
            if cls.gate is not None:
                cls.gate.wait(10)
            if cls.status != 200:
                raw = json.dumps({"type": "error", "error": {"type": "api_error", "message": "boom"}}).encode()
                self.send_response(cls.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            n = len(cls.seen)
            content = [{"type": "text", "text": f"answer {n}"}]
            if body.get("stream"):
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                events = [{"type": "message_start", "message": {"id": f"m{n}", "type": "message", "role": "assistant",
                                                                 "model": body["model"], "content": [],
                                                                 "usage": {**cls.usage, "output_tokens": 1}}},
                          {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                          {"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": content[0]["text"]}},
                          {"type": "content_block_stop", "index": 0},
                          {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": cls.usage["output_tokens"]}},
                          {"type": "message_stop"}]
                for ev in events:
                    self.wfile.write(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
                return
            raw = json.dumps({"id": f"m{n}", "type": "message", "role": "assistant", "model": body["model"],
                              "content": content, "stop_reason": "end_turn", "usage": cls.usage}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        finally:
            with cls.lock:
                cls.inflight -= 1

    @classmethod
    def reset(cls):
        cls.seen, cls.status, cls.gate, cls.inflight, cls.peak = [], 200, None, 0, 0
        cls.usage = {"input_tokens": 1000, "output_tokens": 100}


class BurstServer(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


class LaneHarness(EndpointHarness):
    TOOLS = [{"name": "Read", "description": "read", "input_schema": {"type": "object"}}]
    HEADERS = {"X-Claude-Code-Session-Id": "api-credit-lane-test"}

    def setUp(self):
        # A burst of concurrent clients (the concurrency test sends 16 at once) must never overflow a listen backlog:
        # socketserver's default of 5 resets connections under load (#114 review round 1, finding 4). Both servers
        # listen with room for every request a test sends.
        backlog = mock.patch.object(server.ProviderServer, "request_queue_size", 128)
        backlog.start()
        self.addCleanup(backlog.stop)
        super().setUp()
        FakeAPI.reset()
        self.api = BurstServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.home = Path(tempfile.mkdtemp(prefix="cortados-api-credit-"))
        self.cfg = self.home / "api-credit.json"
        self.ledger = self.home / "api-credit-ledger.json"
        self.write_config(config(20))
        env = mock.patch.dict(os.environ, {"CORTADOS_HOME": str(self.home), "CORTADOS_API_CREDIT_CONFIG": str(self.cfg),
                                           "CORTADOS_ANTHROPIC_API_KEY": KEY})
        env.start()
        self.addCleanup(env.stop)
        port = self.api.server_port
        import socket

        def resolve(host, p):
            # The lane's real transport (anthropic_proxy._dispatch: gated connect and send) reaches the fake: only the
            # pinned origin is ever looked up, and it resolves to the loopback fake.
            assert (host, p) == ("api.anthropic.com", 443), (host, p)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        def plain(sock, host):
            assert host == "api.anthropic.com", host
            return sock                                  # the fake speaks plain HTTP
        for name, fake in (("_resolve", resolve), ("_tls_wrap", plain), ("_proxy_for", lambda host: None)):
            patch = mock.patch.object(anthropic_proxy, name, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.api.shutdown()
        self.api.server_close()
        super().tearDown()

    def write_config(self, cfg):
        self.cfg.write_text(json.dumps(cfg))

    def ledger_totals(self, settled=True):
        """(spent, reserved). The handler settles after the response is written (meanwhile the full reservation
        counts), so by default this waits for every reservation to be settled first."""
        deadline = time.time() + 5
        while settled and time.time() < deadline:
            data = json.loads(self.ledger.read_text())
            if not any(d["reserved"] for d in data["days"].values()):
                break
            time.sleep(0.01)
        data = json.loads(self.ledger.read_text())
        spent = sum(d["spent_usd"] for d in data["days"].values())
        reserved = sum(sum(d["reserved"].values()) for d in data["days"].values())
        return spent, reserved

    def error_of(self, body, headers=None):
        try:
            self.post(body, headers=headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]["message"]
        self.fail("expected an HTTP error")


class PinnedTurnTests(LaneHarness):
    BODY = {"model": "api/claude-sonnet-5", "max_tokens": 200, "messages": [{"role": "user", "content": "hi"}]}

    def test_a_pinned_turn_reaches_the_api_with_the_key_and_settles_the_real_cost(self):
        got = self.post(self.BODY)
        self.assertEqual(got["content"][0]["text"], "answer 1")
        (req,) = FakeAPI.seen
        self.assertEqual((req["key"], req["body"]["model"]), (KEY, "claude-sonnet-5"))
        spent, reserved = self.ledger_totals()
        self.assertEqual(reserved, 0)
        self.assertAlmostEqual(spent, (1000 * 2.0 + 100 * 10.0) / 1e6, places=6)
        self.assertNotIn(KEY, json.dumps(got))

    def test_the_stop_line_refuses_before_anything_is_sent(self):
        self.write_config(config(20, amount=1.0))         # stop line $0.95
        rid = api_credits.reserve(0.949, time.time())
        self.assertIsNotNone(rid)
        code, msg = self.error_of(self.BODY)
        self.assertEqual(code, 429)
        self.assertIn("stop line", msg)
        self.assertEqual(FakeAPI.seen, [])

    def test_a_broken_ledger_or_config_turns_the_lane_off(self):
        self.ledger.write_text("{corrupt")
        code, msg = self.error_of(self.BODY)
        self.assertEqual(code, 503)
        self.assertIn("accounting is unavailable", msg)
        self.ledger.unlink()
        self.cfg.write_text("[]")
        self.assertEqual(self.error_of(self.BODY)[0], 503)
        self.assertEqual(FakeAPI.seen, [])

    def test_an_expired_grant_turns_the_lane_off(self):
        self.write_config(config(0.02))                    # 29 minutes left: inside the hour before expiry
        code, msg = self.error_of(self.BODY)
        self.assertEqual(code, 429)
        self.assertIn("no counting grant", msg)
        self.assertEqual(FakeAPI.seen, [])

    def test_an_api_error_settles_zero(self):
        FakeAPI.status = 500
        self.assertEqual(self.error_of(self.BODY)[0], 500)
        self.assertEqual(self.ledger_totals(), (0.0, 0))

    def test_concurrent_requests_never_pass_the_stop_line(self):
        # 16 requests at once, each reserving its upper bound (Sonnet: its bytes + allowance at $4/M, 2000 output at
        # $10/M), against a grant whose stop line holds 4.5 of them: at most 4 can ever be admitted, however they
        # interleave, and the API sees no more than were admitted. The fake API holds every request until all have been answered
        # or refused, so admitted turns are all in flight together.
        body = {**self.BODY, "max_tokens": 2000}
        est = api_credits.estimate_max_cost(body, "claude-sonnet-5")
        amount = round(4.5 * est / 0.95, 6)                 # the stop line holds 4.5 reservations
        self.write_config(config(20, amount=amount))
        fits = int(0.95 * amount // est)
        self.assertEqual(fits, 4)
        FakeAPI.gate = threading.Event()
        codes: list = []

        start = threading.Barrier(16)

        def one():
            start.wait(10)                                  # all 16 connect at the same moment
            try:
                self.post(body)
                codes.append(200)
            except urllib.error.HTTPError as e:
                codes.append(e.code)
            except Exception as e:                          # a transport failure is a result too, never a lost one
                codes.append(type(e).__name__)

        threads = [threading.Thread(target=one) for _ in range(16)]
        for t in threads:
            t.start()
        deadline = time.time() + 10
        while time.time() < deadline and len(codes) + len(FakeAPI.seen) < 16:
            time.sleep(0.01)
        FakeAPI.gate.set()
        for t in threads:
            t.join(15)
        self.assertFalse([t for t in threads if t.is_alive()])
        self.assertEqual(sorted(codes, key=str), [200] * fits + [429] * (16 - fits))
        self.assertEqual(len(FakeAPI.seen), fits)
        spent, reserved = self.ledger_totals()
        self.assertEqual(reserved, 0)
        self.assertLessEqual(spent, 0.95 * amount)


    def test_a_request_carrying_fallbacks_is_refused_for_good_and_never_sent(self):
        # #114 review round 1, finding 1 (the review's repro): Fable 5.2 at max_tokens=1 with an Opus 5 fallback
        # allowing 128k fast output. Refused before any reservation, non-retryable.
        body = {**self.BODY, "model": "api/claude-fable-5-2", "max_tokens": 1,
                "fallbacks": [{"model": "claude-opus-5", "max_tokens": 128_000, "speed": "fast"}]}
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                              **self.HEADERS})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(cm.exception.headers.get("x-should-retry"), "false")
        self.assertIn("fallbacks", json.loads(cm.exception.read())["error"]["message"])
        self.assertEqual(FakeAPI.seen, [])
        self.assertFalse(self.ledger.exists())                  # nothing was ever reserved

    def test_a_turn_cut_off_at_its_time_cap_keeps_its_whole_reservation(self):
        # #114 review round 1, finding 3: the upstream never answers; the turn is cut at its cap (shrunk here) and the
        # reservation is settled in full, since a closed connection is no proof the API stopped billing.
        FakeAPI.gate = threading.Event()
        self.addCleanup(FakeAPI.gate.set)
        est = api_credits.estimate_max_cost(self.BODY, "claude-sonnet-5")
        with mock.patch.object(anthropic_proxy, "turn_cap_s", return_value=0.5):
            t0 = time.monotonic()
            code, msg = self.error_of(self.BODY)
        self.assertEqual(code, 504)
        self.assertIn("time cap", msg)
        self.assertLess(time.monotonic() - t0, 10)
        spent, reserved = self.ledger_totals()
        self.assertEqual(reserved, 0)
        self.assertGreaterEqual(spent, est)
        self.assertAlmostEqual(spent, est, places=5)

    def test_a_turn_whose_lookup_outlasts_its_cap_is_never_sent_and_settles_zero(self):
        # #114 review round 2, finding 1, end to end: the DNS lookup is held past the turn's cap and released after.
        # The late worker can no longer send (the gate shut at the cap), so the API never sees the request and the
        # reservation is settled at zero, not kept in full.
        release, returned = threading.Event(), threading.Event()
        real = anthropic_proxy._resolve

        def stalled(host, p):
            release.wait(10)
            try:
                return real(host, p)
            finally:
                returned.set()
        with mock.patch.object(anthropic_proxy, "turn_cap_s", return_value=0.3), \
                mock.patch.object(anthropic_proxy, "_resolve", stalled):
            code, msg = self.error_of(self.BODY)
            release.set()
            self.assertTrue(returned.wait(5))
            for t in threading.enumerate():
                if t.name == "cortados-api-open":
                    t.join(5)
        self.assertEqual(code, 504)
        self.assertIn("not dispatched", msg)
        time.sleep(0.2)
        self.assertEqual(FakeAPI.seen, [])
        spent, reserved = self.ledger_totals()
        self.assertEqual((spent, reserved), (0, 0))

    def test_the_reservation_is_taken_for_the_turns_time_cap_and_the_proxy_gets_its_deadline(self):
        seen = {}
        real_reserve, real_serve = api_credits.reserve, anthropic_proxy.serve

        def reserve(cost, now, *a, **kw):
            seen["duration_s"] = kw.get("duration_s")
            return real_reserve(cost, now, *a, **kw)

        def serve(*a, **kw):
            seen["left"] = kw["deadline"] - time.monotonic()
            return real_serve(*a, **kw)
        with mock.patch.object(api_credits, "reserve", reserve), mock.patch.object(anthropic_proxy, "serve", serve):
            self.post(self.BODY)
        self.assertEqual(seen["duration_s"], anthropic_proxy.turn_cap_s())
        self.assertLessEqual(seen["left"], anthropic_proxy.turn_cap_s())


class RoutedConversationTests(LaneHarness):
    """A conversation routing moved to the lane (the route routing.prefer_api_credit returns) is served by an ApiSession:
    relayed per turn, continued by its reply fingerprints, recorded once."""

    def setUp(self):
        super().setUp()
        self.routed: list = []
        self.executed: list = []

        def router(body, client, **kw):
            rid = uuid.uuid4().hex
            self.routed.append(rid)
            return {"router": "test", "provider": routing.API_PROVIDER, "model": "claude-sonnet-5", "route_id": rid,
                    "band": "R2", "bind_reason": routing.API_BIND_REASON,
                    "route": {"chosen": {"lane": routing.API_PROVIDER, "provider": routing.API_PROVIDER,
                                         "model": "claude-sonnet-5"}}}
        self.reg.router = router
        self.reg.executed = lambda route: self.executed.append(route["route_id"])
        # The preference only exists for an expiring grant: two days left, inside the 3-day window.
        self.write_config(config(2))
        self.rechecks: list = []
        self.recheck_answer = (True, "no outward effect (blast_radius 0, band R2)")

        def recheck(body, route):
            self.rechecks.append(route.get("route_id"))
            return self.recheck_answer
        self.reg.local_recheck = recheck

    def opener(self, stream=False):
        return {"model": "cortados", "max_tokens": 200, "tools": self.TOOLS, "stream": stream,
                "messages": [{"role": "user", "content": "hi"}]}

    def follow(self, first, content):
        return {**first, "messages": first["messages"] + [{"role": "assistant", "content": content},
                                                          {"role": "user", "content": "more"}]}

    def test_a_routed_conversation_runs_on_the_api_and_continues_there(self):
        first = self.opener()
        got = self.post(first)
        self.assertEqual(got["model"], "cortados")          # the client's own name, as on every routed lane
        (session,) = self.reg.sessions.values()
        self.assertIsInstance(session, server.ApiSession)
        second = self.post(self.follow(first, got["content"]))
        self.assertEqual(second["content"][0]["text"], "answer 2")
        self.assertEqual(len(self.routed), 1)                # routed once; the continuation found its session
        self.assertEqual(len(self.executed), 1)              # recorded once per conversation
        self.assertEqual([r["body"]["model"] for r in FakeAPI.seen], ["claude-sonnet-5"] * 2)
        self.assertEqual(len(FakeAPI.seen[1]["body"]["messages"]), 3)   # the whole history each turn
        spent, reserved = self.ledger_totals()
        self.assertEqual(reserved, 0)
        self.assertAlmostEqual(spent, 2 * (1000 * 2.0 + 100 * 10.0) / 1e6, places=6)

    def test_a_streamed_routed_turn_commits_its_history(self):
        first = self.opener(stream=True)
        raw = self.post(first)
        self.assertIn("message_stop", raw)
        self.assertIn('"model": "cortados"', raw)
        (session,) = self.reg.sessions.values()
        self.assertEqual(len(session.history), 1)

    def test_a_failed_opening_turn_drops_the_conversation(self):
        FakeAPI.status = 529
        self.assertEqual(self.error_of(self.opener())[0], 529)
        deadline = time.time() + 5
        while self.reg.sessions and time.time() < deadline:   # dropped after the answer is written
            time.sleep(0.01)
        self.assertEqual(len(self.reg.sessions), 0)
        self.assertEqual(self.ledger_totals(), (0.0, 0))

    def test_a_continuation_past_the_stop_line_is_refused_and_nothing_is_sent(self):
        first = self.opener()
        got = self.post(first)
        self.write_config(config(2, amount=0.001))           # now over the stop line
        code, msg = self.error_of(self.follow(first, got["content"]))
        self.assertEqual(code, 400)
        self.assertIn("hard stop", msg)
        self.assertEqual(len(FakeAPI.seen), 1)
        (session,) = self.reg.sessions.values()
        self.assertEqual(len(session.history), 1)            # the conversation is where it was

    # #114 review round 1, finding 2: every continuation passes the gate BEFORE anything is forwarded.
    def refused_continuation(self, headers=None):
        first = self.opener()
        got = self.post(first)
        follow = self.follow(first, got["content"])
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(follow).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                              **self.HEADERS, **(headers or {})})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(cm.exception.headers.get("x-should-retry"), "false")
        msg = json.loads(cm.exception.read())["error"]["message"]
        self.assertEqual(len(FakeAPI.seen), 1)                # only the opening turn ever reached the API
        (session,) = self.reg.sessions.values()
        self.assertEqual(len(session.history), 1)             # untouched: the same history may continue later
        return msg, follow

    def test_a_continuation_marked_sensitive_is_refused_before_it_is_sent(self):
        # The review's case: the opening was cleared, the next turn carries x-cortados-local: sensitive.
        msg, _ = self.refused_continuation({"x-cortados-local": "sensitive"})
        self.assertIn("sensitive", msg)

    def test_a_continuation_asking_for_local_is_refused(self):
        msg, _ = self.refused_continuation({"x-cortados-local": "prefer"})
        self.assertIn("prefer", msg)

    def test_a_continuation_a_detector_marks_is_refused(self):
        with mock.patch.object(routing, "request_sensitivity", return_value="keyword"):
            msg, _ = self.refused_continuation()
        self.assertIn("sensitivity detector", msg)

    def test_a_continuation_with_an_outward_effect_is_refused(self):
        self.recheck_answer = (False, "outward effect (S1 outward=true): never local")
        msg, _ = self.refused_continuation()
        self.assertIn("outward", msg)
        self.assertEqual(len(self.rechecks), 1)                # S1 rescored the continuation (the opening was routed)

    def test_a_continuation_after_the_grant_left_its_preference_window_is_refused(self):
        first = self.opener()
        got = self.post(first)
        self.write_config(config(20))                          # a new grant: nothing about to expire any more
        code, msg = self.error_of(self.follow(first, got["content"]))
        self.assertEqual(code, 400)
        self.assertIn("preference window", msg)
        self.assertEqual(len(FakeAPI.seen), 1)

    def test_a_continuation_without_a_recheck_fails_closed(self):
        self.reg.local_recheck = None
        msg, _ = self.refused_continuation()
        self.assertIn("re-check", msg)

    def test_an_admitted_continuation_was_rescored_first(self):
        first = self.opener()
        got = self.post(first)
        self.post(self.follow(first, got["content"]))
        self.assertEqual(len(FakeAPI.seen), 2)
        self.assertEqual(self.rechecks, [self.routed[0]])


class ScoredConversationTests(RoutedConversationTests):
    """#143: a conversation the lane WON as a scored candidate (bind API_SCORED_REASON) runs and continues on the API
    with no grant about to expire; every continuation still passes the gate (lane on, no detector, S1 re-check)."""

    def setUp(self):
        super().setUp()
        inner = self.reg.router

        def router(body, client, **kw):
            return {**inner(body, client, **kw), "bind_reason": routing.API_SCORED_REASON}
        self.reg.router = router
        self.write_config(config(20))                            # 20 days left: far outside the preference window

    def test_a_routed_conversation_runs_on_the_api_and_continues_there(self):
        super().test_a_routed_conversation_runs_on_the_api_and_continues_there()
        self.assertEqual(len(self.rechecks), 1)                  # the continuation was re-scored first

    def test_a_continuation_after_the_grant_left_its_preference_window_is_refused(self):
        # Not this bind's rule: a scored conversation needs the lane on, not a grant about to expire. It continues.
        first = self.opener()
        got = self.post(first)
        self.write_config(config(25))
        self.post(self.follow(first, got["content"]))
        self.assertEqual(len(FakeAPI.seen), 2)


class FakeCore:
    def __init__(self):
        self.logged: list = []

    def table(self):
        return {"candidates": {"R1": [{"provider": "cursor-cli", "model": "x"},
                                      {"provider": "claude-cli", "model": "claude-haiku-4-5-20251001"}],
                               "R2": [{"provider": "claude-cli", "model": "claude-sonnet-5"}], "R4": []}}

    def log_route(self, rid, meta, decision, route):
        self.logged.append((rid, route))
        return True


def local_route(band="R2", s1=("openai-codex", "gpt-5.6-terra"), bind="auto_route", **kw):
    """A route routing.choose moved to a local node (routing._moved's shape) and stamped by _stamp_sensitivity."""
    return {"router": "t", "provider": "local", "model": "qwen", "route_id": "r1", "band": band, "degraded": False,
            "decision": {"degraded": False, "answers": {"outward": False, "blast_radius": 0}},
            "route": {"chosen": {"lane": "local", "provider": "local", "model": "qwen"},
                      "runner_up": {"lane": s1[0], "provider": s1[0], "model": s1[1]}, "hard_rules": []},
            "local": {"node": {}, "model": {}}, "bind_reason": bind, "context_length": 32768,
            routing.CLOUD_REROUTE_OK: True, **kw}


ON = {"on": True, "expiring_soon": True, "headroom_usd": 100.0, "prefer_within_days": 3.0,
      "grant": {"expires_at": time.time() + 2 * DAY}}
BODY = {"model": "cortados", "max_tokens": 100, "messages": [{"role": "user", "content": "rename a variable"}]}


class PreferenceRuleTests(unittest.TestCase):
    def prefer(self, r, state=ON, body=BODY, excluded=lambda m: False):
        core = FakeCore()
        with mock.patch("cortados_router.api_credits.state", return_value=state):
            out = routing.prefer_api_credit(core, body, r, client="t", conversation_id="c", excluded=excluded)
        return out, core

    def test_expiring_credit_takes_cost_bound_local_work(self):
        out, core = self.prefer(local_route())
        self.assertEqual((out["provider"], out["model"]), (routing.API_PROVIDER, "claude-sonnet-5"))   # band's Claude
        self.assertNotIn("local", out)
        self.assertEqual(out["bind_reason"], routing.API_BIND_REASON)
        self.assertEqual(out["route"]["chosen"]["lane"], routing.API_PROVIDER)
        self.assertEqual(out["route"]["runner_up"]["lane"], "local")
        self.assertEqual(core.logged[0][0], "r1")                       # the routes row is rewritten, same route id
        self.assertIn("api credit expiry preference", out["route"]["hard_rules"][-1])
        out, _ = self.prefer(local_route(band="R1"))
        self.assertEqual(out["model"], "claude-haiku-4-5-20251001")
        out, _ = self.prefer(local_route(s1=("claude-cli#b", "claude-opus-5")))
        self.assertEqual(out["model"], "claude-opus-5")                   # S1's own Claude choice is kept
        out, _ = self.prefer(local_route(bind="index"))
        self.assertEqual(out["provider"], routing.API_PROVIDER)

    def test_everything_else_stays_where_it_was(self):
        cases = [
            (local_route(), {**ON, "expiring_soon": False}),            # no grant close to expiry
            (local_route(), {**ON, "on": False}),                        # lane off
            (local_route(), {**ON, "headroom_usd": 0.0001}),             # does not fit under the stop line
            (local_route(bind="prefer"), ON),                            # the client asked for local
            (local_route(bind="sensitive_flag"), ON), (local_route(bind="keyword"), ON),
            (local_route(**{routing.CLOUD_REROUTE_OK: False}), ON),      # a detector marked it
            (local_route(band="R4"), ON),                                # no Claude model for the band
            (local_route(degraded=True), ON),
            (local_route(decision={"degraded": False, "answers": {"outward": True, "blast_radius": 0}}), ON),
            ({**local_route(), "local": None}, ON),                       # not a local move at all
            (local_route(pinned=True), ON),
        ]
        for r, st in cases:
            out, core = self.prefer(r, st)
            self.assertIs(out, r, (r.get("bind_reason"), st))
            self.assertEqual(core.logged, [])
        r = local_route()
        self.assertIs(self.prefer(r, excluded=lambda m: m == "claude-sonnet-5")[0], r)
        # server-side fallbacks (#114 review round 1): never moved, the request stays local
        self.assertIs(self.prefer(r, body={**BODY, "fallbacks": [{"model": "claude-opus-5"}]})[0], r)
        img = {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}
        self.assertIs(self.prefer(r, body={**BODY, "messages": [{"role": "user", "content": [img]}]})[0], r)
        with mock.patch("cortados_router.api_credits.state", side_effect=RuntimeError("x")):
            self.assertIs(routing.prefer_api_credit(FakeCore(), BODY, r, client="t", conversation_id="c"), r)

    def test_only_cost_bind_reasons_are_preferred(self):
        for reason in routing.API_PREFER_REASONS:
            self.assertEqual(effect_boundary.profile_for(reason), effect_boundary.COST)

    def test_choose_applies_it_after_the_local_move(self):
        moved = local_route()
        with mock.patch.object(routing.execution_routing, "select", return_value={**moved, "local": None,
                                                                                 "provider": "codex-cli"}), \
             mock.patch.object(routing, "served_routes", return_value=()), \
             mock.patch.object(routing, "sensitive_local", side_effect=lambda core, body, r, **kw: r), \
             mock.patch.object(routing, "auto_local", return_value=moved), \
             mock.patch.object(routing, "request_sensitivity", return_value=None), \
             mock.patch("cortados_router.api_credits.state", return_value=ON):
            out = routing.choose(FakeCore(), BODY, client="t", conversation_id="c")
        self.assertEqual(out["provider"], routing.API_PROVIDER)

    def test_the_api_lane_is_exempt_from_the_subscription_floor(self):
        self.assertTrue(routing._floor_exempt(routing.API_PROVIDER))


class LiveSectionTests(unittest.TestCase):
    def test_the_section_carries_state_numbers_and_never_the_key(self):
        now = time.time()
        st = {"on": True, "reason": None, "alert_level": 50, "pct_used": 52.5, "headroom_usd": 85.0,
              "reserved_usd": 1.0, "open_reservations": 1, "expiring_soon": True, "prefer_within_days": 3.0,
              "grant": {"amount_usd": 200.0, "used_usd": 105.0, "left_usd": 95.0, "stop_line_usd": 190.0,
                        "expires_at": now + 7200},
              "month": {"limit_usd": 200.0, "used_usd": 105.0, "stop_line_usd": 190.0, "resets_at": now + DAY},
              "grants": [{"name": "g", "amount_usd": 200.0, "expires_at": now + 7200, "counting": True}]}
        sec = live.api_credit_section(st, now)
        self.assertEqual((sec["status"], sec["on"], sec["alert_level"], sec["grant"]["expires_in_h"]),
                         ("ok", True, 50, 2.0))
        self.assertTrue(sec["expiring_soon"])
        self.assertEqual(live.api_credit_section(None, now)["status"], "error")

    def test_collect_publishes_it_and_a_failure_is_its_own_error(self):
        args = dict(now=time.time(), child_events=lambda: [], office_view=lambda ev: {}, tasks_view=lambda: {},
                    usage_view=lambda: {}, heartbeats=lambda: {})
        snap = live.collect(**args, api_credit=lambda: {"on": False, "reason": "no counting grant", "alert_level": 0})
        self.assertEqual((snap["api_credit"]["on"], snap["api_credit"]["reason"]), (False, "no counting grant"))
        self.assertIn("api_credit_alert", snap["overview"])
        snap = live.collect(**args, api_credit=lambda: 1 / 0)
        self.assertEqual(snap["api_credit"]["status"], "error")
        self.assertNotIn("api_credit", live.collect(**args))

    def test_the_real_state_is_metadata_only(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"CORTADOS_HOME": d, "CORTADOS_API_CREDIT_CONFIG": str(Path(d) / "c.json"),
                                             "CORTADOS_ANTHROPIC_API_KEY": KEY}):
            (Path(d) / "c.json").write_text(json.dumps(config(2)))
            sec = live.api_credit_section(api_credits.state(time.time()), time.time())
        self.assertTrue(sec["on"])
        self.assertTrue(sec["expiring_soon"])
        self.assertNotIn(KEY, json.dumps(sec))
        self.assertNotIn(d, json.dumps(sec))


if __name__ == "__main__":
    unittest.main()
