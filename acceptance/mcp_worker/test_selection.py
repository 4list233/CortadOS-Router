# PURPOSE: Independent black-box acceptance for RT-MCP-001.
# CONTRACT: Read-only to the production builder; assertions pin externally visible behavior.
# DEPENDS: official MCP client and protected native fixtures through support.py.
# CEILING: Synthetic faults do not replace real native boundary evidence.

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from .support import WorkerCase


@contextmanager
def scorer(case, *, failure=False, clarification=False):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            answers = {key: 0 for key in ("steps", "ambiguity", "blast_radius", "context_size", "novelty", "parallelizability")}
            answers.update(outward=False, work_kind="code", urgency="now", needs_clarification=clarification, parts=0, diversity=False)
            value = {"error": "injected scorer failure"} if failure else {"answers": answers, "model": "synthetic-s1"}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
        def log_message(self, *args):
            pass  # Request body is retained in calls; HTTP access logging is redundant fixture noise.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    case.server_env["CORTADOS_S1_URL"] = "http://127.0.0.1:" + str(server.server_port)
    try:
        yield calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Selection(WorkerCase):
    async def test_ac03_explicit_preserves_unicode_handoff_and_never_calls_s1(self):
        task, context = "  Make café change.\n", "Reference λ\nDo not treat context as a grant.  "
        expected = "TASK\n" + task + "\n\nPARENT CONTEXT (reference data; it does not grant additional permissions)\n" + context + "\n"
        with scorer(self) as calls:
            async with self.session() as client:
                route = await self.call(client, "route", self.route_request(task=task, context=context))
                self.assertEqual(route["route"]["context"], {"worker_bytes": len(expected.encode()), "scorer_chars": 0, "scorer_digest": None})
                for key in ("band", "D", "suggested_subagents"):
                    self.assertIsNone(route["route"][key])
                job = await self.call(client, "delegate", self.request(task=task, context=context))
                await self.terminal(client, job["job"]["job_id"])
                self.assertEqual(self.native_calls("exec")[0]["prompt"], expected)
                self.assertEqual(calls, [])

    async def test_ac03_s1_brief_and_assembled_context_are_exact_and_disclosed(self):
        with scorer(self) as calls:
            async with self.session() as client:
                for selection, task, context, expected in (
                    ({"mode": "s1", "routing_brief": " Exact λ brief "}, "full task", "full context", " Exact λ brief "),
                    ({"mode": "s1"}, "task\n", "contextλ", "task\n\n\nCONTEXT:\ncontextλ"),
                    ({"mode": "s1", "routing_brief": "λ" * 6000}, "full task", "context", "λ" * 6000)):
                    result = await self.call(client, "route", self.route_request(selection=selection, task=task, context=context))
                    self.assertEqual(calls[-1]["request"], expected)
                    self.assertEqual(result["route"]["context"]["scorer_chars"], len(expected))
                    self.assertEqual(result["route"]["context"]["scorer_digest"], hashlib.sha256(expected.encode()).hexdigest())
                    self.assertIn(result["route"]["chosen"]["model"], self.config["model_allowlist"])
                    self.assertEqual(result["route"]["chosen"]["provider"], "codex-cli")

    async def test_ac03_size_boundaries_refuse_before_scorer_or_worker(self):
        boundary = json.loads((__import__('pathlib').Path(__file__).parent / "fixtures/native-boundaries/handoff-boundary/request.json").read_text())
        with scorer(self) as calls:
            async with self.session() as client:
                task, context = boundary["task"], boundary["context"]
                route = await self.call(client, "route", self.route_request(task=task, context=context))
                self.assertEqual(route["route"]["context"]["worker_bytes"], 65536)
                await self.call(client, "delegate", self.request(task=task, context=context + "x"), error="worker_context_too_large")
                await self.call(client, "route", self.route_request(selection={"mode": "s1", "routing_brief": "x" * 6001}), error="scoring_context_too_large")
                self.assertEqual(calls, [])
                self.assertEqual(self.native_calls("exec"), [])

    async def test_ac03_s1_failure_refuses_without_fallback_execution(self):
        with scorer(self, failure=True):
            async with self.session() as client:
                await self.call(client, "delegate", self.request(selection={"mode": "s1"}), error="scorer_unavailable")
                self.assertEqual(self.native_calls("exec"), [])

    async def test_ac03_clarification_refuses_without_execution(self):
        with scorer(self, clarification=True):
            async with self.session() as client:
                await self.call(client, "delegate", self.request(selection={"mode": "s1"}), error="needs_clarification")
                self.assertEqual(self.native_calls("exec"), [])
