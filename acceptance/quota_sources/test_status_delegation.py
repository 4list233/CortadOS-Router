# PURPOSE: Normal operator Cursor status delegates to the same bounded native owner in both trees.
# CONTRACT: RT-QUOTA-SOURCES-006 AC-QS01/03/07; no old TUI/date reconstruction fallback.
from contextlib import ExitStack
import importlib
import json
import sys
from unittest.mock import patch
import unittest

from acceptance.fresh_quota.stage_support import StageCase, load


class StandaloneStatusDelegation(unittest.TestCase):
    def test_cursor_status_callsite_passes_exact_environment_to_bounded_owner(self):
        from cortados_router import scarcity, quota_observations
        from integrations.mcp import config
        environment = {'HOME': '/fixture/home', 'AGENT_CLI_CREDENTIAL_STORE': 'file'}
        record = {'source': 'native fixture', 'windows': []}
        normalized = {'fixture': 'normalized-config-owner'}
        with ExitStack() as guards:
            guards.enter_context(patch.object(config, 'load_config', return_value=normalized))
            if hasattr(scarcity, 'load_config'):
                guards.enter_context(patch.object(scarcity, 'load_config', return_value=normalized))
            status = guards.enter_context(patch.object(quota_observations, 'status_record', return_value=record, create=True))
            guards.enter_context(patch.object(scarcity, 'tui_capture', side_effect=AssertionError('Old Cursor TUI must not run')))
            result = scarcity._cursor(environment=environment)
        status.assert_called_once_with('cursor', config=normalized, environment=environment)
        self.assertEqual(result, record)


class SharedStatusDelegation(StageCase):
    def test_shared_status_bridge_and_scarcity_use_installed_bounded_owner(self):
        source = self.install / 'cortados_router/quota_observations.py'
        source.write_text("def status_record(lane, *, config, environment=None):\n return {'source':'native fixture','windows':[]}\n")
        owner = importlib.import_module('cortados_router.quota_observations')
        sys.path.insert(0, str(self.root / 'plugins/cortados-router'))
        bridge = load('quota_sources_fixture_status_bridge', self.root / 'plugins/cortados-router/quota_policy.py')
        scarcity = load('quota_sources_fixture_shared_scarcity', self.root / 'plugins/cortados-router/scarcity.py')
        with patch.object(owner, 'status_record', wraps=owner.status_record) as status, \
             patch.object(scarcity, 'tui_capture', side_effect=AssertionError('Old shared Cursor TUI must not run')):
            direct = bridge.cursor_status()
            routed = scarcity._cursor()
        self.assertEqual(direct, {'source': 'native fixture', 'windows': []})
        self.assertEqual(routed, direct)
        self.assertEqual(status.call_count, 2)
        for call in status.call_args_list:
            self.assertEqual(call.args, ('cursor',))
            self.assertEqual(call.kwargs['config'], json.loads(self.config_path.read_text()))
