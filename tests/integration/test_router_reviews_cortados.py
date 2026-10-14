"""Review-policy checks that read the private CortadOS tree: the PTY plugin's mail guard and the fleet roster.

Moved unchanged from tests/test_router_reviews.py (only ROOT → CORTADOS). Skipped unless CORTADOS_REPO (default ~/cortados)
holds faces.json and plugins/cortados-pty/mailguard.py. Read-only: the guard's taint file is redirected to a temp dir.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch
import unittest

CORTADOS = Path(os.environ.get('CORTADOS_REPO', Path.home() / 'cortados'))
if not ((CORTADOS / 'faces.json').is_file() and (CORTADOS / 'plugins/cortados-pty/mailguard.py').is_file()):
    raise unittest.SkipTest(f"CortadOS integration test: needs the CortadOS tree at CORTADOS_REPO (now {CORTADOS}) "
                            "with faces.json and plugins/cortados-pty/mailguard.py")

from tests import test_router_reviews as _reviews   # module import: a class import here would make unittest rerun ReviewTests
load = _reviews.load


class CortadOSReviewTests(unittest.TestCase):
    setUp = _reviews.ReviewTests.setUp
    author = _reviews.ReviewTests.author

    def test_mail_read_guard_covers_review_dispatch(self):
        import sys
        with patch.object(sys, 'path', [str(CORTADOS/'plugins/cortados-pty'), *sys.path]):
            guard = load('review_mailguard_test', CORTADOS/'plugins/cortados-pty/mailguard.py')
        with patch.object(guard, 'TAINT', self.state/'taint.json'), patch.object(guard, 'inbound_actions_allowed', return_value=False):
            guard.pre_tool_call('mail_get', session_id='s', turn_id='t')
            self.assertEqual(guard.pre_tool_call('router_review', {'action':'request'}, session_id='s', turn_id='t')['action'], 'block')

    def test_profile_instructions_are_opt_in_and_dynamic(self):
        faces = json.loads((CORTADOS/'faces.json').read_text())
        self.assertFalse(any('before it is called done' in r for r in faces['rules']))
        self.assertTrue(any('BOTH Samantha and Rachel' in r and 'opt-in' in r for r in faces['rules']))
        for role in ['samantha','rachel']:
            self.assertEqual(faces['faces'][role]['brain'], 'routed')
            self.assertNotIn('never review Gemini', faces['faces'][role]['identity'])


if __name__ == '__main__': unittest.main()
