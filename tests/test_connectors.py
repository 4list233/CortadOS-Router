"""Offline manifest/policy contract tests; no provider, CLI or live state."""
import copy
import itertools
import json
from pathlib import Path
import unittest

from cortados_router.connectors import (
    ManifestError, RunState, call_hash, decide, mark_external_read,
    suggest_class, validate_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
TIERS = ("owner-interactive", "owner-unattended", "bot", "outcome-loop")


def manifest():
    return {"version": 1, "servers": [
        {"name": "outside", "kind": "claude_ai", "tools": [
            {"name": "read_mail", "class": "read"},
            {"name": "draft_mail", "class": "write"},
            {"name": "send_mail", "class": "outward"},
        ]},
        {"name": "sandbox", "kind": "mcp_stdio", "tools": [
            {"name": "read_mail", "class": "read"},
            {"name": "draft_mail", "class": "write"},
            {"name": "send_mail", "class": "outward"},
        ]},
    ]}


class ManifestTests(unittest.TestCase):
    def test_example_validates_and_covers_kinds(self):
        data = json.loads((ROOT / "tables/connectors.example.json").read_text())
        result = validate_manifest(data)
        self.assertEqual(data, result)
        self.assertEqual({s["kind"] for s in data["servers"]},
                         {"claude_ai", "mcp_stdio", "mcp_http"})

    def test_non_string_schema_keys_have_clear_errors(self):
        for data in ({1: "bad", "other": 1}, {"version": 1, "servers": [
                {"name": "x", "kind": "mcp_stdio", "tools": [], 1: "bad"}]}):
            with self.subTest(data=data), self.assertRaisesRegex(ManifestError, "keys must be strings"):
                validate_manifest(data)

    def test_non_object_entries_have_clear_errors(self):
        for entry in (None, [], "server", 7):
            with self.subTest(entry=entry), self.assertRaisesRegex(ManifestError, "expected object"):
                validate_manifest({"version": 1, "servers": [entry]})

    def test_copy_is_independent(self):
        data = manifest()
        result = validate_manifest(data)
        result["servers"][0]["tools"][0]["class"] = "outward"
        self.assertEqual(data["servers"][0]["tools"][0]["class"], "read")

    def test_root_schema(self):
        for data in (None, [], {}, {"version": True, "servers": []},
                     {"version": 2, "servers": []}, {"version": 1, "servers": {}},
                     {"version": 1, "servers": [], "extra": 1}):
            with self.subTest(data=data), self.assertRaisesRegex(ManifestError, "manifest"):
                validate_manifest(data)
        self.assertEqual(validate_manifest({"version": 1, "servers": []})["servers"], [])

    def test_server_schema(self):
        for change in (
            {"name": ""}, {"name": " spaced "}, {"name": 3}, {"kind": "shell"},
            {"kind": []}, {"tools": {}}, {"tools": None}, {"unknown": True},
        ):
            data = manifest()
            data["servers"][0].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ManifestError, r"servers\[0\]"):
                validate_manifest(data)
        for field in ("name", "kind", "tools"):
            data = manifest()
            del data["servers"][0][field]
            with self.subTest(missing=field), self.assertRaises(ManifestError):
                validate_manifest(data)

    def test_tool_schema(self):
        for tool in (None, {}, {"name": "x"}, {"name": "", "class": "read"},
                     {"name": "x", "class": "execute"}, {"name": "x", "class": []},
                     {"name": "x", "class": "read", "approval": True}):
            data = manifest()
            data["servers"][0]["tools"] = [tool]
            with self.subTest(tool=tool), self.assertRaisesRegex(ManifestError, "tools"):
                validate_manifest(data)

    def test_duplicate_names(self):
        for duplicate_server in (True, False):
            data = manifest()
            if duplicate_server:
                data["servers"].append(copy.deepcopy(data["servers"][0]))
            else:
                data["servers"][0]["tools"].append({"name": "read_mail", "class": "outward"})
            with self.subTest(server=duplicate_server), self.assertRaisesRegex(ManifestError, "duplicate"):
                validate_manifest(data)
        validate_manifest(manifest())  # Same tool name on different servers is legal.

    def test_urls(self):
        for url in ("https://example.invalid/mcp", "http://127.0.0.1:1234/mcp"):
            validate_manifest({"version": 1, "servers": [
                {"name": "x", "kind": "mcp_http", "url": url, "tools": []}]})
        for url in (None, "", "file:///tmp/mcp", "https://", "https://user:secret@host/",
                    "https://host/?token=x", "https://host/#frag", "https://host:bad/",
                    "https://host/ space", "https://[broken"):
            with self.subTest(url=url), self.assertRaisesRegex(ManifestError, "url"):
                validate_manifest({"version": 1, "servers": [
                    {"name": "x", "kind": "mcp_http", "url": url, "tools": []}]})
        with self.assertRaisesRegex(ManifestError, "required"):
            validate_manifest({"version": 1, "servers": [
                {"name": "x", "kind": "mcp_http", "tools": []}]})

    def test_suggestions(self):
        groups = {
            "outward": ("send", "post_message", "publish-page", "deleteItem", "share_doc",
                        "pay_invoice", "invite_user", "deploy", "transfer", "mystery",
                        "get_and_delete", "mcp__slack__post_message", "readAndSend"),
            "read": ("get_item", "list", "search_messages", "readDoc", "query-db", "fetch"),
            "write": ("create_item", "update_doc", "edit", "write_file", "upload", "draft_mail"),
        }
        for expected, names in groups.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(suggest_class(name), expected)
        self.assertEqual(suggest_class(None), "outward")


class HashTests(unittest.TestCase):
    def test_canonical_order_and_nested_json(self):
        a = {"z": [{"b": 2, "a": "é"}], "x": None}
        b = {"x": None, "z": [{"a": "é", "b": 2}]}
        self.assertEqual(call_hash("s", "t", a), call_hash("s", "t", b))
        self.assertEqual(len(call_hash("s", "t", a)), 64)

    def test_exact_call_binding(self):
        baseline = call_hash("s", "t", {"x": [1, 2]})
        for server, tool, args in (
            ("other", "t", {"x": [1, 2]}), ("s", "other", {"x": [1, 2]}),
            ("s", "t", {"x": [2, 1]}), ("s", "t", {"x": [1, 3]}),
            ("s", "t", {"x": [True, 2]}), ("s", "t", {"x": [1, 2], "extra": None}),
        ):
            with self.subTest(server=server, tool=tool, args=args):
                self.assertNotEqual(baseline, call_hash(server, tool, args))

    def test_reject_non_json(self):
        for args in ([], None, {"x": float("nan")}, {"x": float("inf")},
                     {1: "value"}, {"x": (1, 2)}, {"x": {1, 2}}, {"x": object()}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                call_hash("s", "t", args)

    def test_reject_bad_names(self):
        for server, tool in (("", "x"), ("x", None), (" s", "x")):
            with self.subTest(server=server, tool=tool), self.assertRaises(ValueError):
                call_hash(server, tool, {})


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.state = RunState(manifest(), test_targets=("sandbox",))
        self.args = {"message": "hello", "recipient": "test@example.invalid"}

    def approval(self, server, tool, args=None):
        return {call_hash(server, tool, self.args if args is None else args): {"approved": True}}

    def test_tier_class_taint_approval_matrix(self):
        # 4 tiers x 2 servers x 3 classes x 2 taints x 2 approval states.
        for tier, server, tool, taint, approved in itertools.product(
                TIERS, ("outside", "sandbox"),
                ("read_mail", "draft_mail", "send_mail"), (False, True), (False, True)):
            state = RunState(manifest(), taint, ("sandbox",))
            approvals = self.approval(server, tool) if approved else {}
            expected = "allow"
            if tier == "outcome-loop" and (server != "sandbox" or tool == "send_mail"):
                expected = "deny"
            elif tier == "bot" and tool != "read_mail":
                expected = "deny"
            elif tool == "send_mail":
                expected = "allow" if approved else "ask"
            with self.subTest(tier=tier, server=server, tool=tool, taint=taint, approved=approved):
                decision, reason = decide(tier, server, tool, self.args, state, approvals)
                self.assertEqual(decision, expected)
                self.assertTrue(reason)

    def test_unknown_tools_never_get_suggested_grants(self):
        for tier, tool in itertools.product(TIERS, ("get_messages", "draft_note", "mystery")):
            expected = "ask" if tier.startswith("owner-") else "deny"
            with self.subTest(tier=tier, tool=tool):
                self.assertEqual(decide(tier, "sandbox", tool, {}, self.state, {})[0], expected)

    def test_unknown_server_is_outward(self):
        self.assertEqual(decide("owner-interactive", "unknown", "read_mail", {},
                                self.state, {})[0], "ask")
        self.assertEqual(decide("owner-interactive", "unknown", "read_mail", {},
                                self.state, self.approval("unknown", "read_mail", {}))[0], "allow")

    def test_unknown_can_be_explicitly_approved_not_by_outcome_loop(self):
        for tier in TIERS:
            result = decide(tier, "sandbox", "get_unknown", self.args, self.state,
                            self.approval("sandbox", "get_unknown"))[0]
            self.assertEqual(result, "allow" if tier.startswith("owner-") else "deny")

    def test_approval_must_match_all_call_fields(self):
        approval = self.approval("outside", "send_mail")
        for server, tool, args in (
                ("sandbox", "send_mail", self.args),
                ("outside", "post_mail", self.args),
                ("outside", "send_mail", {**self.args, "message": "changed"})):
            with self.subTest(server=server, tool=tool, args=args):
                self.assertEqual(decide("owner-interactive", server, tool, args,
                                        self.state, approval)[0], "ask")

    def test_approval_is_explicit_not_truthy_or_wildcard(self):
        digest = call_hash("outside", "send_mail", self.args)
        for approvals in (None, [], True, {"*": {"approved": True}},
                          {digest: True}, {digest: {"approved": 1}},
                          {digest: {"approved": "yes"}}, {digest: {"approved": False}},
                          {digest: {}}, {"send_mail": {"approved": True}}):
            with self.subTest(approvals=approvals):
                self.assertEqual(decide("owner-interactive", "outside", "send_mail",
                                        self.args, self.state, approvals)[0], "ask")

    def test_taint_is_monotonic_and_pure(self):
        tainted = mark_external_read(self.state)
        self.assertFalse(self.state.tainted)
        self.assertTrue(tainted.tainted)
        self.assertTrue(mark_external_read(tainted).tainted)
        action, reason = decide("owner-unattended", "outside", "send_mail", {},
                                tainted, {})
        self.assertEqual(action, "ask")
        self.assertIn("taint", reason)

    def test_decide_does_not_mutate_inputs(self):
        approval = self.approval("outside", "send_mail")
        snapshot = copy.deepcopy((self.state, self.args, approval))
        decide("owner-interactive", "outside", "send_mail", self.args, self.state, approval)
        self.assertEqual((self.state, self.args, approval), snapshot)

    def test_outcome_defaults_to_no_test_targets(self):
        state = RunState(manifest())
        for tool in ("read_mail", "draft_mail", "send_mail"):
            self.assertEqual(decide("outcome-loop", "outside", tool, {}, state, {})[0], "deny")

    def test_invalid_inputs_fail_closed(self):
        invalid = (
            ("invented", "outside", "read_mail", {}, self.state),
            ([], "outside", "read_mail", {}, self.state),
            ("bot", "outside", "read_mail", {}, {}),
            ("bot", "outside", "read_mail", {}, RunState({})),
            ("bot", "outside", "read_mail", {}, RunState(manifest(), "false")),
            ("bot", "outside", "read_mail", {}, RunState(manifest(), test_targets="outside")),
            ("bot", "outside", "read_mail", {}, RunState(manifest(), test_targets=(None,))),
            ("bot", "outside", "read_mail", {"x": float("nan")}, self.state),
            ("bot", "outside", "", {}, self.state),
            ("bot", [], "read_mail", {}, self.state),
        )
        for tier, server, tool, args, state in invalid:
            with self.subTest(tier=tier, server=server, tool=tool, state=state):
                self.assertEqual(decide(tier, server, tool, args, state, {})[0], "deny")

    def test_class_is_exact_manifest_not_name_or_arguments(self):
        data = manifest()
        data["servers"][0]["tools"][0]["class"] = "outward"
        self.assertEqual(decide("owner-interactive", "outside", "read_mail",
                                {"class": "read", "approved": True},
                                RunState(data), {})[0], "ask")
        self.assertEqual(decide("bot", "OUTSIDE", "read_mail", {}, self.state, {})[0], "deny")


if __name__ == "__main__":
    unittest.main()
