"""The antigravity (agy) lane, #144 PR A: honest probe failures, deny-all, isolation, inventory pools, tables, views.

Fake agy CLIs only (small scripts run in a real PTY by scarcity.tui_capture); no vendor CLI, no network, no account.
State is a throwaway directory; the module's STATE / CACHE / HISTORY / PROBE_DIR are patched to it.
"""
from __future__ import annotations
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import agy_isolation as iso
from cortados_router import scarcity as sc
from cortados_router import unitcost
from cortados_router.provider import live, usage

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "agy-usage-provisional.txt"
# The /usage answer as agy 1.5.16's 30-line pager holds it: GEMINI MODELS and CLAUDE AND GPT MODELS, each with a weekly
# window, a "Refreshes in"/"Quota available" line and a five-hour window. The pager shows 17 lines at a time with a
# "(1–17 of 30 lines)" footer, so its first page (lines 1-17) holds Gemini's two windows and only the Claude/GPT weekly
# one: the case where two "Limit Remaining" lines used to count as a full reading (#144 review round 1). RECONSTRUCTED
# from the owner's description of the screen (layout, groups, footer, keys), not a byte copy: the owner's paste was not
# available to the fix round; replace it with the scrubbed paste when it arrives (docs/AGY-LANE.md).
PAGER_FIXTURE = ROOT / "tests" / "fixtures" / "agy-usage-pager.txt"
PAGE_H = 17


def pager_view(lines: list[str], top: int) -> str:
    """One painted view of the pager: clear, the visible lines, the footer agy prints under them."""
    n = len(lines)
    return ("\x1b[2J\x1b[H" + "\n".join(lines[top:top + PAGE_H])
            + f"\n({top + 1}\u2013{min(top + PAGE_H, n)} of {n} lines)  \u2191/\u2193 scroll \u00b7 pgdown page \u00b7 ctrl+end Bottom\n")

# A fake agy: dumps the NAMES of its environment, records every byte typed into it, prints its boot screen, and answers
# "/usage<Enter>" with the usage screen (MODE "usage") or a trust prompt's Enter with the screen after it.
FAKE = r'''#!{py}
import os, tty
LOG = {log!r}
SCREENS = {screens!a}    # ASCII escapes: Python 3.9's tokenizer can misread a long line of multibyte text
with open(LOG + ".env", "w") as f:
    f.write("\n".join(sorted(os.environ)))
try:
    tty.setraw(0)
except Exception:
    pass
def out(s):
    os.write(1, s.replace("\n", "\r\n").encode())
H = 17
def view(top):
    L = SCREENS["pager"]
    return ("\x1b[2J\x1b[H" + "\n".join(L[top:top + H])
            + "\n(%d\u2013%d of %d lines)  \u2191/\u2193 scroll \u00b7 pgdown page \u00b7 ctrl+end Bottom\n" % (top + 1, min(top + H, len(L)), len(L)))
top = 0
out(SCREENS["boot"])
got = b""
while True:
    try:
        d = os.read(0, 1024)
    except OSError:
        break
    if not d:
        break
    got += d
    with open(LOG + ".keys", "wb") as f:
        f.write(got)
    if "after_trust" in SCREENS and got == b"\r":
        out(SCREENS["after_trust"])
    if "usage" in SCREENS and got.endswith(b"/usage\r"):
        out(SCREENS["usage"])
    if "on_command_text" in SCREENS and got.endswith(b"/usage"):
        out(SCREENS["on_command_text"])
    if "pager" in SCREENS:
        if got.endswith(b"/usage\r"):
            top = 0
            out(view(top))
        elif d == b"\x1b[B" and not SCREENS.get("frozen"):
            top = min(top + 1, max(len(SCREENS["pager"]) - H, 0))
            out(view(top))
'''

BANNER = "Antigravity CLI 1.5.16\n(Google AI Pro) Gemini 4 Neon (High)\nResume with -c (or command below): agy --conversation=00000000-0000-0000-0000-000000000000\n> "
LOGGED_OUT = "Antigravity CLI 1.5.16\nSign in with Google to continue.\nOpen https://accounts.google.com/o/oauth2/auth?x=1 in your browser\n"
FIRST_RUN = "Welcome to Antigravity CLI\nColor scheme: Dark / Solarized Dark / Solarized Light / Terminal default\n"
TRUST = "Do you trust the contents of this folder?\nTrust this workspace [Y/n]\n"


def confirmed_tier():
    """As if the owner's capture had confirmed the tier line (TIER_PATTERN_CONFIRMED is False until then)."""
    return mock.patch.object(iso, "TIER_PATTERN_CONFIRMED", True)


class AgyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        for name, value in (("STATE", self.state), ("CACHE", self.state / "scarcity.json"),
                            ("HISTORY", self.state / "scarcity-history.jsonl"), ("PROBE_DIR", self.state / "usage-probe")):
            p = mock.patch.object(sc, name, value)
            p.start(); self.addCleanup(p.stop)
        # the process env a real provider might carry: every one of these must stay out of agy
        self.environ = {"HOME": str(self.home), "USER": "tester", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
                        "PATH": "/somewhere/else", "GEMINI_API_KEY": "gk-secret", "GOOGLE_GEMINI_BASE_URL": "http://x.invalid",
                        "AGY_ADC_AUTH": "true", "OPENAI_API_KEY": "sk-o", "ANTHROPIC_API_KEY": "sk-a",
                        "CORTADOS_PROVIDER_TOKEN": "tok", "SSH_AUTH_SOCK": "/tmp/agent", "GITHUB_TOKEN": "gh"}

        # agy 1.5.16's real shape (placeholder paths); no config.json / mcp_config.json is allowed too
        self.write_settings({"trustedWorkspaces": ["/Users/example/projects/alpha", "/Users/example/projects/beta"]})

    def write_settings(self, data) -> None:
        p = self.home / iso.SETTINGS_RELPATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))

    def fake(self, screens: dict) -> tuple[str, Path]:
        log = self.tmp / "agy-log"
        p = self.tmp / "bin" / "agy"
        p.parent.mkdir(exist_ok=True)
        p.write_text(FAKE.format(py=sys.executable, log=str(log), screens=screens))
        p.chmod(0o755)
        return str(p), log

    def env(self, binary: str | None):
        e = dict(self.environ)
        if binary:
            e["CORTADOS_AGY_BIN"] = binary
        return mock.patch.dict(os.environ, e, clear=True)

    @staticmethod
    def keys(log: Path) -> bytes:
        p = Path(str(log) + ".keys")
        return p.read_bytes() if p.exists() else b""


class ProbeHonesty(AgyBase):
    def test_not_configured_is_a_state_not_a_failure_and_opens_no_pty(self):
        old = {"error": "RuntimeError: no quota groups on the /usage screen: ", "error_kind": "no-quota-groups",
               "tried_at": 1.0, "windows": [{"window": "week (gemini)", "pool": "gemini", "used_pct": 1.0}]}
        sc.CACHE.write_text(json.dumps({"fetched_at": 0, "lanes": {"antigravity": old}}))
        with self.env(None), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")), \
                mock.patch.object(sc, "_learn_unit_costs"):
            sc._sample_locked(only=["antigravity"])
        rec = json.loads(sc.CACHE.read_text())["lanes"]["antigravity"]
        self.assertEqual(rec["state"], "not-configured")
        self.assertIsNone(rec["error"]); self.assertEqual(rec["windows"], []); self.assertIsNone(rec["fetched_at"])
        self.assertEqual(sc.lane_state(rec), "not configured")
        self.assertEqual(sc.reading_freshness("antigravity", rec), (False, False))       # never fresh, never FAILED
        hist = [json.loads(l) for l in sc.HISTORY.read_text().splitlines()]
        self.assertNotIn("antigravity", hist[-1]["lanes"])                                # not a reading
        entry = usage._one_lane("antigravity", rec, time.time(), sc.reading_freshness)
        self.assertEqual((entry["state"], entry["error"], entry["left_pct"]), ("not configured", None, None))

    def test_binary_missing_records_searched_and_never_forks(self):
        gone = str(self.tmp / "nowhere" / "agy")
        with self.env(gone), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises(iso.ProbeBinaryMissing) as cm:
                sc._antigravity()
        rec = sc._failed_record("antigravity", None, cm.exception)
        self.assertEqual(rec["error_kind"], "binary-missing")
        self.assertEqual(rec["searched"], [gone])
        self.assertEqual(sc.lane_state(rec), "not installed")
        entry = usage._one_lane("antigravity", rec, time.time(), sc.reading_freshness)
        self.assertEqual((entry["state"], entry["error"], entry["stale"]), ("not installed", "binary-missing", True))
        subs = live.subscriptions_section({"lanes": [entry]}, time.time(), live.lane_enabled)
        self.assertEqual(subs["lanes"][0]["state"], "not installed")
        self.assertIsNone(subs["lowest"])

    def test_relative_binary_is_never_resolved_on_path(self):
        with self.env("agy"), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaisesRegex(iso.ProbeBinaryMissing, "not absolute"):
                sc._antigravity()

    def test_exec_failure_raises_instead_of_an_empty_screen(self):
        bad = self.tmp / "agy-bad"
        bad.write_text("#!/nonexistent/interpreter\n")
        bad.chmod(0o755)
        with self.env(str(bad)):
            with self.assertRaises(sc.ProbeExecFailed) as cm:
                sc._antigravity()
        self.assertEqual(sc._failed_record("antigravity", None, cm.exception)["error_kind"], "exec-failed")

    def test_generic_capture_exec_failure_raises(self):
        with self.assertRaises(sc.ProbeExecFailed):
            sc.tui_capture([str(self.tmp / "missing-cli")], "/usage", "never", {"PATH": "/usr/bin:/bin"}, boot_s=3)

    def test_cli_that_exits_at_once_without_output_is_exec_failed(self):
        quiet = self.tmp / "agy-quiet"
        quiet.write_text("#!/bin/sh\nexit 0\n")
        quiet.chmod(0o755)
        with self.env(str(quiet)):
            with self.assertRaises(sc.ProbeExecFailed):
                sc._antigravity()

    def test_logged_out_screen_is_refused_and_nothing_is_typed(self):
        binary, log = self.fake({"boot": LOGGED_OUT})
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual(cm.exception.kind, "not-logged-in")
        self.assertEqual(self.keys(log), b"")
        rec = sc._failed_record("antigravity", None, cm.exception)
        self.assertEqual((rec["error_kind"], sc.lane_state(rec)), ("not-logged-in", "not logged in"))

    def test_first_run_prompt_is_refused_and_nothing_is_typed(self):
        binary, log = self.fake({"boot": FIRST_RUN})
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual(cm.exception.kind, "first-run-pending")
        self.assertEqual(self.keys(log), b"")

    def test_only_the_trust_prompt_is_answered(self):
        binary, log = self.fake({"boot": TRUST, "after_trust": LOGGED_OUT})
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual(cm.exception.kind, "not-logged-in")
        self.assertEqual(self.keys(log), b"\r")       # one Enter for the trust prompt; /usage never typed

    def test_usage_screen_is_read_through_the_isolated_env(self):
        binary, log = self.fake({"boot": BANNER, "usage": "Plan: Google AI Pro\n" + FIXTURE.read_text()})
        with self.env(binary), confirmed_tier():
            rec = sc._antigravity()
        self.assertEqual(rec["tier"], "AI Pro")
        self.assertEqual(rec["binary"], binary)
        self.assertEqual(sorted((w["window"], w["pool"]) for w in rec["windows"]),
                         [("5-hour (claude-gpt)", "claude-gpt"), ("5-hour (gemini)", "gemini"),
                          ("week (claude-gpt)", "claude-gpt"), ("week (gemini)", "gemini")])
        self.assertTrue(self.keys(log).endswith(b"/usage\r"))
        names = Path(str(log) + ".env").read_text().split("\n")
        for n in names:
            self.assertTrue(iso.allowed(n) or n in ("TERM", "PATH") or n.startswith("__CF") or n.startswith("__PYVENV"), n)
        for n in ("GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "AGY_ADC_AUTH", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                  "CORTADOS_PROVIDER_TOKEN", "CORTADOS_AGY_BIN", "SSH_AUTH_SOCK", "GITHUB_TOKEN"):
            self.assertNotIn(n, names)
        fin = sc.finish("antigravity", rec)
        self.assertIn("gemini", fin["pools"]); self.assertIsNotNone(fin["scarcity"])

    def test_forbidden_variable_in_env_file_is_a_hard_failure(self):
        binary, log = self.fake({"boot": BANNER})
        (sc.STATE / "env").write_text("AGY_ADC_AUTH=true\nCLAUDE_CONFIG_DIR=/x\n")
        with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises(iso.ForbiddenEnv) as cm:
                sc._antigravity()
        self.assertIn("AGY_ADC_AUTH", str(cm.exception)); self.assertNotIn("true", str(cm.exception))
        self.assertEqual(sc._failed_record("antigravity", None, cm.exception)["error_kind"], "forbidden-env")

    def test_unsafe_settings_refuse_before_any_start(self):
        binary, _ = self.fake({"boot": BANNER})
        for bad in ({"tools": {"permissionMode": "always-proceed"}}, [],
                    {"security": {"auth": {"selectedType": "api-key"}}}, {"trustedWorkspaces": "x", "mcpServers": {}}):
            with self.subTest(bad=bad):
                self.write_settings(bad)
                with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
                    with self.assertRaises(iso.SettingsRefused):
                        sc._antigravity()

    def test_missing_settings_is_first_run_pending_before_any_start(self):
        binary, _ = self.fake({"boot": BANNER})
        (self.home / iso.SETTINGS_RELPATH).unlink()
        with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises(iso.FirstRunPending) as cm:
                sc._antigravity()
        rec = sc._failed_record("antigravity", None, cm.exception)
        self.assertEqual((rec["error_kind"], sc.lane_state(rec)), ("first-run-pending", "first-run setup pending"))

    def test_mcp_server_in_mcp_config_refuses_before_any_start(self):
        binary, _ = self.fake({"boot": BANNER})
        p = self.home / iso.MCP_CONFIG_RELPATH
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"mcpServers": {"x": {"command": "y"}}}))
        with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaisesRegex(iso.SettingsRefused, "MCP servers"):
                sc._antigravity()

    # ---- #144 round 2: agy stores no auth mode; the subscription is proven from its screen (provisional: refusing)
    def test_full_reading_is_refused_while_the_tier_pattern_is_provisional(self):
        binary, _ = self.fake({"boot": BANNER, "usage": "Plan: Google AI Pro\n" + FIXTURE.read_text()})
        with self.env(binary):
            with self.assertRaisesRegex(iso.SubscriptionUnproven, "PROVISIONAL") as cm:
                sc._antigravity()
        self.assertEqual(sc._failed_record("antigravity", None, cm.exception)["error_kind"], "subscription-unproven")

    def test_api_key_screen_is_refused_even_with_a_tier(self):
        binary, _ = self.fake({"boot": BANNER, "usage": "Plan: Google AI Pro \u00b7 API key\n" + FIXTURE.read_text()})
        with self.env(binary), confirmed_tier():
            with self.assertRaisesRegex(iso.SubscriptionUnproven, "non-subscription"):
                sc._antigravity()

    def test_no_tier_on_screen_is_refused(self):
        binary, _ = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})         # the tier is read from /usage
        with self.env(binary), confirmed_tier():
            with self.assertRaisesRegex(iso.SubscriptionUnproven, "no personal"):
                sc._antigravity()

    def test_account_email_never_reaches_an_error(self):
        binary, _ = self.fake({"boot": BANNER, "usage": "Models & Quota\nAccount: owner.private@example.com\nnothing here\n"
                                                         "Limit Remaining x Limit Remaining y\n"})
        with self.env(binary):
            with self.assertRaises(Exception) as cm:
                sc._antigravity()
        rec = sc._failed_record("antigravity", None, cm.exception)
        self.assertNotIn("owner.private", rec["error"])

    # ---- #144 review round 1, finding 3: a login screen that appears only after /usage gets no more input
    def test_login_screen_after_usage_stops_typing_at_once_and_says_what_was_typed(self):
        binary, log = self.fake({"boot": BANNER, "usage": LOGGED_OUT})
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual(cm.exception.kind, "not-logged-in")
        self.assertEqual(self.keys(log), b"/usage\r")              # no Esc, no second /usage (the old retry typed both)
        self.assertEqual(cm.exception.typed, ["'/usage'", "Enter"])
        self.assertNotIn("nothing was typed", str(cm.exception))
        self.assertIn("typed before it appeared: '/usage', Enter", str(cm.exception))

    def test_refusal_is_checked_before_every_write(self):
        # the sign-in screen is painted as soon as "/usage" arrives, before the probe's Enter: the Enter is never sent
        binary, log = self.fake({"boot": BANNER, "on_command_text": LOGGED_OUT})
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual(self.keys(log), b"/usage")
        self.assertEqual(cm.exception.typed, ["'/usage'"])

    # ---- #144 review round 1, finding 4: the /usage pager is read to its end, a partial reading is an error
    def test_first_page_alone_is_not_a_full_reading(self):
        lines = PAGER_FIXTURE.read_text().splitlines()
        first = pager_view(lines, 0)
        self.assertIn("(1\u201317 of 30 lines)", first)
        g = sc.parse_agy_usage(first)
        self.assertEqual([w["window"] for w in g["gemini models"]], ["week", "5-hour"])
        self.assertEqual([w["window"] for w in g["claude and gpt models"]], ["week"])
        gaps = sc.agy_usage_gaps(first)
        self.assertIn("claude and gpt models: no 5-hour window", gaps)
        self.assertTrue(any(x.startswith("pager not read to the end") for x in gaps), gaps)
        self.assertEqual(sc._agy_next_page(first), (b"\x1b[B", "Down"))

    def test_pager_is_paged_with_navigation_keys_until_every_group_is_read(self):
        lines = PAGER_FIXTURE.read_text().splitlines()
        self.assertEqual(len(lines), 30)
        binary, log = self.fake({"boot": BANNER, "pager": lines})
        with self.env(binary), confirmed_tier():
            rec = sc._antigravity()
        keys = self.keys(log)
        self.assertTrue(keys.startswith(b"/usage\r"))
        self.assertEqual(set(keys[len(b"/usage\r"):].split(b"\x1b[B")), {b""})   # only Down after /usage: no text
        self.assertGreaterEqual(keys.count(b"\x1b[B"), 1)
        got = {(w["window"], w["pool"]): w["used_pct"] for w in rec["windows"]}
        self.assertEqual(got, {("week (gemini)", "gemini"): 12.5, ("5-hour (gemini)", "gemini"): 36.0,
                               ("week (claude-gpt)", "claude-gpt"): 0.0, ("5-hour (claude-gpt)", "claude-gpt"): 42.0})

    def test_pager_that_does_not_move_is_a_partial_reading_error(self):
        lines = PAGER_FIXTURE.read_text().splitlines()
        binary, log = self.fake({"boot": BANNER, "pager": lines, "frozen": True})
        with self.env(binary):
            with self.assertRaises(sc.ProbePartialReading) as cm:
                sc._antigravity()
        self.assertIn("claude and gpt models: no 5-hour window", str(cm.exception))
        rec = sc._failed_record("antigravity", None, cm.exception)
        self.assertEqual((rec["error_kind"], sc.lane_state(rec)), ("partial-reading", "quota screen read only in part"))

    def test_a_missing_group_is_a_partial_reading(self):
        only_gemini = "\n".join(PAGER_FIXTURE.read_text().splitlines()[:12])
        self.assertEqual(sc.agy_usage_gaps(only_gemini), ["no claude-gpt group"])


class Isolation(unittest.TestCase):
    def test_env_allowlist(self):
        env = iso.child_env({"HOME": "/h", "USER": "u", "LANG": "C", "LC_ALL": "C", "TMPDIR": "/t", "PATH": "/evil",
                             "GEMINI_API_KEY": "k", "GOOGLE_GEMINI_BASE_URL": "u", "AGY_ADC_AUTH": "true",
                             "OPENAI_API_KEY": "o", "ANTHROPIC_BASE_URL": "a", "CLAUDE_CODE_OAUTH_TOKEN": "c",
                             "CODEX_HOME": "/c", "CORTADOS_AGY_BIN": "/b", "SHELL": "/bin/zsh", "NODE_OPTIONS": "--x"})
        self.assertEqual(set(env), {"HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "PATH"})
        self.assertEqual(env["PATH"], iso.FIXED_PATH.format(home="/h"))

    def test_env_file_google_family_fails_other_vendors_are_dropped(self):
        for name in ("GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "GOOGLE_APPLICATION_CREDENTIALS", "AGY_ADC_AUTH", "CLOUDSDK_CONFIG"):
            with self.subTest(name=name), self.assertRaises(iso.ForbiddenEnv):
                iso.child_env({"HOME": "/h"}, {name: "x"})
        env = iso.child_env({"HOME": "/h"}, {"CLAUDE_CONFIG_DIR": "/x", "OPENAI_API_KEY": "o", "LANG": "en"})
        self.assertEqual(set(env), {"HOME", "PATH", "LANG"})

    def test_resolve_binary(self):
        with self.assertRaises(iso.LaneNotConfigured):
            iso.resolve_binary(None)
        with self.assertRaises(iso.LaneNotConfigured):
            iso.resolve_binary("  ")
        with self.assertRaises(iso.ProbeBinaryMissing) as cm:
            iso.resolve_binary("/definitely/not/here/agy")
        self.assertEqual(cm.exception.searched, ["/definitely/not/here/agy"])
        self.assertEqual(iso.resolve_binary(sys.executable), sys.executable)


class ParserAndPools(unittest.TestCase):
    def test_provisional_fixture(self):
        g = sc.parse_agy_usage(FIXTURE.read_text())
        self.assertEqual(sorted(g), ["gemini models", "third-party models"])
        week, five = g["gemini models"]
        self.assertEqual((week["window"], week["used_pct"]), ("week", 12.5))
        self.assertEqual((five["window"], five["used_pct"]), ("5-hour", 36.0))
        self.assertAlmostEqual(week["resets_in_h"], 102.2, places=1)
        self.assertEqual(sc._agy_pool_of_group("third-party models"), "claude-gpt")
        self.assertEqual(sc._agy_pool_of_group("claude and gpt models"), "claude-gpt")
        self.assertEqual(sc._agy_pool_of_group("gemini models"), "gemini")

    def test_pools_by_inventory(self):
        for m in ("gemini-4-neon-high", "Gemini 4 Carbon (Low)", "gemini-4-argon-medium", "Gemini 4 Hydrogen (XHigh)",
                  "gemini-pro-orchestrator", "Orchestrator (Neon)", "gemini-3.8-flash-low"):
            self.assertEqual(sc.pool_for("antigravity", m, []), "gemini", m)
        for m in ("claude-opus-5-5-high", "Claude Sonnet 5.5 (Medium)", "gpt-oss-120b-medium"):
            self.assertEqual(sc.pool_for("antigravity", m, []), "claude-gpt", m)
        with self.assertRaises(sc.UnknownAgyModel):
            sc.pool_for("antigravity", "Gemini 3.7 Flash (Medium)", [])      # gone from agy models: no default pool
        self.assertEqual(unitcost._pool_of("agy", "gemini-4-neon-xhigh"), "antigravity:gemini")
        self.assertEqual(unitcost._pool_of("agy", "claude-opus-5-5-low"), "antigravity:claude-gpt")
        self.assertIsNone(unitcost._pool_of("agy", "mystery-model"))


class TablesAndContainment(unittest.TestCase):
    table = json.loads((ROOT / "tables" / "routing-table.json").read_text())

    def test_inventory_file_and_table_agree(self):
        rows = [l.split("\t") for l in (ROOT / "tables/inventory/agy.txt").read_text().splitlines() if l.strip()]
        inv = self.table["inventory"]["antigravity"]["models"]
        table_rows = sorted((r["id"], r["name"]) for rows_ in inv.values() for r in rows_)
        self.assertEqual(sorted((i, n) for i, n in rows), table_rows)
        names = {r["name"] for r in inv["gemini"]}
        for fam in ("Neon", "Carbon", "Argon", "Hydrogen"):
            self.assertTrue(any(f"Gemini 4 {fam}" in n for n in names), fam)
        self.assertLessEqual({"Claude Opus 5.5 (High)", "Claude Sonnet 5.5 (High)", "GPT-OSS 120B (Medium)"},
                             {r["name"] for r in inv["claude-gpt"]})

    def test_every_agy_candidate_is_in_the_inventory(self):
        known = {k.lower() for rows in self.table["inventory"]["antigravity"]["models"].values()
                 for r in rows for k in (r["id"], r["name"])}
        for band, cands in self.table["candidates"].items():
            for c in cands:
                if c.get("provider") == "agy-cli":
                    self.assertIn(c["model"].lower(), known, f"{band}: {c['model']}")

    def test_containment_stays(self):
        budgets = json.loads((ROOT / "tables" / "budgets.json").read_text())
        self.assertEqual(budgets["context"]["ceiling_tokens"]["antigravity"], 0)
        self.assertFalse(live.lane_enabled("antigravity"))


if __name__ == "__main__":
    unittest.main()
