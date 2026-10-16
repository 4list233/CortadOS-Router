# PURPOSE: Pin broker store integrity, transactional rollback, and terminal immutability.
# CONTRACT: RT-MCP-001 sections 6–7; builder regressions supplement protected exams.
# DEPENDS: Standard-library unittest and the production JobStore owner.
import asyncio
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from integrations.mcp.job_store import JobStore, failure_result
from integrations.mcp.schemas import WorkerError, validate


class JobStoreRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mcp-store-regression-')
        self.path = Path(self.temporary.name)
        self.config = {'client_id': 'test', 'state_dir': str(self.path),
                       'limits': {'max_concurrent_jobs': 1, 'max_timeout_s': 10}}
        self.store = JobStore(self.config, 'test-boot')
        self.events = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.events)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.store.close()
        self.temporary.cleanup()

    def request(self):
        return validate('delegate', {'task': 'Private task', 'selection': {'mode': 'explicit', 'model': 'gpt-6-luna'},
            'cwd': str(self.path), 'timeout_s': 10, 'request_id': str(uuid.uuid4())}, self.config)

    @staticmethod
    def route():
        return {'route_id': str(uuid.uuid4()), 'chosen': {'model': 'gpt-6-luna'}}

    def test_job_store_partial_claim_rolls_back_identity_workspace_and_capacity(self):
        request = self.request()
        self.store.db.execute("CREATE TRIGGER fail_insert AFTER INSERT ON jobs BEGIN SELECT RAISE(ABORT,'fault'); END")
        with self.assertRaises(WorkerError) as fault:
            self.store.accept(request, self.route())
        self.assertEqual(fault.exception.code, 'storage_error')
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.active(), [])
        self.assertIsNone(self.store.replay(request))
        self.assertEqual(self.events.getvalue(), '')
        self.store.db.execute('DROP TRIGGER fail_insert')
        accepted, created = self.store.accept(request, self.route())
        self.assertTrue(created)
        self.assertEqual(accepted['state'], 'queued')
        self.assertEqual(len(self.store.active()), 1)

    def test_job_store_uuid_and_request_identity_use_unique_indexes(self):
        request = self.request()
        record, _ = self.store.accept(request, self.route())
        for query, values in (
            ('SELECT record FROM jobs WHERE job_id=?', (record['job_id'],)),
            ('SELECT record FROM jobs WHERE client_id=? AND request_id=?', ('test', request['request_id']))):
            plan = ' '.join(str(row) for row in self.store.db.execute('EXPLAIN QUERY PLAN ' + query, values))
            self.assertIn('USING INDEX', plan)
            self.assertNotIn('SCAN jobs', plan)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute('INSERT INTO jobs SELECT * FROM jobs')

    def test_job_store_terminal_duration_and_result_cannot_change(self):
        accepted, _ = self.store.accept(self.request(), self.route())
        job_id = accepted['job_id']
        self.store.transition(job_id, 'running')
        with mock.patch('integrations.mcp.job_store.time.monotonic', return_value=accepted['accepted_monotonic'] + 4):
            finished = self.store.transition(job_id, 'failed', reason='native_failed',
                result=failure_result(accepted, 'native_failed', 'Native worker failed'))
        self.assertEqual(finished['elapsed_ms'], 4000)
        again = self.store.transition(job_id, 'cancelled', reason='cancel_requested')
        self.assertEqual(self.store.response(again, True), self.store.response(finished, True))
        events = [json.loads(line) for line in self.events.getvalue().splitlines()]
        self.assertEqual([event['state'] for event in events], ['queued', 'running', 'failed'])

    def test_job_store_first_stop_intent_is_durable(self):
        record, _ = self.store.accept(self.request(), self.route())
        first = self.store.transition(record['job_id'], 'cancelling', reason='timeout')
        repeated = self.store.transition(record['job_id'], 'cancelling', reason='cancel_requested')
        self.assertEqual(repeated['stop_intent'], 'timeout')
        self.assertEqual(first, repeated)

    def test_job_store_unknown_schema_refuses_without_migration(self):
        other = self.path / 'incompatible'
        other.mkdir()
        path = other / 'jobs.db'
        with sqlite3.connect(path) as database:
            database.execute('PRAGMA user_version=999')
        before = path.read_bytes()
        with self.assertRaises(WorkerError) as fault:
            JobStore(dict(self.config, state_dir=str(other)), 'test-boot')
        self.assertEqual(fault.exception.code, 'storage_error')
        self.assertEqual(path.read_bytes(), before)

    def test_job_store_unfinished_identity_never_replays_another_attempt(self):
        request = self.request()
        record, _ = self.store.accept(request, self.route())
        self.store.close()
        self.store = JobStore(self.config, 'test-boot')
        repeated, created = self.store.accept(request, self.route())
        self.assertFalse(created)
        self.assertEqual(repeated['job_id'], record['job_id'])
        self.assertEqual(repeated['launch_nonce'], record['launch_nonce'])


class NativeIdentityRegressions(unittest.TestCase):
    def test_process_identity_reuse_never_authorizes_a_signal(self):
        from integrations.mcp.native_process import ProcessTracker
        root = {'pid': 701, 'pgid': 701, 'creation_time_us': 1000}
        reused = dict(root, creation_time_us=2000)
        tracker = ProcessTracker(root)
        with mock.patch('integrations.mcp.native_process.process_table', return_value={701: (reused, 1)}), \
             mock.patch('integrations.mcp.native_process.os.killpg') as kill:
            tracker.signal_groups(signal.SIGKILL)
            self.assertFalse(tracker.contained())
        kill.assert_not_called()
        self.assertTrue(tracker.lost_ownership)

    def test_process_groups_signal_descendants_before_native_parent(self):
        from integrations.mcp.native_process import ProcessTracker
        root = {'pid': 701, 'pgid': 701, 'creation_time_us': 1000}
        child = {'pid': 702, 'pgid': 702, 'creation_time_us': 1100}
        tracker = ProcessTracker(root)
        table = {701: (root, 1), 702: (child, 701)}
        with mock.patch('integrations.mcp.native_process.process_table', return_value=table), \
             mock.patch('integrations.mcp.native_process.process_identity', side_effect=lambda pid: table[pid][0]), \
             mock.patch('integrations.mcp.native_process.os.killpg') as kill:
            tracker.signal_groups(signal.SIGTERM)
        self.assertEqual(kill.call_args_list, [mock.call(702, signal.SIGTERM), mock.call(701, signal.SIGTERM)])

    def test_natural_exit_between_inventory_and_signal_does_not_lose_ownership(self):
        from integrations.mcp.native_process import ProcessTracker
        root = {'pid': 701, 'pgid': 701, 'creation_time_us': 1000}
        tracker = ProcessTracker(root)
        with mock.patch('integrations.mcp.native_process.process_table', return_value={701: (root, 1)}), \
             mock.patch('integrations.mcp.native_process._child_ids', return_value=[]), \
             mock.patch('integrations.mcp.native_process.process_identity', return_value=None), \
             mock.patch('integrations.mcp.native_process.os.killpg') as kill:
            tracker.signal_groups(signal.SIGTERM)
        kill.assert_not_called()
        self.assertFalse(tracker.lost_ownership)

    def test_failed_process_observation_retains_claim_evidence_and_refuses_signals(self):
        from integrations.mcp.native_process import ProcessTracker
        root = {'pid': 701, 'pgid': 701, 'creation_time_us': 1000}
        tracker = ProcessTracker(root)
        with mock.patch('integrations.mcp.native_process.process_table', side_effect=PermissionError('denied')), \
             mock.patch('integrations.mcp.native_process.os.killpg') as kill:
            tracker.signal_groups(signal.SIGKILL)
            self.assertFalse(tracker.contained())
            self.assertEqual(tracker.remaining(), [root])
        kill.assert_not_called()
        self.assertTrue(tracker.lost_ownership)

    def test_watchdog_stop_and_control_loss_before_start_never_launch_native(self):
        from integrations.mcp.native_process import host_boot_id, validate_receipt
        # Ten seconds is a test stall ceiling, not a production startup policy.
        test_wait_s = 10
        stop = {'command': 'STOP', 'reason': 'cancel_requested'}
        for command, early in ((stop, False), (None, False), (stop, True)):
            with self.subTest(command=command, early=early), tempfile.TemporaryDirectory(prefix='mcp-no-start-') as directory:
                state = Path(directory)
                config = {'state_dir': str(state), 'codex_bin': sys.executable, 'codex_home': str(state),
                          'limits': {'process_poll_ms': 20, 'termination_grace_s': 1,
                                     'max_handoff_bytes': 65536, 'max_output_bytes': 1048576}}
                job_id, nonce, boot = str(uuid.uuid4()), str(uuid.uuid4()), host_boot_id()
                bootstrap = {'config': config, 'arguments': {'permissions': {'network': False, 'mcp_servers': []}},
                             'route': {'chosen': {'model': 'gpt-6-luna'}}, 'job_id': job_id,
                             'launch_nonce': nonce, 'host_boot_id': boot}
                process = subprocess.Popen([sys.executable, '-B', '-m', 'integrations.mcp.worker', '--state-dir', directory],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True)
                try:
                    initial = json.dumps(bootstrap).encode() + b'\n'
                    if early:
                        initial += json.dumps(command).encode() + b'\n'
                    process.stdin.write(initial)
                    process.stdin.flush()
                    with selectors.DefaultSelector() as poll:
                        poll.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(poll.select(test_wait_s), 'Watchdog failed to become ready')
                    ready = json.loads(process.stdout.readline())
                    self.assertEqual(ready['event'], 'ready')
                    payload = json.dumps(command).encode() + b'\n' if command and not early else b''
                    output, errors = process.communicate(payload, timeout=test_wait_s)
                    self.assertEqual(process.returncode, 0, errors.decode())
                    messages = [json.loads(line) for line in output.splitlines()]
                    self.assertEqual([message['event'] for message in messages], ['done'])
                    self.assertEqual(messages[0]['reason'], 'cancel_requested' if command else 'broker_interrupted')
                    receipt = json.loads((state / 'jobs' / job_id / 'termination.json').read_text())
                    self.assertTrue(validate_receipt(receipt, job_id, nonce, boot, ready['watchdog']))
                    self.assertIsNone(receipt['native'])
                    self.assertEqual(receipt['observed'], [])
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()


class LaunchAdmissionRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_refreshed_inventory_must_include_the_already_selected_model(self):
        from integrations.mcp.broker import Broker
        broker = object.__new__(Broker)
        broker.config, broker.metadata = {}, object()
        broker.admission_lock = asyncio.Lock()
        record = {'arguments': {}, 'route': {'chosen': {'model': 'gpt-6-luna'}}}
        with mock.patch('cortados_router.worker_admission.admit', return_value={'models': ('gpt-6-astra',)}):
            with self.assertRaises(WorkerError) as refused:
                await broker.admit_launch(record)
        self.assertEqual(refused.exception.code, 'model_unavailable')


if __name__ == '__main__':
    unittest.main()
