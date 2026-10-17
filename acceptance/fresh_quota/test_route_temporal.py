# PURPOSE: Pin quota invalidation during scorer and final resolver delays.
# CONTRACT: RT-FRESH-QUOTA-005 AC-FQ04; available advice requires a final check.
# DEPENDS: Actual route_task, worker_admission and NativeMetadata cache owner.
# CEILING: Deterministic metadata double; no scorer/model subprocess or live state.
from copy import deepcopy
import unittest
from unittest.mock import patch

from cortados_router import core, route_task
from integrations.mcp.native_metadata import NativeMetadata
from integrations.mcp.schemas import WorkerError
from tests import test_mcp_route_task as existing_fixture


class RouteTemporal(unittest.TestCase):
    def setUp(self):
        # Existing fixture builder supplies a private config and storage/log seams.
        existing_fixture.RouteContract.setUp(self)
        self.config['limits']['metadata_max_age_s'] = 30
        self.clock = 2000000000.0
        self.monotonic = 10000.0
        for p in (patch('time.time', side_effect=lambda: self.clock),
                  patch('time.time_ns', side_effect=lambda: int(self.clock * 1e9)),
                  patch('time.monotonic', side_effect=lambda: self.monotonic)):
            p.start()
            self.addCleanup(p.stop)
        self.actual = NativeMetadata(self.config)
        self.actual.version = '0.163.0'
        self.collections = 0
        self.spent_after = None
        self.actual._collect = self.collect
        self.request['selection'] = {'mode': 's1'}

    def collect(self):
        self.collections += 1
        row = existing_fixture.snapshot()
        if self.spent_after is not None and self.monotonic >= self.spent_after:
            row['rate_limits']['ordinaryUsageAllowed'] = False
        return row

    def advance(self):
        self.clock += 31
        self.monotonic += 31

    def expect_spent(self):
        with self.assertRaises(WorkerError) as failure:
            route_task.route_task(self.request, self.config, self.actual)
        self.assertEqual(failure.exception.code, 'quota_spent')
        self.log.assert_not_called()

    def test_scorer_delay_refreshes_before_resolution(self):
        self.spent_after = 10031
        def score(*args):
            self.advance()
            return existing_fixture.decision()
        with patch.object(core, 'decide', side_effect=score), patch.object(core, 'resolve') as resolve:
            self.expect_spent()
        resolve.assert_not_called()
        self.assertEqual(self.collections, 2)

    def test_last_reresolve_delay_cannot_return_old_available_advice(self):
        self.spent_after = 10062
        resolves = []
        def resolve(*args, **kwargs):
            resolves.append(kwargs.get('resources'))
            self.advance()
            return {'chosen': {'provider': 'codex-cli', 'lane': 'codex', 'model': 'gpt-6-luna'},
                    'band': 'R1', 'D': 1, 'subagents': 0, 'verifier': False, 'skipped': [], 'hard_rules': []}
        with patch.object(core, 'decide', return_value=existing_fixture.decision()), patch.object(core, 'resolve', side_effect=resolve):
            self.expect_spent()
        self.assertEqual(len(resolves), 2)
        self.assertEqual(self.collections, 3)
