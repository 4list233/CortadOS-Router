"""The antigravity (agy) lane, #144 PR A: honest probe failures, deny-all, isolation, inventory pools, tables, views.

Fake agy CLIs only (small scripts run in a real PTY by scarcity.tui_capture); no vendor CLI, no network, no account.
State is a throwaway directory; the module's STATE / CACHE / HISTORY / PROBE_DIR are patched to it.
"""
from __future__ import annotations
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
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
import os, time, tty
LOG = {log!r}
SCREENS = {screens!a}    # ASCII escapes: Python 3.9's tokenizer can misread a long line of multibyte text
with open(LOG + ".env", "w") as f:
    f.write("\n".join(sorted(os.environ)))
with open(LOG + ".cwd", "w") as f:
    f.write(os.path.realpath(os.getcwd()))
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
time.sleep(SCREENS.get("start_delay", 0))    # a slow start-up: nothing painted yet
out(SCREENS["boot"])
for step in SCREENS.get("boot_later", []):    # painted later, as agy does while it signs in
    time.sleep(0.6)
    if step.startswith("RAW:"):              # bytes as given: a bare LF stays a bare LF
        os.write(1, step[4:].encode())
    else:
        out(step)
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
    with open(LOG + ".times", "a") as f:
        f.write("%f %r\n" % (time.time(), d))
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

# agy 1.5.16's start-up banner as printed on the owner's screen (2026-11-24; email scrubbed to user@example.com): the
# subscription tier is on its account line, `<email> (Google AI Pro)`, not in the /usage box.
BANNER_FIXTURE = ROOT / "tests" / "fixtures" / "agy-banner.txt"
BANNER = (BANNER_FIXTURE.read_text()
          + "Resume with -c (or command below): agy --conversation=00000000-0000-0000-0000-000000000000\n> ")
LOGGED_OUT = "Antigravity CLI 1.5.16\nSign in with Google to continue.\nOpen https://accounts.google.com/o/oauth2/auth?x=1 in your browser\n"
# agy 1.5.16's transient start-up notice while it signs in from the keyring (captured on the Air 2026-11-25 under the
# probe's env; the spinner glyph is agy's): shown BEFORE the banner even when signed in (#144).
SIGNING_IN = "Welcome to the Antigravity CLI. You are currently not signed in.\n\n \u28f7 Signing in...\n"
FIRST_RUN = "Welcome to Antigravity CLI\nColor scheme: Dark / Solarized Dark / Solarized Light / Terminal default\n"
TRUST = "Do you trust the contents of this folder?\nTrust this workspace [Y/n]\n"


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

    # ---- #144 on the Air: a signed-in agy paints "not signed in / Signing in..." before its banner
    def test_transient_signing_in_notice_then_banner_is_read(self):
        binary, log = self.fake({"boot": SIGNING_IN, "boot_later": [BANNER], "usage": FIXTURE.read_text()})
        with self.env(binary):
            rec = sc._antigravity()
        self.assertEqual(rec["tier"], "AI Pro")
        self.assertEqual(self.keys(log), b"/usage\r")

    def test_notice_painted_before_its_spinner_is_not_refused_mid_read(self):
        notice, spinner = SIGNING_IN.split("\n\n", 1)
        binary, log = self.fake({"boot": notice + "\n", "boot_later": [spinner, BANNER], "usage": FIXTURE.read_text()})
        with self.env(binary):
            self.assertEqual(sc._antigravity()["tier"], "AI Pro")

    def test_signing_in_that_never_finishes_is_refused_and_nothing_is_typed(self):
        binary, log = self.fake({"boot": SIGNING_IN})
        with self.env(binary), mock.patch.object(sc, "tui_capture", self.short_boot(sc.tui_capture)):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual(cm.exception.kind, "not-logged-in")
        self.assertEqual(self.keys(log), b"")

    def test_signing_in_that_ends_on_a_sign_in_screen_is_refused_and_nothing_is_typed(self):
        binary, log = self.fake({"boot": SIGNING_IN, "boot_later": [LOGGED_OUT]})
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual(cm.exception.kind, "not-logged-in")
        self.assertEqual(self.keys(log), b"")

    def test_plain_not_signed_in_screen_is_still_refused(self):
        binary, log = self.fake({"boot": "Antigravity CLI 1.5.16\nYou are not signed in. Run agy and choose a sign-in method.\n"})
        with self.env(binary), mock.patch.object(sc, "tui_capture", self.short_boot(sc.tui_capture)):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual(cm.exception.kind, "not-logged-in")
        self.assertEqual(self.keys(log), b"")

    # ---- #144 fix round 1: only the banner's account line (`<email> (Google AI Pro)`) counts as signed in. A bare email
    # elsewhere (an account picker) neither ends the boot read nor clears the notice, and gets ZERO keystrokes.
    PICKER = "Choose account: test@example.invalid\n  1. test@example.invalid\n  2. Use another account\n"

    def test_account_picker_email_after_the_notice_gets_no_keystroke(self):
        for screens in ({"boot": SIGNING_IN + self.PICKER},
                        {"boot": SIGNING_IN, "boot_later": [self.PICKER]}):
            binary, log = self.fake({**screens, "usage": FIXTURE.read_text()})
            with self.subTest(screens=screens), self.env(binary), \
                    mock.patch.object(sc, "tui_capture", self.short_boot(sc.tui_capture)):
                with self.assertRaises(sc.ProbeRefused) as cm:
                    sc._antigravity()
                self.assertEqual(cm.exception.kind, "not-logged-in")
                self.assertEqual(self.keys(log), b"")
                self.assertEqual(cm.exception.typed, [])

    def test_account_picker_email_without_the_notice_gets_no_keystroke(self):
        binary, log = self.fake({"boot": "Antigravity CLI 1.5.16\n" + self.PICKER, "usage": FIXTURE.read_text()})
        with self.env(binary), mock.patch.object(sc, "tui_capture", self.short_boot(sc.tui_capture)):
            with self.assertRaises(sc.ProbeRefused) as cm:      # the picker is a sign-in screen: refused mid-read
                sc._antigravity()
        self.assertEqual((cm.exception.kind, cm.exception.typed), ("not-logged-in", []))
        self.assertIn("account-picker", cm.exception.facts["rules"])
        self.assertEqual(self.keys(log), b"")

    def test_account_picker_and_consent_screens_match_a_refusal_rule(self):
        for screen in (self.PICKER, "Choose an account\n", "Select your account\n",
                       "Antigravity CLI wants to access your Google Account\n", "Grant access to continue\n"):
            with self.subTest(screen=screen):
                self.assertTrue(any(re.search(p, screen, re.I) for _, p in sc.AGY_REFUSE))
        for benign in (BANNER, FIXTURE.read_text()):
            self.assertFalse(any(re.search(p, benign, re.I) for _, p in sc.AGY_REFUSE))
        # the trust prompt matches the workspace_not_trusted rule and no other (#144 fix round 5)
        self.assertEqual([k for k, p in sc.AGY_REFUSE if re.search(p, TRUST, re.I)], ["workspace_not_trusted"])

    def test_bare_email_is_not_boot_ready_or_settled_but_the_banner_is(self):
        self.assertFalse(sc.AGY_BOOT_READY("Choose account: test@example.invalid"))
        self.assertTrue(sc.AGY_BOOT_READY(BANNER))
        self.assertTrue(sc._agy_unsettled(SIGNING_IN + "Choose account: test@example.invalid\n"))
        self.assertFalse(sc._agy_unsettled(SIGNING_IN + BANNER))
        self.assertFalse(sc._agy_unsettled(BANNER))
        self.assertTrue(sc._agy_unsettled(BANNER + "You are not signed in.\n"))    # a notice AFTER the banner is unsettled

    # ---- #144, the 06:41 sample on the Air: agy's log shows the keyring token loaded and refreshed and "authenticated
    # successfully" 0.5 s in, yet the probe refused "not-logged-in" at boot 3.6 s in. A refusal now carries fixed-name
    # facts (which rule, which check, counts and order of notices / account lines) so the next sample says which.
    def test_sign_in_rules_join_to_the_same_refuse_pattern(self):
        self.assertEqual(sc.AGY_REFUSE[0][1],
                         r"accounts\.google\.com|sign in with google|authori[sz]ation (?:url|code)|paste (?:the|your) (?:authori[sz]ation )?code"
                         r"|" + sc._AGY_NOT_SIGNED_IN + r"(?![.\s]*\S?\s*signing in)(?![.\s]*$)"
                         r"|please (?:sign|log) in|login required"
                         r"|choose (?:an? |your )?account|select (?:an? |your )?account|use another account"
                         r"|wants to access your google account|(?:grant|allow) (?:access|permission)|\bconsent\b"
                         r"|select (?:a |your )?log\s?in method|google oauth|use a google cloud project")

    def test_notice_repainted_after_the_banner_is_refused_with_facts_and_nothing_typed(self):
        repaint = "\x1b[2J\x1b[HWelcome to the Antigravity CLI. You are currently not signed in.\n> \n"
        binary, log = self.fake({"boot": SIGNING_IN, "boot_later": [BANNER, repaint], "usage": FIXTURE.read_text()})
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual((cm.exception.kind, self.keys(log), cm.exception.typed), ("not-logged-in", b"", []))
        f = cm.exception.facts
        self.assertEqual((f["check"], f["acting"], f["rules"]), ("refuse", False, ["not-signed-in-notice"]))
        self.assertEqual((f["notices"], f["signing_in"], f["account_lines"]), (2, 1, 1))
        self.assertTrue(f["account_line_before_last_notice"])
        self.assertFalse(f["account_line_after_last_notice"])
        self.assertTrue(f["ready_seen"])
        rec = sc._failed_record("antigravity", None, cm.exception)
        self.assertEqual(rec["refusal"], f)
        self.assertEqual(rec["error"], "ProbeRefused: probe refused at boot: the CLI is waiting for not-logged-in; "
                                       "nothing was typed")           # the error and its kind are unchanged
        flat = json.dumps(rec)
        self.assertNotIn("@", flat)
        self.assertNotIn("Welcome", flat)

    def test_unsettled_refusal_at_the_end_of_boot_names_its_check(self):
        binary, log = self.fake({"boot": SIGNING_IN})
        with self.env(binary), mock.patch.object(sc, "tui_capture", self.short_boot(sc.tui_capture)):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        f = cm.exception.facts
        self.assertEqual((f["check"], f["acting"], f["ready_seen"], f["rules"]), ("unsettled", True, False, []))
        self.assertEqual((f["notices"], f["signing_in"], f["account_lines"]), (1, 1, 0))
        self.assertGreaterEqual(f["after_s"], 3)

    def test_a_failure_without_facts_drops_an_old_refusal(self):
        rec = sc._failed_record("antigravity", {"refusal": {"check": "refuse"}}, RuntimeError("x"))
        self.assertNotIn("refusal", rec)

    def test_signed_out_screen_is_refused_promptly(self):
        binary, log = self.fake({"boot": LOGGED_OUT, "usage": FIXTURE.read_text()})
        t0 = time.time()
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertLess(time.time() - t0, 5)            # refused on sight, not after the 12 s boot window
        self.assertEqual((cm.exception.kind, self.keys(log)), ("not-logged-in", b""))

    @staticmethod
    def short_boot(real):
        return lambda *a, **k: real(*a, **{**k, "boot_s": 3})

    def test_first_run_prompt_is_refused_and_nothing_is_typed(self):
        binary, log = self.fake({"boot": FIRST_RUN})
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual(cm.exception.kind, "first-run-pending")
        self.assertEqual(self.keys(log), b"")

    # ---- #144 fix round 5: agy runs in ONE fixed folder the owner trusts once by hand. Before the banner's account line
    # is proven the probe types NOTHING: a trust prompt means the folder is not trusted and refuses
    # `workspace_not_trusted` with zero keystrokes. Rounds 1-4's "is the prompt still on screen" geometry is gone.
    def test_trust_prompt_before_the_account_line_gets_no_keystroke(self):
        for screens in ({"boot": TRUST}, {"boot": SIGNING_IN, "boot_later": [TRUST]},
                        {"boot": "\x1b[12;30H" + TRUST}):
            binary, log = self.fake({**screens, "after_trust": BANNER, "usage": FIXTURE.read_text()})
            with self.subTest(screens=screens), self.env(binary), \
                    mock.patch.object(sc, "tui_capture", self.short_boot(sc.tui_capture)):
                with self.assertRaises(sc.ProbeRefused) as cm:
                    sc._antigravity()
                self.assertEqual((cm.exception.kind, cm.exception.typed, self.keys(log)), ("workspace_not_trusted", [], b""))
                self.assertIs(cm.exception.facts["trust_prompt_seen"], True)
                self.assertIn("nothing was typed", str(cm.exception))
                rec = sc._failed_record("antigravity", None, cm.exception)
                self.assertEqual(rec["error_kind"], "workspace_not_trusted")
                self.assertIn("not trusted", sc.lane_state(rec))
                self.assertIs(rec["refusal"]["trust_prompt_seen"], True)
                self.assertNotIn("Trust", json.dumps(rec["refusal"]))

    def test_trust_prompt_after_the_banner_is_refused_too(self):
        binary, log = self.fake({"boot": BANNER, "boot_later": [TRUST], "usage": FIXTURE.read_text()})
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual((cm.exception.kind, self.keys(log)), ("workspace_not_trusted", b""))

    def test_review_reproductions_get_no_keystroke(self):
        # round-4 review: a DECSTBM scroll region then CUP and newlines, and a bare LF in raw mode, hid the prompt yet
        # passed the geometry check. With no keystroke before the account line, both are refused, nothing typed.
        for later in ([TRUST, "RAW:\x1b[1;2r\x1b[2;1H" + "\n" * 5 + "Loading\n"],
                      ["RAW:Trust this workspace [Y/n]\n\n\n"],
                      ["RAW:\x1b[1;2r\x1b[60;1H\n\n" + "Do you trust the contents of this folder?\r\n", BANNER]):
            binary, log = self.fake({"boot": SIGNING_IN, "boot_later": later, "after_trust": BANNER,
                                     "usage": FIXTURE.read_text()})
            with self.subTest(later=later), self.env(binary), \
                    mock.patch.object(sc, "tui_capture", self.short_boot(sc.tui_capture)):
                with self.assertRaises(sc.ProbeRefused) as cm:
                    sc._antigravity()
                self.assertEqual((cm.exception.kind, cm.exception.typed, self.keys(log)), ("workspace_not_trusted", [], b""))

    def test_trust_prompt_with_a_sign_in_screen_gets_no_keystroke(self):
        binary, log = self.fake({"boot": LOGGED_OUT + TRUST, "after_trust": BANNER, "usage": FIXTURE.read_text()})
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertEqual((cm.exception.kind, self.keys(log), cm.exception.typed), ("not-logged-in", b"", []))

    def test_unsettled_without_a_trust_prompt_records_none(self):
        binary, log = self.fake({"boot": SIGNING_IN})
        with self.env(binary), mock.patch.object(sc, "tui_capture", self.short_boot(sc.tui_capture)):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertIs(cm.exception.facts["trust_prompt_seen"], False)
        self.assertNotIn("trust_rejected", cm.exception.facts)

    def test_normal_path_runs_in_the_fixed_folder(self):
        binary, log = self.fake({"boot": SIGNING_IN, "boot_later": [BANNER], "usage": FIXTURE.read_text()})
        with self.env(binary):
            rec = sc._antigravity()
        root, workspace, lock = sc.agy_probe_paths()
        self.assertEqual((root, workspace, lock), (sc.STATE / "agy-probe", sc.STATE / "agy-probe" / "workspace",
                                                    sc.STATE / "agy-probe" / "lock"))
        self.assertEqual(rec["tier"], "AI Pro")
        self.assertEqual(self.keys(log), b"/usage\r")
        self.assertEqual(Path(str(log) + ".cwd").read_text(), os.path.realpath(workspace))
        self.assertEqual((workspace.stat().st_mode & 0o777, root.stat().st_mode & 0o777), (0o700, 0o700))
        self.assertEqual(list(workspace.iterdir()), [])          # kept, empty, for the next run
        self.assertFalse(sc.PROBE_DIR.exists() and any(sc.PROBE_DIR.iterdir()))
        with self.env(binary):                                   # the same folder again: the lock was released
            self.assertEqual(sc._antigravity()["tier"], "AI Pro")

    def test_lock_held_skips_the_sample_and_starts_no_agy(self):
        import fcntl
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        root, workspace, lock = sc.agy_probe_paths()
        root.mkdir(mode=0o700); os.chmod(root, 0o700)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        t0 = time.time()
        with self.env(binary), mock.patch.object(sc, "AGY_PROBE_LOCK_WAIT_S", 0.5), \
                mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises(sc.ProbeSkipped) as cm:
                sc._antigravity()
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(cm.exception.kind, "probe_busy")
        self.assertEqual(self.keys(log), b"")

    def test_skipped_sample_keeps_the_last_reading_out_of_the_history(self):
        old = {"windows": [{"window": "week (gemini)", "pool": "gemini", "used_pct": 1.0}], "tried_at": 1.0,
               "fetched_at": 1.0, "error": None}
        sc.CACHE.write_text(json.dumps({"fetched_at": 0, "lanes": {"antigravity": old}}))

        def busy():
            raise sc.ProbeSkipped("agy probe skipped: another probe held the lock")
        with mock.patch.dict(sc.SAMPLERS, {"antigravity": busy}), \
                mock.patch.object(sc._reaper, "sweep_stale", return_value=None), \
                mock.patch.object(sc, "_learn_unit_costs", lambda: None), redirect_stderr(io.StringIO()) as err:
            sc._sample_locked(force=True, only=["antigravity"])
        lane = json.loads(sc.CACHE.read_text())["lanes"]["antigravity"]
        self.assertEqual((lane["windows"], lane["fetched_at"], lane["last_skip"]["reason"]),
                         (old["windows"], 1.0, "probe_busy"))
        self.assertFalse(lane.get("error"))
        self.assertIn("skipped (probe_busy)", err.getvalue())
        hist = sc.HISTORY.read_text().splitlines() if sc.HISTORY.exists() else []
        self.assertTrue(all("antigravity" not in json.loads(h)["lanes"] for h in hist))

    def test_non_empty_folder_is_refused_before_agy_starts(self):
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        _, workspace, _ = sc.agy_probe_paths()
        workspace.mkdir(parents=True, mode=0o700); os.chmod(workspace.parent, 0o700); os.chmod(workspace, 0o700)
        for leftover in ("notes.txt", ".agents"):
            (workspace / leftover).mkdir() if leftover.startswith(".") else (workspace / leftover).write_text("x")
        with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises(sc.ProbeFolderRefused) as cm:
                sc._antigravity()
        rec = sc._failed_record("antigravity", None, cm.exception)
        self.assertEqual((rec["error_kind"], sc.lane_state(rec)), ("probe_folder_not_clean", "refused: probe folder not empty"))
        self.assertIn("2 entries", str(cm.exception))
        self.assertNotIn("notes", str(cm.exception))             # a count only, never the names
        self.assertEqual(sc.AGY_WORKSPACE_LEFTOVERS, frozenset())

    def test_folder_with_a_symlinked_ancestor_is_refused(self):
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        real = self.tmp / "elsewhere"
        real.mkdir(mode=0o700)
        for link_at in ("state", "agy-probe", "workspace"):
            with self.subTest(link_at=link_at):
                state = self.tmp / f"s-{link_at}"
                if link_at == "state":
                    state.symlink_to(real)
                else:
                    state.mkdir()
                    if link_at == "agy-probe":
                        (state / "agy-probe").symlink_to(real)
                    else:
                        (state / "agy-probe").mkdir(mode=0o700)
                        (state / "agy-probe" / "workspace").symlink_to(real)
                with self.env(binary), mock.patch.object(sc, "STATE", state), \
                        mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
                    with self.assertRaises(sc.ProbeFolderRefused) as cm:
                        sc._antigravity()
                self.assertEqual(cm.exception.kind, "probe_folder_unsafe")

    def test_folder_open_to_others_is_refused_and_not_fixed(self):
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        _, workspace, _ = sc.agy_probe_paths()
        workspace.mkdir(parents=True)
        os.chmod(workspace.parent, 0o700); os.chmod(workspace, 0o755)
        with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises(sc.ProbeFolderRefused) as cm:
                sc._antigravity()
        self.assertEqual(cm.exception.kind, "probe_folder_unsafe")
        self.assertEqual(workspace.stat().st_mode & 0o777, 0o755)

    # ---------------------------------------------------------------- fix round 6: fd-bound folder, no kill by cwd
    def make_folder(self) -> tuple[Path, Path, Path]:
        root, workspace, lock = sc.agy_probe_paths()
        workspace.mkdir(parents=True)
        os.chmod(root, 0o700); os.chmod(workspace, 0o700)
        return root, workspace, lock

    def sleeper(self, cwd: Path):
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=str(cwd))
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        deadline = time.time() + 10          # its identity is readable once ps sees it
        while sc._reaper.process_identity(proc.pid) is None and time.time() < deadline:
            time.sleep(0.05)
        return proc

    def write_record(self, root: Path, rec: dict) -> None:
        p = root / sc.AGY_PROBE_CHILD_RECORD
        p.write_text(json.dumps(rec)); os.chmod(p, 0o600)

    def test_process_open_in_the_folder_skips_the_sample_and_is_not_killed(self):
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        _, workspace, _ = self.make_folder()
        proc = self.sleeper(workspace)       # e.g. the owner's own agy during the one-time trust step
        with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises(sc.ProbeSkipped) as cm:
                sc._antigravity()
        self.assertEqual(cm.exception.kind, "probe_folder_busy")
        self.assertEqual(sc.LANE_STATE_TEXT["probe_folder_busy"], "skipped: a process is open in the probe folder")
        time.sleep(0.3)
        self.assertIsNone(proc.poll())       # still running: never killed
        self.assertEqual(self.keys(log), b"")

    def test_recorded_probe_child_still_alive_is_killed(self):
        root, _, _ = self.make_folder()
        proc = self.sleeper(self.tmp)        # the dead sampler's child, outside the folder (its cwd is not the proof)
        uid, start = sc._reaper.process_identity(proc.pid)
        self.write_record(root, {"pid": proc.pid, "uid": uid, "start": start})
        with sc.agy_probe_workspace():
            pass
        self.assertEqual(proc.wait(timeout=5), -9)
        self.assertFalse((root / sc.AGY_PROBE_CHILD_RECORD).exists())

    def test_recycled_pid_is_not_killed(self):
        root, _, _ = self.make_folder()
        proc = self.sleeper(self.tmp)
        uid, _ = sc._reaper.process_identity(proc.pid)
        for rec in ({"pid": proc.pid, "uid": uid, "start": "Thu Jan  1 00:00:00 2026"},       # other start time
                    {"pid": proc.pid, "uid": uid + 1, "start": sc._reaper.process_identity(proc.pid)[1]}):  # other uid
            with self.subTest(rec=rec):
                self.write_record(root, rec)
                with sc.agy_probe_workspace():
                    pass
                time.sleep(0.3)
                self.assertIsNone(proc.poll())
                self.assertFalse((root / sc.AGY_PROBE_CHILD_RECORD).exists())

    def test_the_probe_records_its_child_and_drops_the_record_after(self):
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        root, _, _ = sc.agy_probe_paths()
        seen = []
        real = sc.agy_probe_workspace.record_child

        def spy(ws, pid):
            real(ws, pid)
            seen.append((pid, json.loads((root / sc.AGY_PROBE_CHILD_RECORD).read_text()),
                         oct((root / sc.AGY_PROBE_CHILD_RECORD).stat().st_mode & 0o777)))
        with self.env(binary), mock.patch.object(sc.agy_probe_workspace, "record_child", spy):
            sc._antigravity()
        [(pid, rec, mode)] = seen
        self.assertEqual((rec["pid"], rec["uid"], mode), (pid, os.geteuid(), "0o600"))
        self.assertTrue(rec["start"])
        self.assertFalse((root / sc.AGY_PROBE_CHILD_RECORD).exists())

    def test_the_end_of_probe_cleanup_never_kills_by_cwd(self):
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        with self.env(binary), mock.patch.object(sc._reaper, "probe_cwd_pids",
                                                 side_effect=AssertionError("no kill by cwd")):
            self.assertEqual(sc._antigravity()["tier"], "AI Pro")

    def test_ancestor_writable_by_others_is_refused(self):
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        for mode in (0o775, 0o757, 0o777):
            with self.subTest(mode=oct(mode)):
                shared = self.tmp / f"shared-{mode:o}"
                (shared / "state").mkdir(parents=True)
                os.chmod(shared, mode)
                with self.env(binary), mock.patch.object(sc, "STATE", shared / "state"), \
                        mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
                    with self.assertRaises(sc.ProbeFolderRefused) as cm:
                        sc._antigravity()
                self.assertEqual(cm.exception.kind, "probe_folder_unsafe")
                self.assertIn("writable by others", str(cm.exception))

    def test_ancestor_rule(self):
        import stat as st_
        me = os.geteuid()

        def st(uid, mode):
            return os.stat_result((st_.S_IFDIR | mode, 0, 0, 2, uid, 0, 0, 0, 0, 0))
        self.assertTrue(sc._ancestor_ok(st(0, 0o755)))
        self.assertTrue(sc._ancestor_ok(st(me, 0o700)))
        self.assertTrue(sc._ancestor_ok(st(0, 0o1777)))         # /private/tmp
        self.assertFalse(sc._ancestor_ok(st(0, 0o777)))         # writable, not sticky
        self.assertFalse(sc._ancestor_ok(st(me, 0o1777)))       # sticky but not root's
        self.assertFalse(sc._ancestor_ok(st(me, 0o775)))
        self.assertFalse(sc._ancestor_ok(st(me + 1, 0o755)))    # another user's

    def test_state_under_the_sticky_root_owned_tmp_is_allowed(self):
        base = Path(tempfile.mkdtemp(dir="/tmp"))                # /tmp -> private/tmp: root's link, root's sticky dir
        self.addCleanup(shutil.rmtree, base, True)
        os.chmod(base, 0o700)
        with mock.patch.object(sc, "STATE", base / "state"), sc.agy_probe_workspace() as folder:
            self.assertEqual(folder, base / "state" / "agy-probe" / "workspace")

    def swap_before_fork(self, swap):
        """Run the probe with `swap()` called just before the fork (in cwd_check, after every folder check and the lock)."""
        real = iso.check_dotenv
        calls = []

        def hook(home, workdir):
            calls.append(workdir)
            if len(calls) == 2:          # 1: prepare(), before the folder is opened; 2: cwd_check, just before the fork
                swap()
            return real(home, workdir)
        return mock.patch.object(iso, "check_dotenv", hook)

    def test_workspace_replaced_by_a_symlink_after_the_checks_does_not_move_the_child(self):
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        root, workspace, _ = self.make_folder()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)

        def swap():
            workspace.rename(root / "workspace-checked")
            workspace.symlink_to(elsewhere)
        with self.env(binary), self.swap_before_fork(swap):
            self.assertEqual(sc._antigravity()["tier"], "AI Pro")
        self.assertEqual(Path(str(log) + ".cwd").read_text(), os.path.realpath(root / "workspace-checked"))

    def test_state_dir_swapped_after_the_lock_has_no_effect(self):
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        root, workspace, _ = self.make_folder()
        moved = self.tmp / "state-moved"

        def swap():
            sc.STATE.rename(moved)
            (sc.STATE / "agy-probe" / "workspace").mkdir(parents=True)
        with self.env(binary), self.swap_before_fork(swap):
            self.assertEqual(sc._antigravity()["tier"], "AI Pro")
        self.assertEqual(Path(str(log) + ".cwd").read_text(), os.path.realpath(moved / "agy-probe" / "workspace"))
        self.assertFalse((moved / "agy-probe" / sc.AGY_PROBE_CHILD_RECORD).exists())   # cleared through the dir fd
        self.assertFalse((sc.STATE / "agy-probe" / sc.AGY_PROBE_CHILD_RECORD).exists())

    def test_slow_start_up_does_not_eat_the_boot_window(self):
        # the boot window starts when the CLI first paints: a 2.5 s start-up under a 3 s window still reads the banner
        # painted 0.6 s after the notice
        binary, log = self.fake({"start_delay": 2.5, "boot": SIGNING_IN, "boot_later": [BANNER],
                                 "usage": FIXTURE.read_text()})
        with self.env(binary), mock.patch.object(sc, "tui_capture", self.short_boot(sc.tui_capture)):
            rec = sc._antigravity()
        self.assertEqual(rec["tier"], "AI Pro")
        self.assertEqual(self.keys(log), b"/usage\r")

    def test_other_clis_trust_answer_read_gets_a_fresh_boot_window_as_on_main(self):
        # claude / codex / cursor still answer their trust prompt (answer_trust, as on main), and the read after the
        # answer gets a fresh boot_s, as on main (#144 fix round 6 restored main's timing)
        binary, log = self.fake({"boot": SIGNING_IN, "boot_later": [TRUST]})
        sc.tui_capture([binary], "/usage", "never", {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, boot_s=3,
                       run_s=0.5, inherit_path=False)
        reads = [line.split(" ", 1) for line in Path(str(log) + ".times").read_text().splitlines()]
        self.assertEqual(reads[0][1], "b'\\r'")
        enter_at = float(reads[0][0])
        usage_at = next(float(t) for t, d in reads[1:] if "/" in d)
        # nothing is painted after the Enter, so the read after it waits its whole window: a fresh 3 s (a shared window
        # would have had only ~0.9 s left)
        self.assertGreaterEqual(usage_at - enter_at, 2.8)

    # agy 1.5.16 signed out, drawn to a headless child on the Air (2026-11-25; described, no email): alternate screen,
    # logo, the notice, a "Signing in..." spinner, then a bare CR overwriting it with the login-method picker.
    LOGIN_METHOD = ("\x1b[?1049h\x1b[H\x1b[2J  ▄▀▀▄ ANTIGRAVITY\n\n" + SIGNING_IN.rstrip("\n"),
                    "\r\x1b[2KSelect login method:\n\n\x1b[36m> 1. Google OAuth\x1b[0m\n  2. Use a Google Cloud project\n\n"
                    "↑/↓ Navigate · enter Select\n")

    def test_login_method_picker_is_refused_mid_read_and_nothing_is_typed(self):
        boot, picker = self.LOGIN_METHOD
        binary, log = self.fake({"boot": boot, "boot_later": [picker], "usage": FIXTURE.read_text()})
        t0 = time.time()
        with self.env(binary):
            with self.assertRaises(sc.ProbeRefused) as cm:
                sc._antigravity()
        self.assertLess(time.time() - t0, 6)             # refused on sight, not at the end of the 12 s boot window
        self.assertEqual((cm.exception.kind, cm.exception.typed, self.keys(log)), ("not-logged-in", [], b""))
        self.assertEqual((cm.exception.facts["check"], cm.exception.facts["acting"]), ("refuse", False))
        self.assertIn("login-method-picker", cm.exception.facts["rules"])
        for screen in ("Select login method:", "> 1. Google OAuth", "2. Use a Google Cloud project"):
            with self.subTest(screen=screen):
                self.assertTrue(re.search(sc.AGY_REFUSE[0][1], screen, re.I))

    def test_usage_screen_is_read_through_the_isolated_env(self):
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        with self.env(binary):
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

    # ---- #144 round 3: dotenv files and private text
    def test_dotenv_with_a_banned_prefix_refuses_the_probe_before_any_start(self):
        binary, log = self.fake({"boot": BANNER})
        for name in ("GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "AGY_ADC_AUTH", "CLOUDSDK_CONFIG"):
            for where in (self.home / ".gemini" / ".env", self.home / ".env", sc.agy_probe_paths()[0] / ".env"):
                with self.subTest(name=name, where=where.name):
                    where.parent.mkdir(parents=True, exist_ok=True)
                    where.write_text(f"{name}=secret-value-42\n")
                    with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
                        with self.assertRaises(iso.ForbiddenEnv) as cm:
                            sc._antigravity()
                    rec = sc._failed_record("antigravity", None, cm.exception)
                    self.assertEqual(rec["error_kind"], "forbidden-env")
                    self.assertNotIn("secret-value-42", rec["error"])
                    where.unlink()
        self.assertEqual(self.keys(log), b"")

    def test_dotenv_planted_in_the_probe_folder_refuses_before_the_fork(self):
        binary, _ = self.fake({"boot": BANNER})
        real = iso.check_dotenv

        def plant_then_check(home, workdir):
            if Path(workdir).name == "workspace" and Path(workdir).is_dir():   # the fixed folder, just before the fork
                Path(workdir, ".env").write_text("GEMINI_API_KEY=k\n")
            return real(home, workdir)
        with self.env(binary), mock.patch.object(iso, "check_dotenv", plant_then_check), \
                mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises(iso.ForbiddenEnv):
                sc._antigravity()
        # the owner's trusted folder is never emptied by code: the planted file stays, and the next probe refuses it
        _, workspace, _ = sc.agy_probe_paths()
        self.assertEqual([p.name for p in workspace.iterdir()], [".env"])
        with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises((iso.ForbiddenEnv, sc.ProbeFolderRefused)):
                sc._antigravity()

    def test_unparsable_dotenv_refuses_the_probe(self):
        binary, _ = self.fake({"boot": BANNER})
        p = self.home / ".gemini" / ".env"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("GEMINI_API_KEY=\"secret\nmore\"\n")
        with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises(iso.SettingsRefused) as cm:
                sc._antigravity()
        self.assertNotIn("secret", str(cm.exception))

    def test_harmless_dotenv_lets_the_probe_run(self):
        p = self.home / ".gemini" / ".env"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("EDITOR=vim\n")
        iso.check_dotenv(self.home, sc.agy_probe_paths()[1])
        binary, log = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        with self.env(binary):
            rec = sc._antigravity()
        self.assertEqual(rec["tier"], "AI Pro")

    def test_no_groups_error_holds_no_screen_text(self):
        marker = "PRIVATE-MARKER-9c1e"
        screen = (f"Models & Quota\nAccount: {marker} owner@example.com\nNote: {marker}\n"
                  "Limit Remaining ?? Limit Remaining ??\n")
        binary, _ = self.fake({"boot": BANNER, "usage": screen})
        with self.env(binary):
            with self.assertRaisesRegex(RuntimeError, "no quota groups") as cm:
                sc._antigravity()
        err = io.StringIO()
        with redirect_stderr(err):
            rec = sc._failed_record("antigravity", None, cm.exception)
        for text in (str(cm.exception), rec["error"], err.getvalue(), json.dumps(rec)):
            self.assertNotIn(marker, text); self.assertNotIn("example.com", text); self.assertNotIn("Account", text)

    def test_unknown_setting_key_is_not_echoed_into_the_record(self):
        binary, _ = self.fake({"boot": BANNER})
        self.write_settings({"trustedWorkspaces": [], "owner.private@example.com": True})
        with self.env(binary), mock.patch.object(sc.pty, "fork", side_effect=AssertionError("must not fork")):
            with self.assertRaises(iso.SettingsRefused) as cm:
                sc._antigravity()
        err = io.StringIO()
        with redirect_stderr(err):
            rec = sc._failed_record("antigravity", None, cm.exception)
        for text in (rec["error"], err.getvalue()):
            self.assertNotIn("example.com", text); self.assertNotIn("owner.private", text)

    # ---- #144 review round 4: an unrecognised group's title never reaches an error, the state, a log or the record
    PRIVATE_GROUP = ("PRIVATE CUSTOMER ACME MODELS\nModels within this group: Acme Secret Model 7\n"
                     "Weekly Limit Remaining [ █████      ] 50.00%\nRefreshes in 1h 0m\n")

    def assert_no_private(self, *texts):
        for text in texts:
            low = text.lower()
            for word in ("private", "customer", "acme", "secret"):
                self.assertNotIn(word, low)

    def sample_with(self, screen: str, boot: str = BANNER) -> tuple[dict, str]:
        binary, _ = self.fake({"boot": boot, "usage": screen})
        err = io.StringIO()
        with self.env(binary), mock.patch.object(sc, "_learn_unit_costs", lambda: None), \
                redirect_stderr(err):
            sc.sample(force=True, only=["antigravity"])
        state = sc.CACHE.read_text()
        history = sc.HISTORY.read_text() if sc.HISTORY.exists() else ""
        self.assert_no_private(state, history, err.getvalue())
        return json.loads(state)["lanes"]["antigravity"], err.getvalue()

    def test_unknown_group_title_never_reaches_a_partial_reading_error(self):
        screen = FIXTURE.read_text() + self.PRIVATE_GROUP
        binary, _ = self.fake({"boot": BANNER, "usage": screen})
        with self.env(binary):
            with self.assertRaises(sc.ProbePartialReading) as cm:
                sc._antigravity()
        self.assertIn("unknown-group-1: no 5-hour window", str(cm.exception))
        err = io.StringIO()
        with redirect_stderr(err):
            rec = sc._failed_record("antigravity", None, cm.exception)
        self.assert_no_private(str(cm.exception), json.dumps(rec), err.getvalue())
        lane, stderr = self.sample_with(screen)
        self.assertEqual(lane["error_kind"], "partial-reading")
        self.assertIn("unknown-group-1: no 5-hour window", lane["error"])
        self.assertIn("unknown-group-1", stderr)

    def test_unknown_groups_are_kept_under_fixed_labels_only(self):
        second = self.PRIVATE_GROUP.replace("PRIVATE CUSTOMER ACME", "SECRET ACME TWO")
        full = lambda g: g + "Five Hour Limit Remaining [ █████      ] 50.00%\nRefreshes in 1h 0m\n"
        screen = FIXTURE.read_text() + full(self.PRIVATE_GROUP) + full(second)
        binary, _ = self.fake({"boot": BANNER, "usage": screen})
        with self.env(binary):
            rec = sc._antigravity()
        self.assert_no_private(json.dumps(rec))
        self.assertEqual({w["pool"] for w in rec["windows"]},
                         {"gemini", "claude-gpt", "unknown-group-1", "unknown-group-2"})
        self.assertIn("week (unknown-group-1)", {w["window"] for w in rec["windows"]})
        lane, _ = self.sample_with(screen)
        self.assertFalse(lane.get("error"), lane.get("error"))
        self.assertIn("5-hour (unknown-group-2)", {w["window"] for w in lane["windows"]})

    # ---- #144: agy stores no auth mode; the subscription tier is read from its start-up banner (confirmed 2026-11-24)
    def test_banner_tier_passes(self):
        binary, _ = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})       # the /usage box shows no tier
        with self.env(binary):
            self.assertEqual(sc._antigravity()["tier"], "AI Pro")

    def test_unconfirmed_pattern_still_refuses(self):
        binary, _ = self.fake({"boot": BANNER, "usage": FIXTURE.read_text()})
        with self.env(binary), mock.patch.object(iso, "TIER_PATTERN_CONFIRMED", False):
            with self.assertRaisesRegex(iso.SubscriptionUnproven, "not confirmed") as cm:
                sc._antigravity()
        self.assertEqual(sc._failed_record("antigravity", None, cm.exception)["error_kind"], "subscription-unproven")

    def test_api_key_screen_is_refused_even_with_a_tier(self):
        binary, _ = self.fake({"boot": BANNER, "usage": "Auth \u00b7 API key\n" + FIXTURE.read_text()})
        with self.env(binary):
            with self.assertRaisesRegex(iso.SubscriptionUnproven, "non-subscription"):
                sc._antigravity()

    def test_banner_without_a_personal_tier_is_refused(self):
        for line in ("user@example.com", "user@example.com (Google AI Plus)", "user@example.com (Free)",
                     "Google AI Pro"):                       # no tier, unknown tiers, a tier not on the account line
            boot = BANNER.replace("user@example.com (Google AI Pro)", line)
            binary, _ = self.fake({"boot": boot, "usage": "Account: user@example.com\n" + FIXTURE.read_text()})
            with self.subTest(line=line), self.env(binary):
                with self.assertRaisesRegex(iso.SubscriptionUnproven, "no personal") as cm:
                    sc._antigravity()
                self.assertEqual(sc.lane_state(sc._failed_record("antigravity", None, cm.exception)),
                                 "refused: subscription login not proven")

    def test_account_email_never_reaches_the_record_state_or_errors(self):
        private = "owner.private@example.com"
        usage = f"Models & Quota\nAccount: {private}\n" + FIXTURE.read_text()
        binary, _ = self.fake({"boot": BANNER.replace("user@example.com", private), "usage": usage})
        with self.env(binary):
            rec = sc._antigravity()                                      # a passing reading
        self.assertEqual(rec["tier"], "AI Pro")
        self.assertNotIn("owner.private", json.dumps(rec))
        lane, stderr = self.sample_with(usage, boot=BANNER.replace("user@example.com", private))
        self.assertNotIn("owner.private", json.dumps(lane) + stderr)
        for state_file in self.state.glob("*"):
            if state_file.is_file():
                self.assertNotIn("owner.private", state_file.read_text(errors="replace"), state_file.name)
        for boot in (BANNER.replace("user@example.com (Google AI Pro)", private),          # refused: no tier
                     BANNER.replace("user@example.com", private)):
            binary, _ = self.fake({"boot": boot, "usage": f"Models & Quota\nAccount: {private}\nnothing here\n"
                                                          "Limit Remaining x Limit Remaining y\n"})
            with self.env(binary):
                with self.assertRaises(Exception) as cm:
                    sc._antigravity()
            rec = sc._failed_record("antigravity", None, cm.exception)
            self.assertNotIn("owner.private", rec["error"])
            self.assertNotIn("owner.private", str(cm.exception))

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
        self.assertIn("claude-gpt: no 5-hour window", gaps)
        self.assertTrue(any(x.startswith("pager not read to the end") for x in gaps), gaps)
        self.assertEqual(sc._agy_next_page(first), (b"\x1b[B", "Down"))

    def test_pager_is_paged_with_navigation_keys_until_every_group_is_read(self):
        lines = PAGER_FIXTURE.read_text().splitlines()
        self.assertEqual(len(lines), 30)
        binary, log = self.fake({"boot": BANNER, "pager": lines})
        with self.env(binary):
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
        self.assertIn("claude-gpt: no 5-hour window", str(cm.exception))
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
        self.assertIsNone(sc._agy_pool_of_group("private customer acme models"))
        self.assertEqual(sc.agy_group_labels(["x models", "gemini models", "y models"]),
                         {"x models": "unknown-group-1", "gemini models": "gemini", "y models": "unknown-group-2"})

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
