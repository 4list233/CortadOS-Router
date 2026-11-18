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
import itertools
import json
import os
import secrets
import shutil
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
        self.tickets: list = []                  # the ticket header of each POST /v1/messages admitted, in order
        self.sent: list = []                     # ... of each one sent, refused (409) or not
        self.polls: list = []                    # the ids of each retirement poll
        self.mode = "complete"                   # complete | hold | error | stream
        self.retire_status = 200                 # 503: a guard that cannot answer (hung)
        self.hold = threading.Event()
        self.arrived = threading.Semaphore(0)
        self.fences: list = []                   # the body of each fence query
        self.fence_status = 200                  # 503: a guard that cannot answer the fence
        self.epoch = None                        # the epoch the last fence query made current (None: a fresh guard)
        self.enforce = False                     # True: refuse a ticket of another epoch, as the real guard does (409)
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
                if st.epoch is not None and body["epoch"] < st.epoch:
                    return self._json(200, {"clear": False, "refused": True, "unknown": 0, "boot": st.boot})
                st.epoch = body["epoch"]
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
            st.sent.append(ticket)
            if st.enforce and (st.epoch is None or int(ticket.split(".")[0], 16) != st.epoch):
                return self._json(409, {"detail": f"{local_gate.TICKET_REFUSAL} (no gate epoch fenced yet)"})
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
        self.epoch_file = Path(self.tmpdir(), "local-gate-epoch")   # the node's epoch file (next_epoch)
        self.gate = self.make_gate(self.upstream.server_port)
        self.url = f"http://127.0.0.1:{self.gate.server_port}"

    def tmpdir(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def make_gate(self, port):
        gov = FixedGovernor(**self.GOVERNOR) if self.GOVERNOR is not None else None
        gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN, upstream=f"http://127.0.0.1:{port}",
                                     read_timeout=self.READ_TIMEOUT, backend={**MLX, "governor": None},
                                     admission=local_governor.Admission(gov), epoch_file=self.epoch_file)
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
        parts = [guard.ticket_parts(t) for t in self.st.tickets]       # the guard's own format check
        self.assertEqual({p[0] for p in parts}, {self.gate.retirer.epoch})
        self.assertEqual(sorted(p[1] for p in parts), [p[1] for p in parts])   # strictly increasing counters
        self.assertEqual(len({p[1] for p in parts}), 3)
        self.assertEqual((local_gate.TICKET_HEADER, local_gate.RETIRE_PATH, local_gate.FENCE_PATH,
                          local_gate.TICKET_REFUSAL, local_gate.EPOCH_MAX),
                         (guard.TICKET_HEADER, guard.RETIRE_PATH, guard.FENCE_PATH, guard.TICKET_REFUSAL,
                          guard.EPOCH_MAX))


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
        self.assertEqual(self.st.fences[-1], {"epoch": gate.retirer.epoch, "known": []})
        self.st.release("old-64k-turn")                                 # its rows leave the scheduler
        self.assertTrue(wait_for(lambda: gate.retirer.fenced() is None))
        self.assertEqual(self.call(msg())[0], 200)
        self.assertIsNone(self.report()["fence"])

    def test_the_fence_lifts_when_the_backend_is_empty_and_tickets_carry_the_fenced_epoch(self):
        self.assertEqual(self.call(msg())[0], 200)
        self.assertEqual(self.st.fences[0], {"epoch": self.gate.retirer.epoch, "known": []})
        self.assertIsNone(self.report()["fence"])
        self.assertEqual(int(self.st.tickets[0].split(".")[0], 16), self.st.epoch)

    def test_round6_a_gate_takes_the_nodes_next_epoch_and_its_counter_ignores_the_clock(self):
        first = self.gate.retirer.epoch
        self.assertEqual(self.epoch_file.read_text(), f"{first}\n")     # taken from the node's file, written back
        second = self.make_gate(self.upstream.server_port).retirer.epoch   # a gate restart: the next one
        self.assertEqual(second, first + 1)
        self.assertEqual(self.epoch_file.read_text(), f"{second}\n")
        r = local_gate.Retirer("127.0.0.1", 9, call=lambda p, b: {}, start=False, epoch=second)
        clock = itertools.cycle([1.8e9, 1.0, 4e9, 0.0, 1.8e9 - 86400, 7e9])       # the wall clock jumping both ways
        with patch.object(local_gate.time, "time", lambda: next(clock)):
            ids = [r.mint() for _ in range(6)]
        guard = load_guard()
        self.assertEqual([guard.ticket_parts(t) for t in ids], [(second, n) for n in range(6)])
        self.assertTrue(all(len(t) == guard.TICKET_LEN == 66 for t in ids))
        self.assertEqual(len({t.split(".")[2] for t in ids}), 6)        # the nonce: unguessable, not an order
        for bad in (0, -1, 1 << 64, "1", True, 1.0):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                local_gate.Retirer("127.0.0.1", 9, call=lambda p, b: {}, start=False, epoch=bad)

    def test_round6_a_refused_epoch_keeps_the_gate_fenced(self):
        answers = collections.deque([{"clear": False, "refused": True, "unknown": 0, "boot": "b"}])
        r = local_gate.Retirer("127.0.0.1", 9, call=lambda path, body: answers.popleft(), start=False, epoch=1)
        self.assertIn("refuses this gate's epoch", r.fence_once())
        self.assertIn("refuses this gate's epoch", r.fenced())

    def test_round6_the_first_turn_after_a_backend_restart_costs_at_most_one_retry(self):
        self.st.enforce = True                                          # the real guard's rule (a): fenced first
        self.assertEqual(self.call(msg())[0], 200)                      # the startup fence, then served
        self.assertEqual(len(self.st.sent), 1)
        self.st.release()
        self.assertTrue(wait_for(lambda: self.adm.inflight == 0))
        with self.st.lock:                                              # vllm-mlx restarts while the gate is idle
            self.st.epoch, self.st.boot, self.st.live = None, "b2", set()
        fences, sent = len(self.st.fences), len(self.st.sent)
        code, _ = self.call(msg())                                      # one client call...
        self.assertEqual(code, 200)
        self.assertEqual(len(self.st.sent) - sent, 2)                   # ...two upstream sends: refused once, then served
        self.assertEqual(len(self.st.tickets), 2)
        self.assertGreater(len(self.st.fences), fences)                 # re-fenced in between
        refused = self.st.sent[sent]
        self.assertIn(refused, self.st.fences[-1]["known"])             # the refused id stays accounted for
        self.assertEqual(self.gate.retirer.boot, "b2")
        self.st.release()
        self.assertTrue(wait_for(lambda: (self.adm.inflight, self.adm.reserved, self.gate.retirer.pending())
                                 == (0, 0, [])))
        sent = len(self.st.sent)
        self.assertEqual(self.call(msg())[0], 200)                      # steady again: no retry
        self.assertEqual(len(self.st.sent) - sent, 1)

    def test_round6_a_refused_retry_answers_busy_and_holds_nothing(self):
        self.st.enforce = True
        self.assertEqual(self.call(msg())[0], 200)
        self.st.release()
        self.assertTrue(wait_for(lambda: self.adm.inflight == 0))
        with self.st.lock:                                              # a newer gate fenced the guard
            self.st.epoch = self.gate.retirer.epoch + 1
        sent = len(self.st.sent)
        code, raw = self.call(msg())
        self.assertEqual(code, 529)                                     # busy, not the guard's 409
        self.assertIn("refuses this gate's epoch", json.loads(raw)[local_gate.GATE_KEY]["fence"])
        self.assertEqual(len(self.st.sent) - sent, 1)                   # never sent a second time
        self.assertTrue(wait_for(lambda: (self.adm.inflight, self.adm.reserved) == (0, 0)))

    def test_round6_a_restarted_gate_supersedes_its_predecessor_at_the_guard(self):
        self.st.enforce = True
        old = self.gate
        self.assertEqual(self.call(msg())[0], 200)                      # the old gate fenced and served
        self.st.release()
        self.assertTrue(wait_for(lambda: old.admission.inflight == 0))
        new = self.make_gate(self.upstream.server_port)                 # the restarted gate: a higher epoch
        self.gate = new
        self.assertEqual(self.call(msg())[0], 200)
        self.assertEqual(self.st.epoch, new.retirer.epoch)
        self.gate = old                                                 # the old gate's turn arrives late
        code, raw = self.call(msg())
        self.assertEqual(code, 529)
        self.assertIn("refuses this gate's epoch", json.loads(raw)[local_gate.GATE_KEY]["fence"])
        self.assertEqual(self.st.epoch, new.retirer.epoch)              # its fence displaced nothing
        self.gate = new
        self.assertEqual(self.call(msg())[0], 200)

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


class EpochFileTests(unittest.TestCase):
    """#126 review round 6: each gate start takes the node's next epoch from a small state file, under an exclusive
    lock, written back durably before it is used; a corrupt file refuses to start and is never reset."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = Path(self.dir, "cfg", "local-gate-epoch")

    def test_round6_a_missing_file_starts_at_1_and_each_start_takes_the_next(self):
        self.assertEqual([local_gate.next_epoch(self.path) for _ in range(3)], [1, 2, 3])
        self.assertEqual(self.path.read_text(), "3\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(str(self.path) + ".lock").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(os.listdir(self.path.parent)), ["local-gate-epoch", "local-gate-epoch.lock"])
        self.path.write_text("41")                                      # no newline: still one integer
        self.assertEqual(local_gate.next_epoch(self.path), 42)

    def test_round6_the_epoch_is_written_and_synced_before_it_is_used(self):
        calls = []
        real_fsync, real_replace = os.fsync, os.replace
        with patch.object(local_gate.os, "fsync", lambda fd: calls.append("fsync") or real_fsync(fd)), \
                patch.object(local_gate.os, "replace", lambda a, b: calls.append("rename") or real_replace(a, b)):
            self.assertEqual(local_gate.next_epoch(self.path), 1)
        self.assertEqual(calls, ["fsync", "rename", "fsync"])           # the temp file, the rename, the directory

    def test_round6_concurrent_gate_starts_get_distinct_increasing_epochs(self):
        import subprocess
        import sys
        got, lock = [], threading.Lock()

        def start():
            e = local_gate.next_epoch(self.path)
            with lock:
                got.append(e)
        threads = [threading.Thread(target=start) for _ in range(24)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)
        # and separate processes, as two gate LaunchAgents would be
        code = ("import sys; from cortados_router import local_gate; "
                "print(local_gate.next_epoch(sys.argv[1]))")
        root = str(Path(__file__).resolve().parent.parent)
        procs = [subprocess.Popen([sys.executable, "-B", "-c", code, str(self.path)], cwd=root,
                                  stdout=subprocess.PIPE, text=True) for _ in range(6)]
        for p in procs:
            out, _ = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0)
            got.append(int(out))
        self.assertEqual(sorted(got), list(range(1, 31)))               # distinct, none lost, none reused
        self.assertEqual(self.path.read_text(), "30\n")

    def test_round6_a_corrupt_epoch_file_refuses_to_start_and_is_never_reset(self):
        self.path.parent.mkdir(mode=0o700)
        for raw in (b"", b"\n", b"abc\n", b"-1\n", b"0\n", b"01\n", b"7\n\n", b" 7\n", b"7.0\n", b"\x00",
                    b"18446744073709551616\n", b"9" * 64):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaisesRegex(local_gate.GateConfigError, "corrupt"):
                    local_gate.next_epoch(self.path)
                self.assertEqual(self.path.read_bytes(), raw)           # left for the owner to inspect
        self.path.write_bytes(b"18446744073709551615\n")                # the last epoch was used
        with self.assertRaisesRegex(local_gate.GateConfigError, "used up"):
            local_gate.next_epoch(self.path)
        self.path.unlink()
        target = Path(self.dir, "elsewhere")
        target.write_text("5\n")
        os.symlink(target, self.path)                                   # a link: refused, never followed
        with self.assertRaisesRegex(local_gate.GateConfigError, "not a regular file"):
            local_gate.next_epoch(self.path)
        self.assertEqual(target.read_text(), "5\n")
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaisesRegex(local_gate.GateConfigError, "not a regular file"):
            local_gate.next_epoch(self.path)

    def test_round6_a_gate_with_a_corrupt_epoch_file_does_not_start(self):
        self.path.parent.mkdir(mode=0o700)
        self.path.write_text("garbage\n")
        with patch.object(local_gate, "pro_local_allowed", return_value=(True, "test")):
            with self.assertRaisesRegex(local_gate.GateConfigError, "corrupt"):
                local_gate.GateServer(("127.0.0.1", 0), token=TOKEN, upstream="http://127.0.0.1:9",
                                      backend={**MLX, "governor": None}, epoch_file=self.path)
        self.assertEqual(self.path.read_text(), "garbage\n")
        # an Ollama node (no retirement) never reads it
        gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN, upstream="http://127.0.0.1:9",
                                     epoch_file=self.path)
        gate.server_close()
        self.assertIsNone(gate.retirer)

    def test_round6_the_tests_never_touch_the_nodes_epoch_file(self):
        self.assertNotEqual(os.path.expanduser(local_gate.default_epoch_file()),
                            os.path.expanduser(local_gate.DEFAULT_EPOCH_FILE))


class RetirerUnitTests(unittest.TestCase):
    def test_only_the_ids_asked_and_confirmed_are_released(self):
        adm = local_governor.Admission(FixedGovernor())
        answers = collections.deque()
        r = local_gate.Retirer("127.0.0.1", 9, call=lambda path, body: answers.popleft(), start=False, epoch=1)
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
    FENCED = True                                                       # False: a fresh guard no gate has fenced

    def setUp(self):
        self.guard = load_guard()
        self.epoch = 5                                                  # the gate's epoch
        self.seq = itertools.count()
        self.ledger = self.guard.TicketLedger()
        if self.FENCED:
            self.assertTrue(self.ledger.fence(self.epoch, [])["clear"])  # what a starting gate does first
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

    def tid(self, epoch=None):
        return local_gate.new_ticket_id(epoch or self.epoch, next(self.seq))

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
        restarted = self.guard.TicketLedger()                             # a new vllm-mlx process
        self.assertEqual(restarted.retire(old), [])                       # no epoch fenced yet: not retired
        self.assertTrue(restarted.fence(self.epoch, [])["clear"])         # the gate fences on the new boot id
        self.assertEqual(restarted.retire(old), old)
        for t in old:
            self.assertIsNotNone(restarted.open(t))                       # tombstoned: never served late
        self.assertIsNone(restarted.open(self.tid()))                     # the gate's next turn is served
        # a malformed id or one of an older epoch could never be admitted here: retired, and not tracked
        other = self.tid(epoch=self.epoch - 1)
        self.assertEqual(restarted.retire(["junk", 7, other]), ["junk", other])
        self.assertEqual(len(restarted._tickets), 4)
        self.assertEqual(self.retire(old[0]), [old[0]])                   # this process never saw it either
        self.assertEqual(self.http(old[0])[0], 409)

    def test_round6_a_fresh_process_admits_no_ticket_until_a_gate_fences_it(self):
        fresh = self.guard.TicketLedger()                                 # restarted while the gate ran on
        t = self.tid()
        self.assertIn("no gate epoch fenced", fresh.open(t))              # no first-ticket adoption
        self.assertIsNone(fresh.epoch)
        self.assertEqual(fresh.retire([t]), [])                           # nor acknowledged
        self.assertTrue(fresh.fence(self.epoch, [])["clear"])
        self.assertIsNone(fresh.open(self.tid()))
        self.assertIsNotNone(fresh.open(self.tid(epoch=self.epoch + 1)))   # an epoch not fenced: refused

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
        good = self.tid()
        for tid in (None, "junk", good.upper(), good[:-1], good + "0", good.replace(".", "-"),
                    "1800000061000." + "a" * 32, self.tid(epoch=self.epoch - 1), self.tid(epoch=self.epoch + 1),
                    "0" * 16 + good[16:], "f" * 66):
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
        q, qout = self.query_paused(lambda: self.ledger.fence(self.epoch, []))
        a, _ = self.admit_in_thread(t)
        self.assertTrue(wait_for(lambda: t in self.ledger._tickets))
        self.sched.waiting.resume.set()
        q.join(5)
        a.join(5)
        self.assertFalse(qout["r"]["clear"])                            # the open exchange or its pending request
        self.assertFalse(self.ledger.fence(self.epoch, [])["clear"])      # queued now, from a ticket not known
        self.assertTrue(self.ledger.fence(self.epoch, [t])["clear"])      # known to the gate asking

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
    """#126 review round 4 (P1b), the guard's side, with epochs (round 6): FENCE_PATH makes the asking gate's
    epoch the only one admitted and says whether a request of a ticket that gate does not know may still be in
    the scheduler."""

    def fence(self, epoch, known=()):
        code, raw = self.http(None, path=self.guard.FENCE_PATH,
                              body=json.dumps({"epoch": epoch, "known": list(known)}).encode())
        self.assertEqual(code, 200)
        data = json.loads(raw)
        self.assertEqual(data["boot"], self.ledger.boot)
        return data

    def test_round4_repro_a_previous_gates_running_row_keeps_the_fence_up(self):
        old = self.tid()
        self.http(old)                                                  # the old gate's 64k turn
        self.sched.step()                                               # running; that gate is gone
        new = self.epoch + 1                                            # the restarted gate
        got = self.fence(new)
        self.assertEqual((got["clear"], got["unknown"], got["epoch"]), (False, 1, new))
        self.assertEqual(self.http(self.tid())[0], 409)                 # the old epoch: refused
        self.assertEqual(self.fence(new)["clear"], False)
        self.sched.done.add(self.rids[0])
        self.sched.step()                                               # its rows leave
        self.assertTrue(self.fence(new)["clear"])
        self.assertEqual(self.ledger._tickets[old]["state"], "closed")   # its gate is gone: nobody will ask
        self.assertEqual(self.http(local_gate.new_ticket_id(new, 0))[0], 200)

    def test_an_open_exchange_or_a_queued_request_of_an_unknown_ticket_is_not_clear(self):
        t = self.tid()
        self.assertIsNone(self.ledger.open(t))
        self.assertFalse(self.fence(self.epoch)["clear"])                 # open: it may still bind
        self.assertTrue(self.fence(self.epoch, [t])["clear"])
        self.ledger.ended(t)
        self.assertTrue(self.fence(self.epoch)["clear"])                  # nothing bound, exchange over

    def test_a_hung_step_keeps_unknown_rows_unclear(self):
        old = self.tid()
        self.http(old)
        self.sched.step()
        self.sched.done.add(self.rids[0])
        with self.ledger.step_lock:                                     # a step that never returns
            th = threading.Thread(target=lambda: self.assertFalse(self.ledger.fence(self.epoch, [])["clear"]))
            th.start()
            th.join(5)
            self.assertFalse(th.is_alive())

    def test_the_fence_endpoint_takes_a_bounded_post_only(self):
        path = self.guard.FENCE_PATH
        self.assertEqual(self.http(None, method="GET", path=path)[0], 405)
        for body in (b"[1]", b'{"epoch": "1", "known": []}', b'{"epoch": 1}', b'{"floor_ms": 1, "known": []}',
                     b'{"epoch": 0, "known": []}', b'{"epoch": -3, "known": []}', b'{"epoch": true, "known": []}',
                     b'{"epoch": 1.0, "known": []}', b'{"epoch": 18446744073709551616, "known": []}',
                     b'{"generation": "' + b"a" * 32 + b'", "known": []}'):
            self.assertEqual(self.http(None, path=path, body=body)[0], 400, body)
        big = json.dumps({"epoch": self.epoch, "known": ["x" * 1000] * 2000}).encode()
        self.assertEqual(self.http(None, path=path, body=big)[0], 413)


class LedgerEpochTests(LedgerBase):
    """#126 review rounds 5 and 6: no wall clock decides admission or retirement; ordered gate epochs and counters do.
    The guard takes an epoch only from a fence, never lowers it, and refuses every other epoch's ticket."""

    def test_round5_repro_a_future_dated_ticket_is_never_acknowledged_and_then_admitted(self):
        # the reviewer's id: once the 13-digit-millisecond format, now malformed: retired, and never admitted
        future = "1800000061000." + "a" * 32
        self.assertEqual(self.ledger.retire([future]), [future])
        self.assertIsNotNone(self.ledger.open(future))
        self.assertIsNotNone(self.ledger.bind(future, "row"))
        # a well-formed id while no epoch is current (a fresh process): never acknowledged
        fresh = self.guard.TicketLedger()
        t = self.tid()
        self.assertEqual(fresh.retire([t]), [])
        self.assertEqual(fresh.retire([t]), [])
        self.assertTrue(fresh.fence(self.epoch, [])["clear"])
        self.assertEqual(fresh.retire([t]), [t])                        # tombstoned now
        self.assertIsNotNone(fresh.open(t))
        self.assertIsNotNone(fresh.bind(t, "row"))

    def test_round5_repro_a_clock_rollback_cannot_readmit_a_previous_gates_ticket(self):
        old_epoch = self.epoch
        old = self.tid()                                                # minted by the old gate, never seen here
        new = self.epoch + 1                                            # the restarted gate, its clock set back
        with patch.object(time, "time", lambda: 1_700_000_000.0):
            self.assertTrue(self.ledger.fence(new, [])["clear"])
            self.assertIsNotNone(self.ledger.open(old))
            self.assertIsNotNone(self.ledger.bind(old, "row"))
            self.assertEqual(self.http(old)[0], 409)
            self.assertEqual(self.calls, [])                            # nothing reached vllm-mlx
            self.assertEqual(self.ledger.retire([old]), [old])          # below the current epoch: for good
            self.assertEqual(self.ledger.fence(old_epoch, [])["refused"], True)   # and never current again
            self.assertEqual(self.ledger.epoch, new)

    def test_round5_clocks_jumping_on_either_side_change_nothing(self):
        r = local_gate.Retirer("127.0.0.1", 9, call=lambda p, b: {}, start=False, epoch=self.epoch)
        r._seq = next(self.seq)
        jumps = itertools.cycle([1.8e9, 0.0, 4.1e9, 1.8e9 - 3600, 2.5e9, 1.0])
        with patch.object(time, "time", lambda: next(jumps)):
            for _ in range(4):
                t = r.mint()
                self.assertEqual(self.http(t), (200, b"ok"))
                self.sched.step()
                self.sched.done.add(self.rids[-1])
                self.sched.step()
                self.assertEqual(self.retire(t), [t])
                self.assertEqual(self.http(t)[0], 409)                  # retired: refused should it arrive late

    def test_round5_an_old_epoch_request_after_the_fence_is_refused_and_never_binds(self):
        inflight = self.tid()
        self.assertIsNone(self.ledger.open(inflight))                   # the old gate's exchange, not yet bound
        new = self.epoch + 1
        self.assertFalse(self.ledger.fence(new, [])["clear"])           # its exchange is still open
        self.assertIsNotNone(self.ledger.bind(inflight, "late-row"))    # refused: an older epoch
        self.assertNotIn("late-row", self.ledger._rids)
        self.ledger.ended(inflight)
        self.assertTrue(self.ledger.fence(new, [])["clear"])
        late = self.tid()                                               # arriving after the fence cleared
        code, body = self.http(late)
        self.assertEqual(code, 409)
        self.assertIn("an older gate epoch", json.loads(body)["detail"])
        self.assertEqual(self.calls, [])
        self.assertTrue(self.ledger.fence(new, [])["clear"])

    def test_round5_a_bound_old_epoch_request_never_enters_the_scheduler(self):
        t = self.tid()
        self.assertIsNone(self.ledger.open(t))
        self.assertIsNone(self.ledger.bind(t, "r1"))                    # bound before the fence...
        self.ledger.fence(self.epoch + 1, [])
        with self.ledger.admit_lock:
            self.assertFalse(self.ledger.entering("r1"))                # ...refused at the scheduler's add

    def test_round5_a_delayed_same_epoch_request_after_its_retirement_is_refused(self):
        t = self.tid()
        self.assertEqual(self.retire(t), [t])                           # the gate gave up before it arrived
        self.assertEqual(self.http(t)[0], 409)
        self.assertEqual(self.calls, [])
        u = self.tid()
        self.http(u)
        self.sched.step()
        self.sched.done.add(self.rids[0])
        self.sched.step()
        self.assertEqual(self.retire(u), [u])
        self.assertEqual(self.http(u)[0], 409)                          # served once, retired: never again

    def test_round6_an_older_epoch_is_refused_at_open_bind_and_the_scheduler_and_a_newer_one_until_fenced(self):
        older, newer = self.tid(epoch=self.epoch - 1), self.tid(epoch=self.epoch + 1)
        self.assertIn("older gate epoch", self.ledger.open(older))
        self.assertIn("not fenced yet", self.ledger.open(newer))
        self.assertEqual(self.ledger.retire([older, newer]), [older])   # the newer one may still be admitted
        self.assertEqual(self.http(newer)[0], 409)
        self.assertEqual(self.calls, [])
        t = self.tid()
        self.assertIsNone(self.ledger.open(t))
        self.assertIsNone(self.ledger.bind(t, "r1"))
        self.assertTrue(self.ledger.fence(self.epoch + 1, [t])["clear"])   # the newer gate fences
        self.assertIsNotNone(self.ledger.bind(t, "r2"))
        with self.ledger.admit_lock:
            self.assertFalse(self.ledger.entering("r1"))
        self.assertIsNone(self.ledger.open(newer))                      # its ticket is admitted now
        gone = self.tid(epoch=self.epoch)
        self.assertEqual(self.ledger.retire([gone]), [gone])            # below the current epoch: untracked
        self.assertNotIn(gone, self.ledger._tickets)


class LedgerFreshEpochTests(LedgerBase):
    """#126 review round 6 (P2a, P2b), on a guard no gate has fenced yet (a fresh vllm-mlx process)."""
    FENCED = False

    def test_round6_repro_p2a_an_unseen_ticket_of_an_epoch_that_may_still_be_fenced_is_never_acknowledged(self):
        a, b = 3, 4                                                     # a stale gate A, the live gate B
        ta, tb = self.tid(epoch=a), self.tid(epoch=b)
        self.assertIn("no gate epoch fenced", self.ledger.open(ta))     # no first-ticket adoption
        self.assertIsNone(self.ledger.epoch)
        self.assertEqual(self.ledger.retire([tb]), [])                  # nothing current: never acknowledged
        self.assertTrue(self.ledger.fence(a, [])["clear"])              # the stale gate's (delayed) fence
        self.assertEqual(self.ledger.retire([tb]), [])                  # above the current epoch: not either
        self.assertNotIn(tb, self.ledger._tickets)
        self.assertTrue(self.ledger.fence(b, [tb])["clear"])            # B fences: tb is still B's, reserved
        self.assertEqual(self.ledger.retire([tb]), [tb])                # tombstoned, then acknowledged
        self.assertIsNotNone(self.ledger.open(tb))                      # the delayed ticket: refused
        self.assertIsNotNone(self.ledger.bind(tb, "late"))
        self.assertEqual(self.http(tb)[0], 409)
        self.assertEqual(self.calls, [])

    def test_round6_repro_p2b_a_delayed_old_fence_never_displaces_the_live_gate(self):
        a, b = 3, 4
        self.assertTrue(self.ledger.fence(b, [])["clear"])
        got = self.ledger.fence(a, [])                                  # A's fence, delayed
        self.assertEqual((got["clear"], got.get("refused"), got["epoch"]), (False, True, b))
        self.assertEqual(self.ledger.epoch, b)                          # nothing changed
        self.assertTrue(self.ledger.fence(b, [])["clear"])              # B re-fences: accepted, for good
        self.assertIsNone(self.ledger.open(self.tid(epoch=b)))
        self.assertIsNotNone(self.ledger.open(self.tid(epoch=a)))

    def test_round6_repro_p2a_through_the_retirer_the_reservation_waits_for_the_live_gates_fence(self):
        # the reviewer's second repro: the real Retirer against the real ledger, a delayed ticket of the live gate
        def call(path, body):
            code, raw = self.http(None, path=path, body=json.dumps(body).encode())
            self.assertEqual(code, 200)
            return json.loads(raw)
        stale = local_gate.Retirer("127.0.0.1", 9, call=call, start=False, epoch=3)
        live = local_gate.Retirer("127.0.0.1", 9, call=call, start=False, epoch=4)
        adm = local_governor.Admission(FixedGovernor())
        res = adm.try_acquire(load=adm.text_load(100, 1))[0]
        tb = live.mint()                                                # sent; its bytes are delayed
        live.hand_over(res, tb)
        self.assertEqual(live.poll_once(), 0)                           # a fresh guard: not acknowledged
        self.assertIsNone(stale.fence_once())                           # the stale gate fences first
        self.assertEqual(live.poll_once(), 0)                           # above the current epoch: still held
        self.assertEqual((adm.inflight, adm.retiring), (1, 1))
        self.assertIsNone(live.fence_once())                            # the live gate fences
        stale._raise_fence("fenced: lost contact (test)")                # the stale gate tries again: refused
        self.assertIn("refuses this gate's epoch", stale.fence_once() or "")
        self.assertEqual(live.poll_once(), 1)                           # tombstoned: released
        self.assertEqual((adm.inflight, adm.reserved), (0, 0))
        self.assertEqual(self.http(tb)[0], 409)                         # the delayed ticket arrives: refused
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sched.waiting, collections.deque())

    def test_round6_delayed_fences_and_tickets_in_every_order_around_a_guard_restart(self):
        # an old gate (epoch 3) and the restarted gate (epoch 4); each event once, in every order, with the guard
        # restarting (a fresh ledger) before any of them; every state is checked against the rules after each event
        old, new = 3, 4
        events = ("fence-old", "ticket-old", "fence-new", "ticket-new")
        for order in itertools.permutations(events):
            for cut in range(len(order) + 1):
                with self.subTest(order=order, restart_before=cut):
                    ledger = self.guard.TicketLedger()
                    fenced, seen = [], {}
                    t = {"ticket-old": self.tid(epoch=old), "ticket-new": self.tid(epoch=new)}
                    for i, ev in enumerate(order):
                        if i == cut:
                            ledger, fenced = self.guard.TicketLedger(), []   # vllm-mlx restarts: forgets all
                        if ev.startswith("fence"):
                            e = old if ev == "fence-old" else new
                            got = ledger.fence(e, [])
                            self.assertEqual(got.get("refused", False), new in fenced and e == old)
                            if not got.get("refused"):
                                fenced.append(e)
                        else:
                            why = ledger.open(t[ev])
                            e = old if ev == "ticket-old" else new
                            current = max(fenced) if fenced else None
                            self.assertEqual(why is None, current == e, (ev, why, fenced))
                            if why is None:
                                ledger.ended(t[ev])
                            seen[ev] = why is None
                    current = max(fenced) if fenced else None
                    self.assertEqual(ledger.epoch, current)
                    # retirement: an id is acknowledged untracked only when it can never be admitted
                    for ev, tid in t.items():
                        e = old if ev == "ticket-old" else new
                        got = ledger.retire([tid])
                        if current is None or e > current:
                            self.assertEqual(got, [], (ev, current))
                        else:
                            self.assertEqual(got, [tid], (ev, current))
                            self.assertIsNotNone(ledger.open(tid))     # and never admitted afterwards
                    if current == new:
                        self.assertTrue(ledger.fence(old, [])["refused"])
                        self.assertEqual(ledger.epoch, new)


class LedgerBoundTests(LedgerBase):
    """#126 review round 4 (P2b): at most TICKET_MAX ids are tracked on every insert path, and an evicted tombstone
    cannot be replayed (the epoch's floor rises past every evicted counter)."""

    def test_round4_repro_tombstones_stay_within_the_bound_and_are_never_replayed(self):
        cap = self.guard.TICKET_MAX
        ids = [self.tid() for _ in range(cap + 100)]                    # distinct, increasing counters
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
            live = [self.tid() for _ in range(8)]
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

    def test_round5_repro_a_shadow_package_in_a_by_name_folder_is_refused(self):
        # the reviewer's repro: mlx_lm/tool_parsers/qwen3_coder/__init__.py beside the pinned qwen3_coder.py (a
        # package wins: tokenizer_utils.py 630 imports exactly this name); and the same in chat_templates
        import importlib.machinery
        suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
        for folder, stem in (("tool_parsers", "qwen3_coder"), ("chat_templates", "deepseek_v32")):
            for rel in (f"{stem}/__init__.py", f"{stem}/x.py", f"planted{suffix}", "planted.abi3.so", "evil.pth",
                        f"{stem}.pyc", "notes.txt"):
                for argv in (SERVE_TEXT, ["--check", "/models/text"]):
                    with self.subTest(f"{folder}/{rel} {argv[0]}"):
                        self._clean()
                        path = Path(self.dir, "mlx_lm", folder, *rel.split("/"))
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(f"open({str(self.marker)!r}, 'w').write('ran')\n")
                        top = Path(self.dir, "mlx_lm", folder, rel.split("/")[0])
                        try:
                            rc, err = self.run_main(argv)
                        finally:
                            shutil.rmtree(top) if top.is_dir() else top.unlink()
                        self.assertEqual(rc, 2, err)
                        self.assertIn(f"mlx_lm/{folder}/{rel.split('/')[0]} is not in the 0.31.3 release", err)
                        self.assertFalse(self.marker.exists())
        self._clean()
        self.assertEqual(self.run_main(SERVE_TEXT)[0], 0)

    def test_round5_a_shadow_package_beside_any_pinned_module_is_refused(self):
        # outside the by-name folders too: mlx_lm/utils/__init__.py would be imported in place of the pinned utils.py
        for rel, name in (("utils/__init__.py", "mlx_lm.utils"),
                          ("models/qwen3_5/__init__.py", "mlx_lm.models.qwen3_5")):
            with self.subTest(rel):
                self._clean()
                path = Path(self.dir, "mlx_lm", *rel.split("/"))
                path.parent.mkdir(parents=True)
                path.write_text(f"open({str(self.marker)!r}, 'w').write('ran')\n")
                try:
                    rc, err = self.run_main(SERVE_TEXT)
                finally:
                    shutil.rmtree(path.parent)
                self.assertEqual(rc, 2, err)
                # round 6: the tree check names it first (verify_lm_resolution alone: MlxLmPinTests)
                self.assertIn(f"mlx_lm/{rel.rsplit('/', 1)[0]} is a package beside {name.rsplit('.', 1)[1]}.py", err)
                self.assertFalse(self.marker.exists())

    def test_round5_cached_bytecode_beside_a_pinned_file_is_never_read(self):
        # a __pycache__ file Python would accept for tokenizer_utils.py (its header carries the source's mtime and
        # size, the only things Python checks) holding other code: the guard serves with its bytecode isolated
        import importlib.util
        import marshal
        import sys
        src = Path(self.dir, "mlx_lm", "tokenizer_utils.py")
        st = src.stat()
        code = compile(f"open({str(self.marker)!r}, 'w').write('ran')\n", str(src), "exec")
        # the in-tree cache a venv's Python reads (no pycache_prefix; Apple's own Python sets one: not here)
        with patch.object(sys, "pycache_prefix", None):
            cached = Path(importlib.util.cache_from_source(str(src)))
        self.assertEqual(cached.parent, src.parent / "__pycache__")
        cached.parent.mkdir(exist_ok=True)
        cached.write_bytes(importlib.util.MAGIC_NUMBER + (0).to_bytes(4, "little")
                           + (int(st.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
                           + (st.st_size & 0xFFFFFFFF).to_bytes(4, "little") + marshal.dumps(code))
        with patch.object(sys, "pycache_prefix", None):                 # the control: Python does read it
            spec = importlib.util.spec_from_file_location("cortados_control_tokenizer_utils", str(src))
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
        self.assertTrue(self.marker.exists(), "the forged cache was not read: the test proves nothing")
        self.marker.unlink()
        for folder in ("tool_parsers", "chat_templates"):               # a pycache in a by-name folder is allowed
            Path(self.dir, "mlx_lm", folder, "__pycache__").mkdir(exist_ok=True)
        self._clean()
        with patch.object(sys, "pycache_prefix", None):                 # main() sets its own, then run_main restores
            rc, err = self.run_main(SERVE_TEXT)
        self.assertEqual(rc, 0, err)
        self.assertIn("mlx_lm.tokenizer_utils", sys.modules)             # imported, from its verified source
        self.assertFalse(self.marker.exists(), "cached bytecode ran in place of a pinned source")

    def test_round6_repro_a_sourceless_version_package_never_runs(self):
        # the reviewer's repro: the release's __init__ runs `from ._version import __version__`, and a pyc-only
        # mlx_lm/_version/ package wins over the pinned _version.py (sys.pycache_prefix does not stop a sourceless
        # loader): before, serve returned 0 and the marker ran
        import importlib.util
        import marshal
        import sys
        lm = Path(self.dir, "mlx_lm")
        (lm / "__init__.py").write_text('import mlx.nn\nfrom . import convert, utils, tokenizer_utils\n'
                                        'from ._version import __version__\n')
        (lm / "_version.py").write_text('__version__ = "0.31.3"\n')
        pins = {**self.guard.MLX_LM_PINNED_SHA256, "__init__.py": self._sha("mlx_lm", "__init__.py"),
                "_version.py": self._sha("mlx_lm", "_version.py")}
        p = patch.object(self.guard, "MLX_LM_PINNED_SHA256", pins)
        p.start()
        self.addCleanup(p.stop)
        self._clean()
        rc, err = self.run_main(SERVE_TEXT)                              # the clean chain, _version included
        self.assertEqual(rc, 0, err)
        self.assertIn("mlx_lm._version", sys.modules)
        code = compile(f"open({str(self.marker)!r}, 'w').write('ran')\n__version__ = '0.31.3'\n", "_version", "exec")
        pkg = lm / "_version"
        pkg.mkdir()
        (pkg / "__init__.pyc").write_bytes(importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(code))
        self._clean()
        import importlib
        importlib.invalidate_caches()
        spec = self.guard.find_on_disk("mlx_lm._version", [str(lm)])     # the control: Python would import it
        self.assertTrue(spec.origin.endswith("__init__.pyc"))
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
        self.assertTrue(self.marker.exists(), "the planted package does not run: the test proves nothing")
        self.marker.unlink()
        for argv in (SERVE_TEXT, ["--check", "/models/text"]):
            with self.subTest(argv[0]):
                self._clean()
                rc, err = self.run_main(argv)
                self.assertEqual(rc, 2, err)
                self.assertIn("mlx_lm/_version is a package beside _version.py", err)
                self.assertFalse(self.marker.exists(), "the sourceless _version ran")
                self.assertNotIn("mlx_lm", sys.modules)
        shutil.rmtree(pkg)
        self._clean()
        self.assertEqual(self.run_main(SERVE_TEXT)[0], 0)

    def test_round6_sourceless_bytecode_in_any_verified_tree_is_refused_before_import(self):
        import sys
        for rel in ("vllm_mlx/stray.pyc", "vllm_mlx/engine/planted/__init__.pyc", "mlx_lm/models/qwen3_5.pyc",
                    "mlx/nn/layers/evil.pyc", "mlx/utils.pyc"):
            for argv in (SERVE_TEXT, ["--check", "/models/text"]):
                with self.subTest(f"{rel} {argv[0]}"):
                    self._clean()
                    path = Path(self.dir, *rel.split("/"))
                    made = None if path.parent.exists() else path.parent
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"\0" * 16)
                    try:
                        rc, err = self.run_main(argv)
                    finally:
                        shutil.rmtree(made) if made is not None else path.unlink()
                    self.assertEqual(rc, 2, err)
                    self.assertIn(f"{rel} is sourceless bytecode", err)
                    self.assertNotIn("mlx_lm", sys.modules)
                    self.assertNotIn("mlx.nn", sys.modules)
        self._clean()
        self.assertEqual(self.run_main(SERVE_TEXT)[0], 0)

    def test_round6_the_mlx_vlm_tree_is_checked_for_sourceless_modules_too(self):
        vllm, vlm = str(Path(self.dir, "vllm_mlx")), str(Path(self.dir, "mlx_vlm"))
        self.guard.verify_vision(vllm, vlm, None)
        planted = Path(vlm, "models", "planted.pyc")
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_bytes(b"\0" * 16)
        with self.assertRaisesRegex(self.guard.GuardError, "mlx_vlm/models/planted.pyc is sourceless bytecode"):
            self.guard.verify_vision(vllm, vlm, None)
        self.guard.verify_vision(vllm, vlm, "0.7.12", {}, {})              # the post-import version check reads no tree
        planted.unlink()
        self.guard.verify_vision(vllm, vlm, None)

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
