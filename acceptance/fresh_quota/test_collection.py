# PURPOSE: Exercise collection deadlines, output bounds and shared in-flight observations.
# CONTRACT: RT-FRESH-QUOTA-005 AC-FQ07; real subprocesses, private filesystem only.
# DEPENDS: Public collect API, closed child protocol, standard Popen boundary.
# CEILING: Provider command replaced locally at process construction; no production bypass.
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from .support import EPOCH, ROOT, observation, scope


class Collection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fresh-quota-child-exam-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.log = self.home / 'children.jsonl'
        self.control_path = self.home / 'control.json'
        self.control = {'log': str(self.log), 'outputs': {}}
        for lane in ('codex', 'claude'):
            record = observation(lane)['record']
            record['windows'][0]['reset_epoch'] += time.time() - EPOCH
            self.control['outputs'][lane] = {'source': 'exam-child', 'auth_scope': scope(lane), 'record': record, 'error': None}
        self.config = {
            'schema': 'cortados.mcp-worker.config.v1', 'client_id': 'collection-exam',
            'state_dir': str(self.home), 'codex_home': str(self.home),
            'codex_bin': str(ROOT / 'acceptance/mcp_worker/fake_codex.py'),
            'model_allowlist': ['gpt-6.1-sol'], 'allow_network': False, 'connector_registry': {},
            'limits': {'max_concurrent_jobs': 2, 'max_timeout_s': 30, 'termination_grace_s': 1,
                       'startup_timeout_s': 1, 'max_handoff_bytes': 65536, 'max_output_bytes': 65536,
                       'metadata_max_age_s': 30, 'process_poll_ms': 20}}
        from cortados_router import quota_observations
        self.collector = importlib.reload(quota_observations)
        self.processes = []
        real_popen = subprocess.Popen
        def launch(argv, *args, **kwargs):
            if argv[0] == 'ps':
                return real_popen(argv, *args, **kwargs)
            self.assertEqual(list(argv)[1:5], ['-B', '-m', 'cortados_router.quota_observations', '--observe-lane'])
            self.assertEqual(len(argv), 6)
            fixture = Path(__file__).with_name('fixtures') / 'observation_child.py'
            process = real_popen([sys.executable, '-B', str(fixture), argv[-1], str(self.control_path)], *args, **kwargs)
            self.processes.append(process)
            return process
        self.launch = self.enterContext(patch.object(subprocess, 'Popen', side_effect=launch))
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)

    def call(self, lanes=('codex',)):
        self.control_path.write_text(json.dumps(self.control))
        return self.collector.collect(lanes, config=self.config)

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_deduplicated_lexical_lanes_keep_their_probe_start_times(self):
        self.control['delays'] = {'codex': 0.4}
        started = time.time()
        result = self.call(('codex', 'claude', 'codex'))
        finished = time.time()
        self.assertEqual([row['lane'] for row in result], ['claude', 'codex'])
        self.assertEqual(self.launch.call_count, 2)
        for row in result:
            self.assertIsNone(row['error'])
            self.assertGreaterEqual(row['observed_at'], started)
            self.assertLess(row['observed_at'], finished - 0.25,
                            'batch completion must not replace lane probe-start observation')
        for child in self.rows():
            self.assertEqual(child['pgid'], child['pid'], 'probe must own its process group')
        starts = [row['started'] for row in self.rows()]
        self.assertLess(max(starts) - min(starts), 0.3, 'lanes must launch concurrently')

    def test_simultaneous_callers_share_one_inflight_lane_probe(self):
        self.control['delays'] = {'codex': 0.3}
        self.control_path.write_text(json.dumps(self.control))
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(self.collector.collect, ('codex',), config=self.config) for _ in range(2)]
            results = [future.result(timeout=5) for future in pending]
        self.assertEqual(self.launch.call_count, 1)
        self.assertEqual(results[0], results[1])

    def test_deadline_reaps_probe_and_descendant(self):
        self.control['mode'] = 'hang'
        started = time.monotonic()
        result = self.call()
        # 1s collection + 1s configured cleanup grace + 3s scheduler allowance.
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result[0]['error']['code'], 'quota_unknown')
        for row in self.rows():
            pid = row.get('child_pid', row.get('pid'))
            proc = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
            self.assertTrue(proc.returncode or not proc.stdout.strip() or proc.stdout.strip().startswith('Z'),
                            'expired probe group retained a runnable process')

    def test_stdout_or_stderr_overflow_is_bounded_unknown(self):
        for mode in ('overflow', 'stderr-overflow'):
            with self.subTest(mode=mode):
                importlib.reload(self.collector)
                self.control['mode'] = mode
                started = time.monotonic()
                result = self.call()
                self.assertEqual(result[0]['error']['code'], 'quota_unknown')
                self.assertLess(time.monotonic() - started, 5)

    def test_invalid_extra_or_failed_child_output_is_unknown(self):
        for mode in ('invalid', 'extra-json', 'exit-error'):
            with self.subTest(mode=mode):
                importlib.reload(self.collector)
                self.control['mode'] = mode
                self.assertEqual(self.call()[0]['error']['code'], 'quota_unknown')

    def test_child_cannot_supply_authoritative_timestamps(self):
        self.control['outputs']['codex']['observed_at'] = time.time()
        self.assertEqual(self.call()[0]['error']['code'], 'quota_unknown')

    def test_invalid_lane_refuses_before_process_creation(self):
        with self.assertRaises(ValueError):
            self.call(('unknown-provider',))
        self.launch.assert_not_called()

    def test_parent_never_calls_shared_sample_or_learner(self):
        from cortados_router import scarcity
        with patch.object(scarcity, 'sample', side_effect=AssertionError('shared sample forbidden')):
            result = self.call()
        self.assertIsNone(result[0]['error'])
        self.assertFalse((self.home / 'scarcity.json').exists())
        self.assertFalse((self.home / 'unit-cost.json').exists())
        self.assertFalse((self.home / 'scarcity-history.jsonl').exists())

    def test_changed_source_configuration_cannot_reuse_lane_cache(self):
        self.call()
        self.config['codex_home'] = str(self.home / 'different-auth-store')
        self.call()
        self.assertEqual(self.launch.call_count, 2)

    def test_expired_success_followed_by_failed_refresh_never_reuses_old_record(self):
        self.config['limits']['metadata_max_age_s'] = 1
        self.assertIsNone(self.call()[0]['error'])
        time.sleep(1.05)  # Cross the configured one-second cache age, with 50ms scheduling margin.
        self.control['mode'] = 'exit-error'
        result = self.call()
        self.assertEqual(self.launch.call_count, 2)
        self.assertIsNone(result[0]['record'])
        self.assertEqual(result[0]['error']['code'], 'quota_unknown')

    def test_empty_lane_set_has_no_observation_io(self):
        self.assertEqual(self.call(()), ())
        self.launch.assert_not_called()
