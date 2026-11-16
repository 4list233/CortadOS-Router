"""#124: a request a sensitivity detector marked (the x-cortados-local flag, the keyword list, the S1 signal, a cloud
refusal), answered in the cloud because the Pro could not take it, carries a short notice as its own leading block, and
its route row records the detector and reason categories. Fakes only: the fake Claude CLI, test_provider_local's
in-thread fake Ollama, a stubbed select and core.decide; a throwaway router.db. The keyword is a neutral placeholder."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import core
from cortados_router.provider import (anthropic_wire, live, local_lane, responses_wire, routing, sensitive_notice,
                                      server, tasks)
from tests.test_local_effort_reroute import RerouteEndToEndBase
from tests.test_local_mlx import _gate_too_large, _gate_unsupported
from tests.test_provider import READ_TOOL, TOKEN
from tests.test_provider_local import FakeOllama, TAG
from tests.test_provider_local_auto import body
from tests.test_provider_sensitive import (CLEAN, R3_ROUTE, SECRET_TERM, ContinuationRecheckBase, SensitiveBase,
                                           write_config)
from tests.provider_fakes import setUpModule, tearDownModule  # noqa: F401  (FAKE_* reach the fake CLIs, #106)

NOTICE_START = "⚠ CortadOS: this request was marked sensitive ("
CLOUD = "claude-opus-test"                         # R3_ROUTE's model: what the stubbed select routes to


def sse_events(raw):
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]


def notice(detector, reason, model=CLOUD):
    return sensitive_notice.text(detector, reason, model)


class VocabularyTests(unittest.TestCase):
    def test_wording(self):
        self.assertEqual(notice("flag", "too_large"),
                         "⚠ CortadOS: this request was marked sensitive (flag) but the Pro couldn't take it (too large),"
                         " so it was answered by claude-opus-test in the cloud.")
        self.assertIn("(S1)", notice("s1", "switched_off"))
        self.assertIn("(cloud refusal)", notice("refusal", "unavailable"))
        self.assertIn("(image or unsupported content)", notice("keyword", "unsupported"))

    def test_refused_continuation_wording(self):
        # Review round 2: a refused continuation says plainly that the cloud model refused and the turn stayed.
        self.assertEqual(notice("refusal", "refused_continuation"),
                         "⚠ CortadOS: claude-opus-test in the cloud refused this turn (marked sensitive: cloud refusal)."
                         " The turn was not moved to the Pro (refusal after the opening turn): only a conversation's"
                         " opening turn can be moved, so the refusal stands.")
        got = notice(f"keyword: {SECRET_TERM}", "refused_continuation", f"model with {SECRET_TERM}")
        self.assertNotIn("zebra", got)
        self.assertTrue(sensitive_notice.is_notice(got))

    def test_only_categories_ever_reach_the_text(self):
        # A caller handing request text or a matched term as a category gets the fallback labels, never the string.
        got = notice(f"keyword: {SECRET_TERM}", f"the {SECRET_TERM} is too big", f"model with {SECRET_TERM}")
        self.assertNotIn("zebra", got)
        self.assertIn("(unreadable detector)", got)
        self.assertIn("(unavailable)", got)
        self.assertIn("answered by a cloud model in the cloud.", got)
        self.assertEqual(sensitive_notice.mark("keyword", "x\ny"), {"detector": "keyword", "reason": "unavailable"})

    def test_every_reason_and_detector_round_trips_through_the_recogniser(self):
        for det in sensitive_notice.DETECTORS:
            for why in sensitive_notice.REASONS:
                t = notice(det, why)
                self.assertTrue(sensitive_notice.is_notice(t), t)
                self.assertEqual(sensitive_notice.strip_leading(t + "the answer"), "the answer")

    def test_strip_leading_leaves_other_text_alone(self):
        for t in ("the answer", "⚠ CortadOS: something else", "x " + notice("flag", "busy")):
            self.assertEqual(sensitive_notice.strip_leading(t), t)

    def test_opt_out_header(self):
        self.assertTrue(sensitive_notice.opted_out({"x-cortados-sensitive-notice": " OFF "}))
        for v in (None, "", "on", "no", "0"):
            self.assertFalse(sensitive_notice.opted_out({"x-cortados-sensitive-notice": v} if v is not None else {}))

    def test_valid_record(self):
        rec = sensitive_notice.record({"detector": "flag", "reason": "too_large"}, sensitive_notice.SHOWN)
        self.assertEqual(sensitive_notice.valid_record(rec), rec)
        for bad in (None, [], {**rec, "detector": SECRET_TERM}, {**rec, "notice": "maybe"}, {**rec, "reason": 3}):
            self.assertIsNone(sensitive_notice.valid_record(bad))


class FingerprintTests(unittest.TestCase):
    """The session fingerprints the model's blocks; the client sends the notice back however it kept it."""

    MODEL = [{"type": "text", "text": "Let me look."}, {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}]

    def test_separate_merged_or_dropped_notice_all_match(self):
        want = anthropic_wire.reply_fingerprint(self.MODEL)
        n = notice("flag", "too_large")
        separate = [{"type": "text", "text": n}] + self.MODEL
        merged = [{"type": "text", "text": n + "\n\nLet me look."}, self.MODEL[1]]
        for blocks in (separate, merged, self.MODEL):
            self.assertEqual(anthropic_wire.reply_fingerprint(blocks), want)

    def test_a_notice_does_not_hide_a_different_reply(self):
        other = [{"type": "text", "text": notice("flag", "busy")}, {"type": "text", "text": "Something else."}]
        self.assertNotEqual(anthropic_wire.reply_fingerprint(other), anthropic_wire.reply_fingerprint(self.MODEL))

    def test_rebuild_transcript_leaves_the_notice_out(self):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": [{"type": "text", "text": notice("flag", "too_large")},
                                                  {"type": "text", "text": "hello"}]}]
        text = json.dumps(anthropic_wire.rebuild_transcript(msgs))
        self.assertNotIn("CortadOS: this request", text)
        self.assertIn("hello", text)


class WhyCategoryTests(unittest.TestCase):
    def test_routing_reasons_map_to_categories(self):
        cases = {
            "excluded by the client (x-cortados-exclude-model)": "excluded",
            "probe failed within the last 30s": "unavailable",
            "unavailable (node pro: local gate: local switch off)": "switched_off",
            "unavailable (node pro: local gate: local disabled: on battery)": "on_battery",
            "unavailable (node pro did not answer /api/tags within 1s (asleep, offline or busy?): x)": "unavailable",
            "unavailable (" + local_lane.GATE_REFUSED.format(node="pro") + ")": "gate_token",
            "does not list qwen:27b": "model_missing",
            "node busy (2 in flight)": "busy",
            "request ≈40k tokens exceeds its 32k context": "too_large",
            "too large for the node (request ≈90k)": "too_large",
            "the request came in on /v1/responses (the local relay speaks Messages only)": "responses_wire",
            "S1 is degraded (a degraded scorer never routes local)": "s1_degraded",
            "outward effect (S1 outward=true): never local": "outward_effect",
            "blast_radius 2 > 1 (an effect outside the workspace)": "outward_effect",
            "S1 gave no answers": "not_admitted",
            "something new": "unavailable",
        }
        for why, want in cases.items():
            self.assertEqual(routing.why_category(why), want, why)
            self.assertIn(want, sensitive_notice.REASONS)


class ChooseMarksCloudRoutesTests(SensitiveBase):
    """routing.choose: a request a detector marked that stays on S1's cloud route carries the mark; nothing else does."""

    def mark(self, r):
        return r.get(sensitive_notice.ROUTE_KEY)

    def test_each_detector_with_the_pro_switched_off(self):
        self.unavailable = "node pro: local gate: local switch off"
        self.assertEqual(self.mark(self.choose(local="sensitive")), {"detector": "flag", "reason": "switched_off"})
        self.assertEqual(self.mark(self.choose(body(f"the {SECRET_TERM}"))),
                         {"detector": "keyword", "reason": "switched_off"})
        self.answers(sensitive=True)
        with patch.object(routing, "S1_SENSITIVE_ANSWER", "sensitive"):
            self.assertEqual(self.mark(self.choose()), {"detector": "s1", "reason": "switched_off"})

    def test_the_pro_on_battery_or_unreachable(self):
        self.unavailable = "node pro: local gate: local disabled: on battery"
        self.assertEqual(self.mark(self.choose(local="sensitive"))["reason"], "on_battery")
        routing._auto_down.clear()
        self.unavailable = "node pro did not answer /api/tags within 1s (asleep, offline or busy?): OSError: x"
        self.assertEqual(self.mark(self.choose(local="sensitive"))["reason"], "unavailable")
        self.assertEqual(self.mark(self.choose(local="sensitive"))["reason"], "unavailable")   # the AUTO_DOWN window

    def test_too_large_for_the_context_and_other_skips(self):
        self.assertEqual(self.mark(self.choose(body("x" * (4 * 40000)), local="sensitive"))["reason"], "too_large")
        self.tags = {"phi:latest"}
        self.assertEqual(self.mark(self.choose(local="sensitive"))["reason"], "model_missing")
        self.tags = {TAG}
        self.assertEqual(self.mark(self.choose(local="sensitive", exclude_model="qwen-27b"))["reason"], "excluded")
        self.assertEqual(self.mark(self.choose(body(**{routing.NO_LOCAL_KEY: True}), local="sensitive"))["reason"],
                         "responses_wire")
        self.answers(outward=True)
        self.assertEqual(self.mark(self.choose(local="sensitive"))["reason"], "outward_effect")

    def test_no_sensitive_section_or_an_unreadable_list(self):
        os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9")
        self.assertEqual(self.mark(self.choose(local="sensitive")), {"detector": "flag", "reason": "not_configured"})
        os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9", sensitive=True)
        (Path(self.dir) / "keywords.txt").unlink()
        with self.assertLogs(routing.logger, level="WARNING"):
            self.assertEqual(self.mark(self.choose(body("hello"))), {"detector": "unknown", "reason": "config_error"})

    def test_no_mark_when_taken_cleared_prefer_or_pinned(self):
        self.assertIsNone(self.mark(self.choose(local="sensitive")))            # taken: local
        self.unavailable = "node pro: local gate: local switch off"
        self.assertIsNone(self.mark(self.choose(body("hello"))))                # no detector marks it
        self.assertIsNone(self.mark(self.choose(body("hello"), local="prefer")))   # a cost preference only
        self.unavailable = None
        pinned = self.choose(body(model="sub/phi:latest"), local="sensitive")   # a pin: the person's choice
        self.assertTrue(pinned["pinned"])
        self.assertIsNone(self.mark(pinned))

    def test_the_local_bind_records_its_detector_for_the_reroute(self):
        r = self.choose(local="sensitive")
        self.assertEqual(r[sensitive_notice.DETECTOR_KEY], "flag")
        self.assertNotIn(local_lane.CLOUD_REROUTE_OK, r)


class NoticeHarness(ContinuationRecheckBase):
    """The provider end to end: a `cortados` opening turn routed through routing.choose (select stubbed to the R3
    cloud route), the fake Ollama behind the local lane and the fake Claude CLI as the cloud."""

    DETECTOR = "flag"
    NODE_POST = None                             # a replacement for FakeOllama.do_POST (the gate's refusal)

    def setUp(self):
        super().setUp()
        if type(self).NODE_POST is not None:
            p = patch.object(FakeOllama, "do_POST", type(self).NODE_POST)   # the class's plain function, unbound
            p.start()
            self.addCleanup(p.stop)
        local_lane._gate_reports.clear()
        self.addCleanup(local_lane._gate_reports.clear)
        self.records = []
        self.reg.sensitive_cloud = lambda rid, rec: self.records.append((rid, rec)) or True
        if self.DETECTOR == "keyword":
            write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", sensitive=True,
                         keywords=f"{SECRET_TERM}\n")
        if self.DETECTOR == "s1":
            p = patch.object(routing, "S1_SENSITIVE_ANSWER", "sensitive")
            p.start()
            self.addCleanup(p.stop)
            selected = {**R3_ROUTE, "decision": {**R3_ROUTE["decision"], "answers": {**CLEAN, "sensitive": True}}}
            routing.execution_routing.select = lambda core_, **kw: json.loads(json.dumps(
                {**selected, "route_id": "execution:provider:call:" + os.urandom(4).hex()}))

    def opening(self, text="summarise the notes in this folder", stream=False, tools=True, headers=None):
        if self.DETECTOR == "keyword":
            text = f"{text} about the {SECRET_TERM}"
        req = {**body(text), "stream": stream, **({"tools": [READ_TOOL]} if tools else {})}
        hdr = {**({"x-cortados-local": "sensitive"} if self.DETECTOR == "flag" else {}), **(headers or {})}
        return req, hdr

    def assertNoticeJson(self, got, reason, detector=None):
        self.assertEqual(got["content"][0], {"type": "text", "text": notice(detector or self.DETECTOR, reason)})
        self.assertTrue(got["content"][1]["text"].startswith("echo: "), got["content"])
        self.assertEqual(got["stop_reason"], "end_turn")
        self.assertNotIn(SECRET_TERM, got["content"][0]["text"])

    def assertNoticeSse(self, raw, reason, detector=None):
        evs = sse_events(raw)
        starts = [e for e in evs if e["type"] == "content_block_start"]
        deltas = [e for e in evs if e["type"] == "content_block_delta"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual([e["index"] for e in starts], [0, 1])
        self.assertEqual([e["index"] for e in stops], [0, 1])
        self.assertEqual(deltas[0], {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": notice(detector or self.DETECTOR, reason)}})
        self.assertTrue(deltas[1]["delta"]["text"].startswith("echo: "))
        self.assertEqual([e["delta"]["stop_reason"] for e in evs if e["type"] == "message_delta"], ["end_turn"])
        self.assertEqual(evs[-1]["type"], "message_stop")

    def assertRecorded(self, reason, detector=None, shown=sensitive_notice.SHOWN):
        (rid, rec), = self.records
        self.assertTrue(rid.startswith("execution:provider:call:"))
        self.assertEqual(rec, {"detector": detector or self.DETECTOR, "reason": reason, "notice": shown})


class GateRefusalMixin:
    """The node bound the opening turn, then its gate refused it for good: re-routed to the cloud with the notice."""

    REASON = None

    def test_json(self):
        req, hdr = self.opening()
        with self.assertLogs(server.logger, level="WARNING") as cm:
            got = self.post(req, headers=hdr)
        self.assertNoticeJson(got, self.REASON)
        self.assertEqual(len(self.node_posts()), 1)                    # the node was tried once
        self.assertRecorded(self.REASON)
        said = "\n".join(cm.output)
        self.assertIn("sensitive-cloud: route", said)
        self.assertNotIn("zebra", said)                                 # categories only in the log

    def test_sse(self):
        req, hdr = self.opening(stream=True)
        self.assertNoticeSse(self.post(req, headers=hdr), self.REASON)
        self.assertRecorded(self.REASON)

    def test_one_shot(self):
        req, hdr = self.opening(tools=False)
        self.assertNoticeJson(self.post(req, headers=hdr), self.REASON)


class FlagTooLargeTests(GateRefusalMixin, NoticeHarness):
    NODE_POST, REASON = _gate_too_large, "too_large"


class KeywordTooLargeTests(GateRefusalMixin, NoticeHarness):
    DETECTOR, NODE_POST, REASON = "keyword", _gate_too_large, "too_large"


class S1TooLargeTests(GateRefusalMixin, NoticeHarness):
    DETECTOR, NODE_POST, REASON = "s1", _gate_too_large, "too_large"


class FlagImageTests(GateRefusalMixin, NoticeHarness):
    NODE_POST, REASON = _gate_unsupported, "unsupported"


class KeywordImageTests(GateRefusalMixin, NoticeHarness):
    DETECTOR, NODE_POST, REASON = "keyword", _gate_unsupported, "unsupported"


class S1ImageTests(GateRefusalMixin, NoticeHarness):
    DETECTOR, NODE_POST, REASON = "s1", _gate_unsupported, "unsupported"


def _switched_off(handler):
    """FakeOllama's GET /api/tags answered as the Pro's gate does with the manual switch off."""
    raw = json.dumps({"type": "error", "error": {"type": "overloaded_error",
                                                 "message": "local gate: local switch off"}}).encode()
    handler.send_response(503)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class ProSwitchedOffTests(NoticeHarness):
    def setUp(self):
        super().setUp()
        p = patch.object(FakeOllama, "do_GET", _switched_off)
        p.start()
        self.addCleanup(p.stop)

    def test_each_detector_json_and_sse(self):
        for det in ("flag", "keyword", "s1"):
            self.DETECTOR = det
            if det == "keyword":
                write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", sensitive=True,
                             keywords=f"{SECRET_TERM}\n")
            for stream in (False, True):
                routing._auto_down.clear()
                self.records.clear()
                with patch.object(routing, "S1_SENSITIVE_ANSWER", "sensitive" if det == "s1" else None), \
                        patch.object(routing.execution_routing, "select", lambda core_, **kw: json.loads(json.dumps(
                            {**R3_ROUTE, "route_id": "execution:provider:call:" + os.urandom(4).hex(),
                             "decision": {**R3_ROUTE["decision"],
                                          "answers": {**CLEAN, "sensitive": det == "s1"}}}))):
                    req, hdr = self.opening(stream=stream)
                    got = self.post(req, headers=hdr)
                if stream:
                    self.assertNoticeSse(got, "switched_off")
                else:
                    self.assertNoticeJson(got, "switched_off")
                self.assertRecorded("switched_off")
        self.assertEqual(self.node_posts(), [])                         # nothing ever reached the node

    def test_opt_out_suppresses_the_block_but_still_records_and_logs(self):
        req, hdr = self.opening()
        with self.assertLogs(server.logger, level="WARNING") as cm:
            got = self.post(req, headers={**hdr, "X-Cortados-Sensitive-Notice": "off"})
        self.assertEqual(len(got["content"]), 1)
        self.assertTrue(got["content"][0]["text"].startswith("echo: "))
        self.assertRecorded("switched_off", shown=sensitive_notice.OPTED_OUT)
        self.assertTrue(any("notice opted_out" in m for m in cm.output))


class ProUnavailableTests(NoticeHarness):
    def test_node_down(self):
        self.ollama.shutdown()
        self.ollama.server_close()
        for stream in (False, True):
            routing._auto_down.clear()
            self.records.clear()
            req, hdr = self.opening(stream=stream)
            got = self.post(req, headers=hdr)
            (self.assertNoticeSse if stream else self.assertNoticeJson)(got, "unavailable")
            self.assertRecorded("unavailable")


class ToolUseAndContinuationTests(NoticeHarness):
    NODE_POST = _gate_too_large

    def open_tool_turn(self, stream=False):
        req, hdr = self.opening("use tool to read the notes", stream=stream)
        return req, hdr, self.post(req, headers=hdr)

    def test_tool_use_reply_keeps_its_blocks_and_stop_reason(self):
        _, _, got = self.open_tool_turn()
        self.assertEqual([b["type"] for b in got["content"]], ["text", "text", "tool_use"])
        self.assertEqual(got["content"][0]["text"], notice("flag", "too_large"))
        self.assertEqual(got["stop_reason"], "tool_use")

    def test_tool_use_reply_streamed(self):
        _, _, raw = self.open_tool_turn(stream=True)
        evs = sse_events(raw)
        starts = [(e["index"], e["content_block"]["type"]) for e in evs if e["type"] == "content_block_start"]
        self.assertEqual(starts, [(0, "text"), (1, "text"), (2, "tool_use")])
        self.assertEqual([e["delta"]["stop_reason"] for e in evs if e["type"] == "message_delta"], ["tool_use"])

    def continue_with(self, req, got, assistant_content):
        use = next(b for b in got["content"] if b["type"] == "tool_use")
        nxt = {**req, "messages": req["messages"] + [
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "notes"}]}]}
        return self.post(nxt)

    def test_continuation_after_the_warned_turn(self):
        # The client sends the reply back as received, notice block included: the same conversation continues on the
        # same backend (no 409, no rebuild), and the later turn carries no notice.
        req, hdr, got = self.open_tool_turn()
        (s,) = self.reg.sessions.values()
        nxt = self.continue_with(req, got, got["content"])
        self.assertEqual(len(nxt["content"]), 1)
        self.assertTrue(nxt["content"][0]["text"].startswith("tool said: "), nxt["content"])
        self.assertEqual(nxt["stop_reason"], "end_turn")
        self.assertFalse(any(sensitive_notice.is_notice(b.get("text")) for b in nxt["content"]))
        self.assertEqual(list(self.reg.sessions.values()), [s])
        self.assertEqual(len(self.records), 1)                          # recorded once, for the opening turn

    def test_continuation_with_the_notice_merged_or_dropped(self):
        for keep in ("merged", "dropped"):
            for s in list(self.reg.sessions.values()):
                self.reg.drop(s)
            req, hdr, got = self.open_tool_turn()
            content = got["content"][1:]
            if keep == "merged":
                content = [{"type": "text", "text": got["content"][0]["text"] + "\n\n" + content[0]["text"]},
                           content[1]]
            nxt = self.continue_with(req, got, content)
            self.assertTrue(nxt["content"][0]["text"].startswith("tool said"), (keep, nxt))
            self.assertEqual(len(self.reg.sessions), 1, keep)


class CostRerouteHasNoNoticeTests(NoticeHarness):
    """A request no detector marks, bound by a COST rule (auto_route, prefer), then re-routed: no notice, no record."""
    BAND = "R0"
    DETECTOR = None
    NODE_POST = _gate_too_large

    def test_auto_route_and_prefer(self):
        for hdr in ({}, {"x-cortados-local": "prefer"}):
            for stream in (False, True):
                FakeOllama.seen.clear()
                req, _ = self.opening(stream=stream)
                got = self.post(req, headers=hdr)
                if stream:
                    self.assertNotIn(NOTICE_START, got)
                    self.assertIn("echo: ", got)
                else:
                    self.assertEqual(len(got["content"]), 1)
                    self.assertTrue(got["content"][0]["text"].startswith("echo: "))
                self.assertEqual(len(self.node_posts()), 1, hdr)        # it was bound local, then re-routed
        self.assertEqual(self.records, [])

    def test_a_plain_cloud_route_has_no_notice(self):
        routing.execution_routing.select = lambda core_, **kw: json.loads(json.dumps(
            {**R3_ROUTE, "route_id": "execution:provider:call:" + os.urandom(4).hex()}))   # R3: no auto_route band
        got = self.post(self.opening()[0])
        self.assertEqual(len(got["content"]), 1)
        self.assertEqual(self.records, [])


class SensitiveNodeErrorStillNeverReroutesTests(RerouteEndToEndBase):
    """#122 unchanged: a sensitive request's node 5xx is answered, never re-routed, so it never gets a notice."""
    HEADERS_OPEN = {"x-cortados-local": "sensitive"}

    def test_flag_and_keyword(self):
        records = []
        self.reg.sensitive_cloud = lambda rid, rec: records.append(rec) or True
        self.assertAnsweredLocally(self.HEADERS_OPEN, streams=(False, True))
        write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", sensitive=True,
                     keywords=f"{SECRET_TERM}\n")
        self.assertAnsweredLocally(text=f"explain the {SECRET_TERM}", streams=(False, True))
        self.assertEqual(records, [])


class ResponsesWireTests(NoticeHarness):
    """/v1/responses: the local relay speaks Messages only, so a flagged request is always answered in the cloud, and
    the notice is its own leading `message` output item."""

    def request(self, *items, stream=False):
        from tests.test_provider_responses import request
        return request(*items, tools=(), stream=stream)

    def test_json_and_sse(self):
        from tests.test_provider_responses import user
        got = self.post(self.request(user("summarise the notes")), path="/v1/responses",
                        headers={"x-cortados-local": "sensitive"})
        out = got["output"]
        self.assertEqual([i["type"] for i in out], ["message", "message"])
        self.assertEqual(out[0]["content"][0]["text"], notice("flag", "responses_wire"))
        self.assertTrue(out[1]["content"][0]["text"].startswith("echo: "))
        self.assertRecorded("responses_wire")
        raw = self.post(self.request(user("summarise the notes"), stream=True), path="/v1/responses",
                        headers={"x-cortados-local": "sensitive"})
        evs = sse_events(raw)
        done = [e for e in evs if e["type"] == "response.output_item.done"]
        self.assertEqual([e["output_index"] for e in done], [0, 1])
        self.assertEqual(done[0]["item"]["content"][0]["text"], notice("flag", "responses_wire"))
        self.assertEqual([e["sequence_number"] for e in evs], list(range(len(evs))))   # created is 0
        final = evs[-1]["response"]["output"]
        self.assertEqual(final[0]["content"][0]["text"], notice("flag", "responses_wire"))

    def test_continuation_with_the_notice_item_in_the_input(self):
        from tests.test_provider_responses import request, user
        first = request(user("summarise the notes"))
        got = self.post(first, path="/v1/responses", headers={"x-cortados-local": "sensitive"})
        (s,) = self.reg.sessions.values()
        nxt = request(*first["input"], *got["output"], user("and the second one"))
        again = self.post(nxt, path="/v1/responses")
        self.assertEqual(len(again["output"]), 1)
        self.assertIn("and the second one", again["output"][0]["content"][0]["text"])
        self.assertEqual(list(self.reg.sessions.values()), [s])
        parsed = responses_wire.parse_request(nxt)
        self.assertEqual(parsed.history, [anthropic_wire.reply_fingerprint(
            [{"type": "text", "text": got["output"][1]["content"][0]["text"]}])])


class RouteRowRecordTests(unittest.TestCase):
    """core.log_sensitive_cloud on a throwaway router.db, read back by the Tasks and Live views as categories."""

    def setUp(self):
        self._state = core.STATE
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-scw-"))
        self.addCleanup(setattr, core, "STATE", self._state)
        r = {"band": "R3", "chosen": {"lane": "claude", "provider": "claude-cli", "model": CLOUD}, "subagents": 0,
             "verifier": False, "hard_rules": []}
        dec = {"engine": "s1", "engine_ms": 1, "degraded": False}
        for rid in ("r-warned", "r-plain"):
            self.assertTrue(core.log_route(rid, {"session_id": "conv-1", "request": f"the {SECRET_TERM}"}, dec, r))

    def test_written_and_published_as_categories(self):
        self.assertTrue(core.log_sensitive_cloud("r-warned", sensitive_notice.record(
            {"detector": "keyword", "reason": "too_large"}, sensitive_notice.SHOWN)))
        with sqlite3.connect(core.STATE / "router.db") as db:
            rows = dict(db.execute("SELECT turn_id, sensitive_cloud FROM routes").fetchall())
        self.assertEqual(json.loads(rows["r-warned"]), {"detector": "keyword", "notice": "shown", "reason": "too_large"})
        self.assertIsNone(rows["r-plain"])
        view = tasks.collect(core.STATE)
        by_id = {t["task_id"]: t for t in view["tasks"]}
        self.assertEqual(by_id["r-warned"]["sensitive_cloud"],
                         {"detector": "keyword", "reason": "too_large", "notice": "shown"})
        self.assertIsNone(by_id["r-plain"]["sensitive_cloud"])
        self.assertEqual(view["totals"]["sensitive_cloud"], 1)
        self.assertNotIn("zebra", json.dumps(view))
        calls = live.routed_section(view, time.time())["calls"]
        self.assertEqual({c["task_id"]: c["sensitive_cloud"] for c in calls}["r-warned"]["reason"], "too_large")

    def test_a_foreign_value_is_not_published(self):
        with sqlite3.connect(core.STATE / "router.db") as db:
            db.execute("UPDATE routes SET sensitive_cloud=? WHERE turn_id='r-warned'",
                       (json.dumps({"detector": SECRET_TERM, "reason": "too_large", "notice": "shown"}),))
        view = tasks.collect(core.STATE)
        self.assertIsNone({t["task_id"]: t for t in view["tasks"]}["r-warned"]["sensitive_cloud"])
        self.assertEqual(view["totals"]["sensitive_cloud"], 0)
        self.assertNotIn("zebra", json.dumps(view))

    def test_an_old_db_without_the_column_reads_as_none(self):
        old = Path(tempfile.mkdtemp(prefix="cortados-scw-old-"))
        with sqlite3.connect(old / "router.db") as db:               # the base table only: no later columns
            db.executescript(core.DDL)
            db.execute("INSERT INTO routes (turn_id, ts, session_id, band) VALUES ('r-old', 1.0, 'conv-1', 'R3')")
        view = tasks.collect(old)
        self.assertEqual(view["errors"], [])
        self.assertTrue(all(t["sensitive_cloud"] is None for t in view["tasks"]))


if __name__ == "__main__":
    unittest.main()
