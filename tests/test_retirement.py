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
        if self.path == local_gate.RETIRE_PATH:
            st.polls.append(list(body["tickets"]))
            if st.retire_status != 200:
                return self._json(st.retire_status, {"detail": "busy"})
            with st.lock:                         # an id it does not hold (or never saw) is retired
                return self._json(200, {"retired": [t for t in body["tickets"] if t not in st.live], "boot": "b"})
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
        conn = http.client.HTTPConnection("127.0.0.1", gate.server_port, timeout=10)
        conn.request("POST", "/v1/messages", body=json.dumps(msg()).encode(),
                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}"})
        self.assertEqual(conn.getresponse().status, 502)
        conn.close()
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


class RetirerUnitTests(unittest.TestCase):
    def test_only_the_ids_asked_and_confirmed_are_released(self):
        adm = local_governor.Admission(FixedGovernor())
        answers = collections.deque()
        r = local_gate.Retirer("127.0.0.1", 9, call=lambda ids: answers.popleft(), start=False)
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


class GuardLedgerTests(unittest.TestCase):
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


# ------------------------------------------------------------------------------------------------ import order (P2b)


class ImportOrderTests(VisionGuardBase):
    """#126 review round 3 (P2b): `serve` and `--check` verify every pinned file on disk before anything of vllm-mlx,
    mlx-lm or mlx is imported, so a planted line in a pinned file never runs."""

    def setUp(self):
        super().setUp()
        lm_pins = write_lm(self.dir, self.guard.MLX_LM_PINNED_SHA256)
        # the released import chain: vllm_mlx.scheduler imports mlx_lm, whose __init__ imports mlx.nn (via generate)
        init = Path(self.dir, "mlx_lm", "__init__.py")
        init.write_text('import mlx.nn\n__version__ = "0.31.3"\n')
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
                         ("mlx_lm", "__init__.py"), ("mlx_lm", "generate.py"), ("vllm_mlx", "__init__.py"),
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
