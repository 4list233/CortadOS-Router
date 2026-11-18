"""#126 review round 3: a text turn's reservation is released only when vllm-mlx confirms the turn left its scheduler
(P1), and the guard checks every pinned file on disk before anything of vllm-mlx, mlx-lm or mlx is imported (P2b).

P1 has two sides, both with fakes only (no vllm-mlx, no MLX, no model):
  * the gate (cortados_router/local_gate.py): a fake guard upstream holds each ticketed turn "in its scheduler" until
    the test lets it go, and answers RETIRE_PATH as the guard does;
  * the guard's TicketLedger, middleware and install_tickets (integrations/vllm-mlx/cortados_vllm_mlx_guard.py) on a
    fake scheduler that keeps 0.6.0's bookkeeping (waiting queue, running map, request-to-uid map, deferred aborts)."""
from __future__ import annotations

import asyncio
import collections
import http.client
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cortados_router import local_gate, local_governor
from tests.test_local_mlx import MLX, SERVED, TAG, TOKEN, FixedGovernor, wait_for
from tests.test_pro_vision import VisionGuardBase
from tests.test_text_prefill import SERVE_TEXT, write_lm
from tests.test_vllm_mlx_guard import load_guard

GIB = 2 ** 30


# ------------------------------------------------------------------------------------------- the gate's side (P1)


class GuardState:
    """What the fake guard's vllm-mlx holds: `live` tickets are in its scheduler (rows in the batch), until released."""

    def __init__(self):
        self.lock = threading.Lock()
        self.live: set = set()
        self.tickets: list = []                  # the ticket header of each POST /v1/messages, in order
        self.polls: list = []                    # the ids of each retirement poll
        self.mode = "complete"                   # complete | hold | error | stream
        self.retire_status = 200                 # 503: a guard that cannot answer (hung)
        self.hold = threading.Event()
        self.arrived = threading.Semaphore(0)
        self.fences: list = []                   # the body of each fence query
        self.fence_status = 200                  # 503: a guard that cannot answer the fence
        self.floor = 0                           # the highest floor_ms a fence query set
        self.boot = "b"                          # the guard process (a new value: vllm-mlx restarted)

    def release(self, *tickets):
        with self.lock:
            for t in tickets or list(self.live):
                self.live.discard(t)


class FakeGuard(BaseHTTPRequestHandler):
    state: GuardState = GuardState()

    def log_message(self, *a):
        pass

    def _json(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._json(200, {"object": "list", "data": [{"id": SERVED, "object": "model"}]})

    def do_POST(self):
        st = FakeGuard.state
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path == local_gate.FENCE_PATH:
            st.fences.append(body)
            if st.fence_status != 200:
                return self._json(st.fence_status, {"detail": "busy"})
            with st.lock:                         # live tickets the gate does not know: a previous gate's rows
                unknown = [t for t in st.live if t not in body["known"]]
                st.floor = max(st.floor, body["floor_ms"])
                return self._json(200, {"clear": not unknown, "unknown": len(unknown), "boot": st.boot})
        if self.path == local_gate.RETIRE_PATH:
            st.polls.append(list(body["tickets"]))
            if st.retire_status != 200:
                return self._json(st.retire_status, {"detail": "busy"})
            with st.lock:                         # an id it does not hold (or never saw) is retired
                return self._json(200, {"retired": [t for t in body["tickets"] if t not in st.live],
                                        "boot": st.boot})
        ticket = self.headers.get(local_gate.TICKET_HEADER)
        with st.lock:
            st.tickets.append(ticket)
            st.live.add(ticket)                   # queued in the scheduler: rows from here until released
        st.arrived.release()
        if st.mode == "hold":
            st.hold.wait(10)
        if st.mode == "error":
            return self._json(500, {"detail": "the scheduler aborted the request"})
        if st.mode == "stream":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            try:
                for i in range(200):
                    self.wfile.write(f"event: ping\ndata: {i}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.02)
            except OSError:
                pass
            self.close_connection = True
            return
        self._json(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": SERVED,
                         "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                         "usage": {"input_tokens": 3, "output_tokens": 1}})


def msg(max_tokens=16, stream=False):
    return {"model": TAG, "max_tokens": max_tokens, "stream": stream, "messages": [{"role": "user", "content": "hi"}]}


class RetireGateBase(unittest.TestCase):
    READ_TIMEOUT = 5.0
    GOVERNOR = {"idle_s": 3600.0}                 # FixedGovernor kwargs (owner idle: count 8); None: no governor

    def setUp(self):
        FakeGuard.state = self.st = GuardState()
        allowed = patch.object(local_gate, "pro_local_allowed", return_value=(True, "test AC + on"))
        allowed.start()
        self.addCleanup(allowed.stop)
        fast = patch.object(local_gate, "RETIRE_POLL_S", 0.02)
        fast.start()
        self.addCleanup(fast.stop)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeGuard)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self._stop, self.upstream)
        self.gate = self.make_gate(self.upstream.server_port)
        self.url = f"http://127.0.0.1:{self.gate.server_port}"

    def make_gate(self, port):
        gov = FixedGovernor(**self.GOVERNOR) if self.GOVERNOR is not None else None
        gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN, upstream=f"http://127.0.0.1:{port}",
                                     read_timeout=self.READ_TIMEOUT, backend={**MLX, "governor": None},
                                     admission=local_governor.Admission(gov))
        threading.Thread(target=gate.serve_forever, daemon=True).start()
        self.addCleanup(self._stop, gate)
        return gate

    @staticmethod
    def _stop(server):
        FakeGuard.state.hold.set()
        server.shutdown()
        server.server_close()

    def call(self, body, path="/v1/messages", timeout=10):
        conn = http.client.HTTPConnection("127.0.0.1", self.gate.server_port, timeout=timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request("POST" if body is not None else "GET", path, body=data,
                         headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}"})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    @property
    def adm(self):
        return self.gate.admission

    def report(self):
        code, raw = self.call(None, "/api/tags")
        self.assertEqual(code, 200)
        return json.loads(raw)[local_gate.GATE_KEY]

    def polled(self, n=2):
        """At least n more retirement polls have been answered (the gate asked and was told: not yet)."""
        start = len(self.st.polls)
        return wait_for(lambda: len(self.st.polls) >= start + n)


class GateRetirementTests(RetireGateBase):
    def test_round3_repro_a_gate_timeout_keeps_the_64k_rows_reserved(self):
        # the reviewer's repro: a 64k turn held upstream, the gate times out (502), then 7 short turns arrive before
        # the backend lets the 64k row go. Before: 8 rows upstream against 7 tickets, high-water 1,170, 12.05 GiB
        # reserved, while 8 rows at 64k need 31.25 GiB of KV against a 25 GiB budget
        self.gate.read_timeout = 0.5
        self.st.mode = "hold"
        code, _ = self.call(msg(max_tokens=64000))
        self.assertEqual(code, 502)                                     # the gate gave up; the backend did not
        self.assertEqual(len(self.st.live), 1)
        self.assertTrue(self.polled())
        self.assertEqual((self.adm.inflight, self.adm.retiring), (1, 1))
        self.assertGreaterEqual(self.adm._high_water, 64000)
        long_load = self.adm._loads[0]
        self.st.mode = "complete"                                       # the short turns answer at once, rows kept
        codes = [self.call(msg())[0] for _ in range(7)]
        self.assertIn(529, codes)                                       # the 64k width refuses most of them
        cfg = self.adm.governor.cfg
        with self.st.lock:
            held = len(self.st.live)
        # every row the backend holds has a gate ticket, and the reservation covers them at the 64k width
        self.assertEqual(self.adm.inflight, held)
        self.assertIn(long_load, self.adm._loads)
        self.assertGreaterEqual(self.adm.reserved, local_governor.text_set_bytes(
            cfg, self.adm._loads, self.adm._high_water)["total"])
        self.assertLessEqual(held * (64001 + 256) * cfg["kv_bytes_per_token"], self.adm.reserved)
        self.assertLessEqual(self.adm.reserved, local_governor.available_bytes(cfg))
        # the backend lets everything go: only now is the reservation returned, the set reset
        self.st.hold.set()
        self.st.release()
        self.assertTrue(wait_for(lambda: (self.adm.inflight, self.adm.reserved) == (0, 0)))
        self.assertEqual((self.adm.retiring, self.adm._high_water), (0, 0))
        self.assertEqual(self.gate.retirer.pending(), [])

    def test_normal_completion_retires_only_after_the_confirmation(self):
        code, _ = self.call(msg())
        self.assertEqual(code, 200)
        self.assertTrue(self.polled())                                   # asked, and told "not yet"
        self.assertEqual((self.adm.inflight, self.adm.retiring), (1, 1))
        rep = self.report()
        self.assertEqual((rep["inflight"], rep["retiring"]), (1, 1))
        self.assertIn("await backend release", rep["reason"])
        self.st.release()
        self.assertTrue(wait_for(lambda: (self.adm.inflight, self.adm.retiring, self.adm.reserved) == (0, 0, 0)))
        self.assertEqual(self.report()["retiring"], 0)

    def test_a_client_disconnect_retires_only_after_the_confirmation(self):
        self.st.mode = "stream"
        sock = socket.create_connection(("127.0.0.1", self.gate.server_port))
        raw = json.dumps(msg(stream=True)).encode()
        sock.sendall(b"POST /v1/messages HTTP/1.1\r\nhost: x\r\ncontent-type: application/json\r\n"
                     + f"authorization: Bearer {TOKEN}\r\ncontent-length: {len(raw)}\r\n\r\n".encode() + raw)
        self.assertTrue(sock.recv(64))                                   # the stream started
        sock.close()                                                     # the client goes away mid-stream
        self.assertTrue(wait_for(lambda: self.adm.retiring == 1))
        self.assertTrue(self.polled())
        self.assertEqual(self.adm.inflight, 1)
        self.st.release()
        self.assertTrue(wait_for(lambda: (self.adm.inflight, self.adm.reserved) == (0, 0)))

    def test_a_backend_abort_retires_only_after_the_confirmation(self):
        self.st.mode = "error"
        self.assertEqual(self.call(msg())[0], 500)
        self.assertTrue(self.polled())
        self.assertEqual((self.adm.inflight, self.adm.retiring), (1, 1))
        self.st.release()
        self.assertTrue(wait_for(lambda: self.adm.inflight == 0))

    def test_a_hung_guard_keeps_the_ticket_reserved_and_the_node_busy(self):
        self.st.retire_status = 503                                     # the guard cannot answer
        self.assertEqual(self.call(msg())[0], 200)
        self.st.release()                                               # even a backend that let it go: unconfirmed
        self.assertTrue(self.polled(3))
        self.assertEqual((self.adm.inflight, self.adm.retiring), (1, 1))
        self.assertIsNotNone(self.gate.retirer.last_error)
        self.assertGreater(self.adm.reserved, 0)
        self.assertEqual(self.report()["retiring"], 1)
        # unreachable altogether: still reserved
        self.upstream.shutdown()
        self.upstream.server_close()
        time.sleep(0.2)
        self.assertEqual((self.adm.inflight, self.adm.retiring), (1, 1))

    def test_a_backend_restart_clears_every_retiring_ticket(self):
        for _ in range(3):
            self.assertEqual(self.call(msg())[0], 200)
        self.assertTrue(self.polled())
        self.assertEqual(self.adm.retiring, 3)
        # a new vllm-mlx process: it never saw the old ids (its guard tombstones and retires them)
        FakeGuard.state.live = set()
        self.assertTrue(wait_for(lambda: (self.adm.inflight, self.adm.retiring, self.adm.reserved) == (0, 0, 0)))

    def test_a_connect_that_failed_sent_nothing_and_is_released_at_once(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        gate = self.make_gate(port)

        def post():
            conn = http.client.HTTPConnection("127.0.0.1", gate.server_port, timeout=10)
            conn.request("POST", "/v1/messages", body=json.dumps(msg()).encode(),
                         headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}"})
            try:
                r = conn.getresponse()
                return r.status, json.loads(r.read())
            finally:
                conn.close()
        # #126 round 4: a guard that cannot be reached cannot lift the startup fence: busy, nothing sent
        code, data = post()
        self.assertEqual(code, 529)
        self.assertIn("did not confirm the fence", data[local_gate.GATE_KEY]["fence"])
        # the backend went away after the fence lifted: the connect fails, nothing was sent, released at once
        gate.retirer.stop()
        self.assertTrue(wait_for(lambda: not gate.retirer._thread.is_alive()))
        with gate.retirer._cv:
            gate.retirer._fence = None
        self.assertEqual(post()[0], 502)
        self.assertTrue(wait_for(lambda: gate.admission.inflight == 0))
        self.assertEqual((gate.admission.retiring, gate.retirer.pending()), (0, []))

    def test_each_turn_carries_a_fresh_ticket_and_the_probe_none(self):
        for _ in range(3):
            self.assertEqual(self.call(msg())[0], 200)
        self.report()
        guard = load_guard()
        self.assertEqual(len(set(self.st.tickets)), 3)
        for t in self.st.tickets:
            self.assertIsNone(guard._ticket_problem(t, time.time()))   # the guard's own format check
        self.assertEqual((local_gate.TICKET_HEADER, local_gate.RETIRE_PATH), (guard.TICKET_HEADER, guard.RETIRE_PATH))


class UngovernedRetirementTests(RetireGateBase):
    GOVERNOR = None                                # a vllm-mlx text node without a governor still sends tickets

    def test_tickets_and_retirement_without_a_governor(self):
        self.assertEqual(self.call(msg())[0], 200)
        self.assertEqual(len(self.st.tickets), 1)
        self.assertIsNotNone(self.st.tickets[0])
        self.assertTrue(self.polled())
        self.assertEqual((self.adm.inflight, self.adm.retiring), (1, 1))
        self.st.release()
        self.assertTrue(wait_for(lambda: self.adm.inflight == 0))


class FenceGateTests(RetireGateBase):
    """#126 review round 4 (P1b): a gate admits no text turn until the guard confirms no row of a turn it does not know
    remains (FENCE_PATH), at start and whenever it cannot vouch for the backend's state."""

    def foreign(self, *tickets):
        with self.st.lock:
            self.st.live.update(tickets)

    def test_round4_repro_a_restarted_gate_admits_nothing_beside_a_previous_gates_rows(self):
        # the previous gate's 64k turn survives in the batch; the restarted gate knows nothing of it
        FakeGuard.state = self.st = GuardState()
        self.foreign("old-64k-turn")
        gate = self.make_gate(self.upstream.server_port)
        self.gate, self.url = gate, f"http://127.0.0.1:{gate.server_port}"
        for _ in range(2):                                              # two more 64k turns: both refused, busy
            code, raw = self.call(msg(max_tokens=65536))
            self.assertEqual(code, 529)
            cap = json.loads(raw)[local_gate.GATE_KEY]
            self.assertIn("does not know", cap["fence"])
            self.assertIn("does not know", json.loads(raw)["error"]["message"])
        self.assertEqual((self.adm.inflight, self.adm.reserved), (0, 0))
        self.assertEqual(self.st.tickets, [])                           # nothing reached the backend
        self.assertIn("does not know", self.report()["fence"])
        self.assertTrue(self.st.fences)
        self.assertEqual(self.st.fences[-1], {"floor_ms": gate.retirer.start_ms, "known": []})
        self.st.release("old-64k-turn")                                 # its rows leave the scheduler
        self.assertTrue(wait_for(lambda: gate.retirer.fenced() is None))
        self.assertEqual(self.call(msg())[0], 200)
        self.assertIsNone(self.report()["fence"])

    def test_the_fence_lifts_when_the_backend_is_empty_and_tickets_are_minted_after_its_floor(self):
        self.assertEqual(self.call(msg())[0], 200)
        self.assertEqual(self.st.fences[0], {"floor_ms": self.gate.retirer.start_ms, "known": []})
        self.assertIsNone(self.report()["fence"])
        self.assertGreaterEqual(int(self.st.tickets[0][:13]), self.gate.retirer.start_ms)
        # a clock that went back still mints at or after the floor (the guard refuses ids minted before it)
        self.assertEqual(local_gate.new_ticket_id(now=1.0, floor_ms=5_000_000_000_000)[:13], "5000000000000")

    def test_a_hung_or_unreachable_guard_keeps_the_fence_up_visibly(self):
        FakeGuard.state = self.st = GuardState()
        self.st.fence_status = 503                                      # a guard that cannot answer
        gate = self.make_gate(self.upstream.server_port)
        self.gate = gate
        for _ in range(3):
            code, raw = self.call(msg())
            self.assertEqual(code, 529)
            self.assertIn("did not confirm the fence", json.loads(raw)[local_gate.GATE_KEY]["fence"])
        self.assertEqual(self.st.tickets, [])
        self.assertTrue(wait_for(lambda: len(self.st.fences) >= 2))     # the thread keeps asking
        self.upstream.shutdown()                                        # unreachable altogether
        self.upstream.server_close()
        time.sleep(0.3)
        self.assertIn("did not confirm the fence", gate.retirer.fenced())
        self.assertIn("did not confirm the fence", gate.capacity()["fence"])

    def test_lost_contact_fences_until_the_guard_answers_again(self):
        self.assertEqual(self.call(msg())[0], 200)                      # retiring: its rows are still live
        self.st.retire_status = self.st.fence_status = 503
        self.assertTrue(wait_for(lambda: self.gate.retirer.fenced() is not None))
        self.assertIn("lost contact", self.report()["fence"] or "")
        self.assertEqual(self.call(msg())[0], 529)
        self.assertEqual(self.adm.retiring, 1)                          # still reserved meanwhile
        self.st.retire_status = self.st.fence_status = 200              # the guard answers: its row is known
        self.assertTrue(wait_for(lambda: self.gate.retirer.fenced() is None))
        self.assertEqual(self.call(msg())[0], 200)

    def test_a_guard_restart_fences_again_and_lifts_with_the_turns_this_gate_knows(self):
        self.assertEqual(self.call(msg())[0], 200)                      # retiring (live in the batch)
        self.st.mode = "hold"
        held = threading.Thread(target=self.call, args=(msg(),))
        held.start()
        self.addCleanup(held.join, 10)
        self.assertTrue(self.st.arrived.acquire(timeout=5))
        self.assertTrue(self.st.arrived.acquire(timeout=5))
        fences = len(self.st.fences)
        with self.st.lock:                                              # a new vllm-mlx process, mid-turn
            self.st.boot = "b2"
        self.assertTrue(wait_for(lambda: len(self.st.fences) > fences))
        self.assertTrue(wait_for(lambda: self.gate.retirer.fenced() is None))
        last = self.st.fences[-1]
        self.assertIn(self.st.tickets[1], last["known"])                # the in-flight turn is known
        self.assertEqual(self.gate.retirer.boot, "b2")
        self.st.hold.set()
        held.join(10)
        self.st.release()                                               # the new process holds none of them
        self.assertTrue(wait_for(lambda: (self.adm.inflight, self.adm.retiring) == (0, 0)))


class RetirerUnitTests(unittest.TestCase):
    def test_only_the_ids_asked_and_confirmed_are_released(self):
        adm = local_governor.Admission(FixedGovernor())
        answers = collections.deque()
        r = local_gate.Retirer("127.0.0.1", 9, call=lambda path, body: answers.popleft(), start=False)
        t1 = adm.try_acquire(load=adm.text_load(100, 1))[0]
        t2 = adm.try_acquire(load=adm.text_load(100, 1))[0]
        r.hand_over(t1, "a")
        r.hand_over(t2, "b")
        self.assertEqual((adm.inflight, adm.retiring), (2, 2))
        for bad in ({"retired": "a"}, ["a"], {"nope": []}):
            answers.append(bad)
            with self.assertRaises(ValueError):
                r.poll_once()
        self.assertEqual((adm.inflight, adm.retiring), (2, 2))          # a bad answer releases nothing
        answers.append({"retired": ["zz", "b", 3]})                     # an id not asked for is ignored
        self.assertEqual(r.poll_once(), 1)
        self.assertEqual((adm.inflight, adm.retiring, r.pending()), (1, 1, ["a"]))
        answers.append({"retired": ["a"]})
        self.assertEqual(r.poll_once(), 1)
        self.assertEqual((adm.inflight, adm.retiring, adm.reserved), (0, 0, 0))


# --------------------------------------------------------------------------------------------- the guard's side (P1)


class Req:
    def __init__(self, request_id, prompt, sampling_params=None):
        self.request_id, self.prompt, self.sampling_params = request_id, prompt, sampling_params
        self.prompt_token_ids, self.finished = None, False

    def is_finished(self):
        return self.finished


class Sched:
    """vllm-mlx 0.6.0's Scheduler bookkeeping: add queues; abort is deferred to the next step, which processes it
    first, then schedules the waiting into the batch and finishes what `done` names; remove_finished_request (the
    engine's cleanup) drops the requests entry only."""

    def __init__(self):
        self.waiting, self.running, self.request_id_to_uid, self.requests = collections.deque(), {}, {}, {}
        self._pending_abort_ids, self.done, self.refuse = set(), set(), False
        self.after_step = None                                           # a hook run at the end of a step

    def add_request(self, request):
        if self.refuse:                                                  # e.g. the prompt cap: nothing queued
            raise ValueError("refused before the queue")
        self.requests[request.request_id] = request
        self.waiting.append(request)

    def abort_request(self, rid):
        self._pending_abort_ids.add(rid)

    def remove_finished_request(self, rid):
        return self.requests.pop(rid, None)

    def _drop(self, rid):
        self.running.pop(rid, None)
        self.request_id_to_uid.pop(rid, None)
        for r in list(self.waiting):
            if r.request_id == rid:
                self.waiting.remove(r)
        req = self.requests.get(rid)
        if req is not None:
            req.finished = True

    def step(self):
        while self._pending_abort_ids:
            self._drop(self._pending_abort_ids.pop())
        while self.waiting:
            r = self.waiting.popleft()
            self.running[r.request_id] = r
            self.request_id_to_uid[r.request_id] = len(self.request_id_to_uid)
        for rid in list(self.done):
            self._drop(rid)
        self.done.clear()
        if self.after_step is not None:
            self.after_step()

    def reset(self):
        for rid in list(self.requests):
            self._drop(rid)
        self.waiting.clear()
        self.running.clear()
        self.request_id_to_uid.clear()


class Engine:
    """EngineCore.add_request's shape: the id, the scheduler's add, and abort + cleanup when it raises."""

    def __init__(self, scheduler):
        self.scheduler = scheduler

    async def add_request(self, prompt, sampling_params=None, request_id=None, images=None, videos=None,
                          prefix_boundary=0):
        try:
            self.scheduler.add_request(Req(request_id or "auto", prompt, sampling_params))
        except BaseException:
            self.scheduler.abort_request(request_id)
            raise
        return request_id


class App:
    def __init__(self):
        self.middleware = []

    def add_middleware(self, cls, **kw):
        self.middleware.insert(0, (cls, kw))


class HTTPError(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.guard = load_guard()
        self.now = [time.time()]
        self.ledger = self.guard.TicketLedger(clock=lambda: self.now[0])
        # fresh classes per test (install_tickets patches them in place)
        self.Sched = type("Sched", (Sched,), {k: Sched.__dict__[k] for k in ("add_request", "step", "reset")})
        self.Engine = type("Engine", (Engine,), {"add_request": Engine.__dict__["add_request"]})
        self.app = App()
        self.current = self.guard.install_tickets(self.ledger, self.Sched, self.Engine, self.app, HTTPError)
        self.sched = self.Sched()
        self.engine = self.Engine(self.sched)
        cls, kw = self.app.middleware[0]
        self.calls, self.rids = [], []
        self.mw = cls(self.handler, **kw)

    def tid(self, age=0.0):
        return local_gate.new_ticket_id(self.now[0] - age)

    async def handler(self, scope, receive, send):
        """The server's /v1/messages: the engine's add_request in the request's own task (any other path: a read)."""
        if scope["path"] == "/v1/messages":
            self.calls.append(self.current.get())
            self.rids.append(await self.engine.add_request("hi", None))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    def http(self, tid, method="POST", path="/v1/messages", body=b""):
        """One request through the middleware: (status, body)."""
        sent = []
        msgs = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return msgs.pop(0) if msgs else {"type": "http.disconnect"}

        async def send(m):
            sent.append(m)
        headers = [(b"content-type", b"application/json")]
        if tid is not None:
            headers.append((self.guard.TICKET_HEADER.encode(), tid.encode()))
        asyncio.run(self.mw({"type": "http", "method": method, "path": path, "headers": headers}, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        return start["status"], b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")

    def retire(self, *ids):
        code, raw = self.http(None, path=self.guard.RETIRE_PATH, body=json.dumps({"tickets": list(ids)}).encode())
        self.assertEqual(code, 200)
        data = json.loads(raw)
        self.assertEqual(data["boot"], self.ledger.boot)
        return data["retired"]


class GuardLedgerTests(LedgerBase):
    def test_normal_completion_retires_after_the_step_that_finished_it(self):
        t = self.tid()
        self.assertEqual(self.http(t), (200, b"ok"))
        self.assertEqual(self.calls, [t])                               # the handler ran with its ticket
        rid = self.rids[0]
        self.assertEqual(self.retire(t), [])                            # queued: waiting
        self.sched.step()
        self.assertIn(rid, self.sched.running)
        self.assertEqual(self.retire(t), [])                            # running: rows in the batch
        self.sched.done.add(rid)
        self.sched.step()                                               # finished and removed in this step
        self.assertEqual(self.retire(t), [t])
        self.assertEqual(self.retire(t), [t])                           # asked again: still retired

    def test_round3_repro_a_disconnect_or_gate_timeout_waits_for_the_deferred_abort(self):
        # the gate gave up (timeout or client gone): the server's stream_outputs finally queues the abort and drops
        # the requests entry, but the rows stay in the batch until the executor's next step processes the abort
        t = self.tid()
        self.http(t)
        rid = self.rids[0]
        self.sched.step()
        self.sched.abort_request(rid)                                   # deferred (scheduler.py 2227-2242)
        self.sched.remove_finished_request(rid)                         # engine_core._cleanup_request
        self.assertNotIn(rid, self.sched.requests)
        for _ in range(3):
            self.assertEqual(self.retire(t), [])                        # still in running / the uid map
        self.sched.step()                                               # _process_pending_aborts: the rows go
        self.assertEqual(self.retire(t), [t])

    def test_a_waiting_request_whose_entry_was_dropped_is_still_held(self):
        t = self.tid()
        self.http(t)
        rid = self.rids[0]
        self.sched.abort_request(rid)
        self.sched.remove_finished_request(rid)                         # waiting, but no requests entry
        self.assertEqual(self.retire(t), [])
        self.sched.step()
        self.assertEqual(self.retire(t), [t])

    def test_a_backend_abort_of_everything_retires_at_its_safe_point(self):
        tickets = [self.tid() for _ in range(3)]
        for t in tickets:
            self.http(t)
        self.sched.step()
        self.assertEqual(self.retire(*tickets), [])
        self.sched.reset()                                              # generation error recovery / reset
        self.assertEqual(sorted(self.retire(*tickets)), sorted(tickets))

    def test_a_hung_step_retires_nothing(self):
        t = self.tid()
        self.http(t)
        self.sched.step()
        self.sched.done.add(self.rids[0])
        started, stop = threading.Event(), threading.Event()

        def hang():                                                     # the request left the batch...
            started.set()
            stop.wait(5)                                                # ...but the step never returns
        self.sched.after_step = hang
        th = threading.Thread(target=self.sched.step)
        th.start()
        self.assertTrue(started.wait(5))
        try:
            for _ in range(3):
                self.assertEqual(self.retire(t), [])                     # no safe point reached: reserved
        finally:
            stop.set()
            th.join(5)
        self.assertEqual(self.retire(t), [t])                           # the step's end was the safe point

    def test_a_queue_changing_while_read_retires_nothing(self):
        t = self.tid()
        self.http(t)
        self.sched.step()
        self.sched.done.add(self.rids[0])

        class Exploding:
            def __iter__(self):
                raise RuntimeError("deque mutated during iteration")

        def explode():
            self.sched.waiting = Exploding()                            # read while another thread appends
        self.sched.after_step = explode
        self.sched.step()                                               # its safe point could not read the queue
        self.assertEqual(self.retire(t), [])
        self.sched.waiting, self.sched.after_step = collections.deque(), None
        self.assertEqual(self.retire(t), [t])

    def test_a_restart_retires_every_id_it_never_saw_and_refuses_them_after(self):
        old = [self.tid() for _ in range(3)]
        restarted = self.guard.TicketLedger(clock=lambda: self.now[0])    # a new vllm-mlx process
        self.assertEqual(restarted.retire(old), old)
        for t in old:
            self.assertIsNotNone(restarted.open(t))                       # tombstoned: never served late
        # a malformed or stale id could never be admitted here: retired, and not tracked
        stale = self.tid(age=self.guard.TICKET_MAX_AGE_S + 5)
        self.assertEqual(restarted.retire(["junk", 7, stale]), ["junk", stale])
        self.assertEqual(len(restarted._tickets), 3)
        self.assertEqual(self.retire(old[0]), [old[0]])                   # this process never saw it either
        self.assertEqual(self.http(old[0])[0], 409)

    def test_an_open_exchange_is_never_retired(self):
        t = self.tid()
        self.assertIsNone(self.ledger.open(t))                            # its ASGI call is still running
        self.assertEqual(self.retire(t), [])
        self.ledger.ended(t)
        self.assertEqual(self.retire(t), [t])                             # nothing bound to it: closed, retired

    def test_no_bind_after_the_ticket_is_closed(self):
        # an orphaned task of an ended exchange reaching add_request after its ticket was retired is refused
        t = self.tid()
        self.assertIsNone(self.ledger.open(t))
        self.ledger.ended(t)
        self.assertEqual(self.retire(t), [t])
        token = self.current.set(t)
        try:
            with self.assertRaises(HTTPError) as cm:
                asyncio.run(self.engine.add_request("late", None))
        finally:
            self.current.reset(token)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual((list(self.sched.waiting), self.sched.requests), ([], {}))

    def test_a_request_refused_before_the_queue_retires_without_a_step(self):
        self.sched.refuse = True
        t = self.tid()
        with self.assertRaises(ValueError):
            self.http(t)
        self.assertEqual(self.retire(t), [t])

    def test_requests_without_a_fresh_ticket_never_reach_vllm_mlx(self):
        for tid in (None, "junk", self.tid(age=self.guard.TICKET_MAX_AGE_S + 5), self.tid(age=-120)):
            with self.subTest(tid):
                code, body = self.http(tid)
                self.assertEqual(code, 409)
                self.assertIn(self.guard.TICKET_REFUSAL, json.loads(body)["detail"])
        t = self.tid()
        self.http(t)
        self.assertEqual(self.http(t)[0], 409)                           # an id is used once
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.http(None, method="GET", path="/v1/models")[0], 200)   # reads pass without one
        # and the scheduler refuses a request no ticket is bound to, whoever adds it
        with self.assertRaises(HTTPError):
            self.sched.add_request(Req("direct", "hi"))
        with self.assertRaises(HTTPError):                               # the engine outside any ticketed request
            asyncio.run(self.engine.add_request("hi", None))
        self.assertEqual(len(self.sched.waiting), 1)

    def test_the_retire_endpoint_takes_a_bounded_post_only(self):
        self.assertEqual(self.http(None, method="GET", path=self.guard.RETIRE_PATH)[0], 405)
        self.assertEqual(self.http(None, path=self.guard.RETIRE_PATH, body=b"[1]")[0], 400)
        big = json.dumps({"tickets": ["x" * 1000] * 2000}).encode()
        self.assertEqual(self.http(None, path=self.guard.RETIRE_PATH, body=big)[0], 413)

    def test_install_refuses_twice_and_a_missing_seam(self):
        with self.assertRaises(self.guard.GuardError):
            self.guard.install_tickets(self.ledger, self.Sched, self.Engine, App(), HTTPError)
        with self.assertRaises(self.guard.GuardError):
            self.guard.install_tickets(self.ledger, type("S", (), {"add_request": lambda s, r: None}),
                                       type("E", (Engine,), {}), App(), HTTPError)


class PausingQueue(collections.deque):
    """A waiting queue whose next read (iteration) pauses until released: a safe point caught mid-snapshot."""

    def __init__(self, *a):
        super().__init__(*a)
        self.pause_next, self.reading, self.resume, self.during = False, threading.Event(), threading.Event(), None

    def __iter__(self):
        if self.pause_next:
            self.pause_next = False
            self.reading.set()
            if self.during is not None:
                self.during()                                           # same thread, mid-read
            else:
                self.resume.wait(5)
        return super().__iter__()


class LedgerAtomicityTests(LedgerBase):
    """#126 review round 4 (P1a): admission (enqueue + entered) and a safe point's snapshot + "gone" transition are
    atomic with each other (TicketLedger.admit_lock), and a request numbered at or after a snapshot's start is never
    gone by that snapshot (the entry sequence number)."""

    def setUp(self):
        super().setUp()
        self.sched.waiting = PausingQueue()
        self.sched.step()                                               # watched from its first step

    def state(self, tid):
        with self.ledger._lock:
            return [self.ledger._rids[r][1] for r in self.ledger._tickets[tid]["rids"]]

    def admit_in_thread(self, t):
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("r", self.http(t)))
        th.start()
        return th, out

    def query_paused(self, query):
        """Start `query` in a thread and pause it inside its scheduler read (after it noted the entry number)."""
        self.sched.waiting.pause_next = True
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("r", query()))
        th.start()
        self.assertTrue(self.sched.waiting.reading.wait(5))
        return th, out

    def test_round4_repro_a_retire_query_racing_admission_never_retires_a_running_request(self):
        t = self.tid()
        q, qout = self.query_paused(lambda: self.ledger.retire([t]))    # paused after an empty snapshot began
        a, aout = self.admit_in_thread(t)                               # a bound request is being enqueued
        self.assertTrue(wait_for(lambda: t in self.ledger._tickets and self.ledger._tickets[t]["rids"]))
        time.sleep(0.1)
        self.assertEqual(self.state(t), ["pending"])                    # held at the admission lock, not queued
        self.sched.waiting.resume.set()
        q.join(5)
        a.join(5)
        self.assertEqual(aout["r"], (200, b"ok"))
        self.assertEqual(self.state(t), ["entered"])                    # never marked gone
        self.sched.step()                                               # running: rows in the batch
        self.assertEqual(self.retire(t), [])                            # the exchange ended; still not retired
        self.sched.done.add(self.rids[0])
        self.sched.step()
        self.assertEqual(self.retire(t), [t])

    def test_round4_a_step_safe_point_racing_admission_never_marks_it_gone(self):
        t = self.tid()
        s, _ = self.query_paused(self.sched.step)                       # the executor's post-step safe point
        a, aout = self.admit_in_thread(t)                               # an inline add on the event loop
        self.assertTrue(wait_for(lambda: t in self.ledger._tickets and self.ledger._tickets[t]["rids"]))
        time.sleep(0.1)
        self.assertEqual(self.state(t), ["pending"])
        self.sched.waiting.resume.set()
        s.join(5)
        a.join(5)
        self.assertEqual(self.state(t), ["entered"])
        self.assertEqual(self.retire(t), [])                            # waiting: rows to come
        self.sched.step()
        self.assertEqual(self.retire(t), [])

    def test_round4_a_fence_query_racing_admission_never_reports_clear(self):
        t = self.tid()
        q, qout = self.query_paused(lambda: self.ledger.fence(1, []))
        a, _ = self.admit_in_thread(t)
        self.assertTrue(wait_for(lambda: t in self.ledger._tickets))
        self.sched.waiting.resume.set()
        q.join(5)
        a.join(5)
        self.assertFalse(qout["r"]["clear"])                            # the open exchange or its pending request
        self.assertFalse(self.ledger.fence(1, [])["clear"])             # queued now, from a ticket not known
        self.assertTrue(self.ledger.fence(1, [t])["clear"])             # known to the gate asking

    def test_round4_an_entry_numbered_after_a_snapshot_began_is_never_gone_by_it(self):
        # the number check alone (should any add ever run outside the lock): an add completing mid-read
        t = self.tid()
        self.assertIsNone(self.ledger.open(t))
        self.assertIsNone(self.ledger.bind(t, "late"))

        def add_mid_read():                                             # RLock: the same thread may re-enter
            self.assertTrue(self.ledger.entering("late"))
            self.ledger.entered("late")
        self.sched.waiting.pause_next, self.sched.waiting.during = True, add_mid_read
        self.assertTrue(self.ledger.safe_point())
        self.assertEqual(self.state(t), ["entered"])                    # not in the snapshot, numbered after it
        self.assertTrue(self.ledger.safe_point())                       # a later one: it is in no queue, gone
        self.assertEqual(self.state(t), ["gone"])

    def test_round4_a_query_with_admission_running_answers_from_the_last_safe_point(self):
        t = self.tid()
        self.http(t)
        self.sched.step()
        self.sched.done.add(self.rids[0])
        self.sched.step()                                               # gone at this step's safe point
        with self.ledger.admit_lock:                                    # an add in progress elsewhere
            th = threading.Thread(target=lambda: self.assertEqual(self.ledger.retire([t]), [t]))
            th.start()
            th.join(5)                                                  # answered without waiting for it
            self.assertFalse(th.is_alive())


class LedgerFenceTests(LedgerBase):
    """#126 review round 4 (P1b), the guard's side: FENCE_PATH raises the floor and says whether a request of a ticket
    the asking gate does not know may still be in the scheduler."""

    def fence(self, floor_ms, known=()):
        code, raw = self.http(None, path=self.guard.FENCE_PATH,
                              body=json.dumps({"floor_ms": floor_ms, "known": list(known)}).encode())
        self.assertEqual(code, 200)
        data = json.loads(raw)
        self.assertEqual(data["boot"], self.ledger.boot)
        return data

    def test_round4_repro_a_previous_gates_running_row_keeps_the_fence_up(self):
        old = self.tid(age=1.0)
        self.http(old)                                                  # the old gate's 64k turn
        self.sched.step()                                               # running; that gate is gone
        start = int(self.now[0] * 1000)
        got = self.fence(start)
        self.assertEqual((got["clear"], got["unknown"]), (False, 1))
        self.assertEqual(self.http(self.tid(age=2.0))[0], 409)          # minted before the floor: refused
        self.assertEqual(self.fence(start)["clear"], False)
        self.sched.done.add(self.rids[0])
        self.sched.step()                                               # its rows leave
        self.assertTrue(self.fence(start)["clear"])
        self.assertEqual(self.ledger._tickets[old]["state"], "closed")   # below the floor: nobody will ask
        self.assertEqual(self.http(local_gate.new_ticket_id(self.now[0], start))[0], 200)

    def test_an_open_exchange_or_a_queued_request_of_an_unknown_ticket_is_not_clear(self):
        t = self.tid()
        self.assertIsNone(self.ledger.open(t))
        self.assertFalse(self.fence(1)["clear"])                        # open: it may still bind
        self.assertTrue(self.fence(1, [t])["clear"])
        self.ledger.ended(t)
        self.assertTrue(self.fence(1)["clear"])                         # nothing bound, exchange over
        self.assertEqual(self.fence(5)["floor_ms"], 5)
        self.assertEqual(self.fence(3)["floor_ms"], 5)                  # the floor never goes down

    def test_a_hung_step_keeps_unknown_rows_unclear(self):
        old = self.tid()
        self.http(old)
        self.sched.step()
        self.sched.done.add(self.rids[0])
        with self.ledger.step_lock:                                     # a step that never returns
            th = threading.Thread(target=lambda: self.assertFalse(self.ledger.fence(1, [])["clear"]))
            th.start()
            th.join(5)
            self.assertFalse(th.is_alive())

    def test_the_fence_endpoint_takes_a_bounded_post_only(self):
        path = self.guard.FENCE_PATH
        self.assertEqual(self.http(None, method="GET", path=path)[0], 405)
        for body in (b"[1]", b'{"floor_ms": "1", "known": []}', b'{"floor_ms": 1}', b'{"floor_ms": true, "known": []}'):
            self.assertEqual(self.http(None, path=path, body=body)[0], 400, body)
        big = json.dumps({"floor_ms": 1, "known": ["x" * 1000] * 2000}).encode()
        self.assertEqual(self.http(None, path=path, body=big)[0], 413)


class LedgerBoundTests(LedgerBase):
    """#126 review round 4 (P2b): at most TICKET_MAX ids are tracked on every insert path, and an evicted tombstone
    cannot be replayed (the floor rises past every evicted id)."""

    def test_round4_repro_tombstones_stay_within_the_bound_and_are_never_replayed(self):
        cap = self.guard.TICKET_MAX
        ids = [self.tid(age=300 - i * 0.03) for i in range(cap + 100)]  # distinct, fresh, increasing
        self.assertEqual(self.ledger.retire(ids[:cap]), ids[:cap])      # 8192 tombstones
        for t in ids[cap:]:                                             # 100 more queries of unknown ids
            self.assertEqual(self.ledger.retire([t]), [t])
            self.assertLessEqual(len(self.ledger._tickets), cap)
        self.assertLessEqual(len(self.ledger._tickets), cap)
        for t in ids[:150] + ids[-3:]:                                  # evicted or still tracked: refused
            self.assertIsNotNone(self.ledger.open(t))
        self.assertEqual(self.http(ids[0])[0], 409)
        self.assertEqual(self.ledger.retire([ids[0]]), [ids[0]])        # below the floor: retired, not tracked
        self.assertLessEqual(len(self.ledger._tickets), cap)

    def test_open_turns_are_never_evicted_and_an_untrackable_id_is_not_retired(self):
        with patch.object(self.guard, "TICKET_MAX", 8):
            live = [self.tid(age=10 - i) for i in range(8)]
            for t in live:
                self.assertIsNone(self.ledger.open(t))                  # 8 exchanges open
            extra = self.tid()
            self.assertEqual(self.ledger.retire([extra]), [])           # cannot be tombstoned: not retired
            self.assertEqual(self.ledger.open(extra), "too many tickets tracked")
            self.assertEqual(len(self.ledger._tickets), 8)
            self.ledger.ended(live[0])
            self.assertEqual(self.ledger.retire([live[0]]), [live[0]])  # closed: evictable now
            self.assertEqual(self.ledger.retire([extra]), [extra])
            self.assertEqual(len(self.ledger._tickets), 8)
            self.assertIsNotNone(self.ledger.open(live[0]))             # evicted, below the floor: refused
            self.assertIsNotNone(self.ledger.open(extra))               # tombstoned: refused


# ------------------------------------------------------------------------------------------------ import order (P2b)


class ImportOrderTests(VisionGuardBase):
    """#126 review round 3 (P2b): `serve` and `--check` verify every pinned file on disk before anything of vllm-mlx,
    mlx-lm or mlx is imported, so a planted line in a pinned file never runs."""

    def setUp(self):
        super().setUp()
        lm_pins = write_lm(self.dir, self.guard.MLX_LM_PINNED_SHA256)
        # the released import chain: vllm_mlx.scheduler imports mlx_lm, whose __init__ imports mlx.nn (via generate)
        init = Path(self.dir, "mlx_lm", "__init__.py")
        # round 4: and convert, utils and tokenizer_utils, which __init__ imports too (each pinned)
        init.write_text('import mlx.nn\nfrom . import convert, utils, tokenizer_utils\n__version__ = "0.31.3"\n')
        lm_pins["__init__.py"] = self._sha("mlx_lm", "__init__.py")
        sched = Path(self.dir, "vllm_mlx", "scheduler.py")
        sched.write_text("import mlx_lm\nimport mlx.core\n" + sched.read_text())
        self.repin()
        for name, value in (("MLX_LM_PINNED_SHA256", lm_pins), ("_text_preflight", self.real["_text_preflight"]),
                            ("_text_modules", self.real["_text_modules"])):
            p = patch.object(self.guard, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._unimport_lm)
        self.marker = Path(self.tmp, "ran")

    def _unimport_lm(self):
        import sys
        for name in list(sys.modules):
            if name.split(".")[0] == "mlx_lm":
                del sys.modules[name]

    def _clean(self):
        self._fresh()
        self._unimport_lm()

    def test_round3_repro_a_marker_in_a_pinned_file_never_runs(self):
        self._clean()
        rc, err = self.run_main(SERVE_TEXT)                              # the clean chain serves
        self.assertEqual(rc, 0, err)
        import sys
        self.assertIn("mlx.nn", sys.modules)                             # imported, after the checks
        for pkg, rel in (("mlx", "nn/__init__.py"), ("mlx", "nn/layers/activations.py"), ("mlx", "utils.py"),
                         ("mlx_lm", "__init__.py"), ("mlx_lm", "generate.py"), ("mlx_lm", "convert.py"),
                         ("mlx_lm", "utils.py"), ("mlx_lm", "tokenizer_utils.py"),
                         ("mlx_lm", "tool_parsers/qwen3_coder.py"), ("vllm_mlx", "__init__.py"),
                         ("vllm_mlx", "scheduler.py")):
            for argv in (SERVE_TEXT, ["--check", "/models/text"]):
                with self.subTest(f"{pkg}/{rel} {argv[0]}"):
                    self._clean()
                    path = Path(self.dir, pkg, *rel.split("/"))
                    text = path.read_text()
                    path.write_text(text + f"open({str(self.marker)!r}, 'w').write('ran')\n")
                    try:
                        rc, err = self.run_main(argv)
                    finally:
                        path.write_text(text)
                    self.assertEqual(rc, 2, err)
                    self.assertIn(f"{pkg}/{rel} differs", err)
                    self.assertFalse(self.marker.exists(), f"{pkg}/{rel} ran before its pin was checked")
                    self.assertNotIn("mlx.nn", sys.modules)
                    self.assertNotIn("mlx_lm", sys.modules)

    def shadow(self, rel):
        """A folder earlier on sys.path holding `rel` (e.g. mlx/__init__.py) that writes the marker when run."""
        import importlib
        import sys
        import tempfile
        root = tempfile.mkdtemp(dir=self.tmp)
        path = Path(root, *rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"open({str(self.marker)!r}, 'w').write('ran')\n")
        sys.path.insert(0, root)
        importlib.invalidate_caches()
        self.addCleanup(lambda: root in sys.path and sys.path.remove(root))
        self.addCleanup(importlib.invalidate_caches)
        return root

    def test_round4_repro_a_shadow_mlx_parent_never_runs(self):
        import sys
        for rel, why in (("mlx/__init__.py", "mlx resolves to"),          # a regular package shadowing the namespace
                         ("mlx/nn/__init__.py", "mlx.nn resolves to"),    # an earlier namespace portion's mlx.nn
                         ("mlx/core.py", "mlx.core resolves to"),         # ... or its mlx.core
                         ("mlx.py", "mlx resolves to")):                  # a plain module named mlx
            for argv in (SERVE_TEXT, ["--check", "/models/text"]):
                with self.subTest(f"{rel} {argv[0]}"):
                    self._clean()
                    root = self.shadow(rel)
                    try:
                        rc, err = self.run_main(argv)
                    finally:
                        sys.path.remove(root)
                    self.assertEqual(rc, 2, err)
                    self.assertIn(why, err)
                    self.assertFalse(self.marker.exists(), f"a shadow {rel} ran before mlx was verified")
                    self.assertNotIn("mlx", sys.modules)
        self._clean()
        self.assertEqual(self.run_main(SERVE_TEXT)[0], 0)                 # the shadow gone: it serves

    def test_round4_an_unreleased_module_in_mlx_lms_by_name_folders_is_refused(self):
        for folder in ("tool_parsers", "chat_templates"):
            with self.subTest(folder):
                self._clean()
                extra = Path(self.dir, "mlx_lm", folder, "planted.py")
                extra.write_text(f"open({str(self.marker)!r}, 'w').write('ran')\n")
                try:
                    rc, err = self.run_main(SERVE_TEXT)
                finally:
                    extra.unlink()
                self.assertEqual(rc, 2, err)
                self.assertIn(f"mlx_lm/{folder}/planted.py is not in the 0.31.3 release", err)
                self.assertFalse(self.marker.exists())

    def test_the_released_mlx_nn_init_is_pinned(self):
        # mlx.nn is a regular package in every 0.32.4 wheel (its __init__ imports the layers): pinned by hash, while
        # mlx itself must stay a namespace package (no mlx/__init__.py, round 2)
        real = load_guard()
        self.assertEqual(real.MLX_PY_SHA256["nn/__init__.py"],
                         "6d578784bfe696a3ba6eca2a559c87e9fd1ec5eaa5e6d4e199e6e959ebe0a492")
        self.assertEqual(real.PINNED_SHA256["__init__.py"],
                         "f1a099967fa0c1a7673dadb813751f0adc31abcd94bbf16712e2db3d96687724")


if __name__ == "__main__":
    unittest.main()
