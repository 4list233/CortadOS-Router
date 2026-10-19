# PURPOSE: Pin actual monitor lifetime, fatal signaling and Broker containment.
# CONTRACT: RT-COWORK-MCP-008 v6 R093–102; AC03–06,15,16.
# DEPENDS: Real runtime/IPC/store/Broker; prior fake native only at process boundary.
# CEILING: Private synthetic state; no real provider, credentials or live operations.
import asyncio
from contextlib import ExitStack
import json
import sqlite3
from unittest.mock import patch
from .support import (CoreCase, FatalCapture, event_barrier, lose_monitor, mod,
                      monitor_fixture, remote_counts, uid, wait_for_fixture)


class MonitorLifetimeExam(CoreCase):
    async def outcome(self, mode, disabled=False, capacity=False):
        gate = await monitor_fixture(self, mode)
        signal = FatalCapture(self)
        if disabled:
            from .support import owner
            await self.runtime.disable_owner(owner(self.fixture.principal))
        if capacity:
            self.runtime.fail_capacity()
        before = remote_counts(self)
        await lose_monitor(self, gate)
        self.assertTrue(self.runtime.fatal_failed, 'Monitor loss must latch without another request')
        self.assertTrue(self.runtime.broker.store.admission_closed)
        signal.assert_one(self)
        self.assertEqual(remote_counts(self), before)
        await event_barrier()
        signal.assert_one(self)

    async def test_unexpected_return_signals_without_followup(self):
        await self.outcome('return')

    async def test_unexpected_exception_is_consumed_and_signals(self):
        await self.outcome('exception')

    async def test_external_cancellation_is_not_owned_shutdown(self):
        await self.outcome('cancel')

    async def test_disabled_owner_still_requires_monitor(self):
        await self.outcome('return', disabled=True)

    async def test_capacity_only_state_does_not_excuse_monitor_loss(self):
        await self.outcome('exception', capacity=True)

    async def test_admission_checks_done_task_before_queued_observer(self):
        gate = await monitor_fixture(self)
        signal = FatalCapture(self)
        observed = {}
        finished = asyncio.get_running_loop().create_future()
        def admission():
            observed['done'] = self.runtime.monitor_task.done()
            observed['signals_before'] = len(signal.attempts)
            try:
                self.runtime.authorize(self.runtime.config.principal, 'task_create')
                observed['allowed'] = True
            except mod('remote_schemas').RemoteError as error:
                observed['code'] = error.code
            finished.set_result(None)
        gate.release.set()
        asyncio.get_running_loop().call_soon(admission)
        await finished
        self.assertTrue(observed['done'])
        self.assertEqual(observed['signals_before'], 0, 'Probe must precede the queued callback')
        self.assertEqual(observed.get('code'), 'storage_error')
        self.assertNotIn('allowed', observed)
        await event_barrier()
        signal.assert_one(self)
        self.error(await self.call('task_create', {'request_id': uid(), 'task_group_id': uid()}),
                   'storage_error')
        self.assertEqual(remote_counts(self)['remote_tasks'], 0)

    async def test_healthy_monitor_and_unknown_lookups_do_not_latch(self):
        await monitor_fixture(self)
        task = await self.task()
        self.error(await self.call('task_get', {'task_id': uid()}), 'task_not_found')
        self.error(await self.call('job_result', {'task_id': task['task_id'], 'job_id': uid()}),
                   'job_not_found')
        self.error(await self.call('task_create', {'request_id': uid()}), 'invalid_request')
        self.assertFalse(self.runtime.fatal_failed)
        self.assertFalse(self.runtime.monitor_task.done())
        await self.task()

    async def test_owned_shutdown_cancellation_emits_no_false_fatal(self):
        gate = await monitor_fixture(self)
        signal = FatalCapture(self)
        task = self.runtime.monitor_task
        await self.stack.aclose()
        self.assertTrue(task.done())
        self.assertFalse(gate.ready_at_stop, 'Admission closes before owned cancellation')
        self.assertFalse(signal.attempts)
        self.assertFalse(self.runtime.fatal_failed)
        self.assertIsNone(self.runtime.lock_fd)

    async def test_prior_terminal_loss_is_not_reclassified_by_late_shutdown(self):
        gate = await monitor_fixture(self)
        signal = FatalCapture(self)
        gate.release.set()
        # Same-task await resumes before task completion callbacks queued afterward.
        await gate.finished.wait()
        self.assertTrue(self.runtime.monitor_task.done())
        await self.stack.aclose()
        self.assertTrue(self.runtime.fatal_failed)
        signal.assert_one(self)

    async def test_signal_sink_failure_is_safe_once_and_surfaces_at_teardown(self):
        gate = await monitor_fixture(self, 'exception')
        signal = FatalCapture(self, fail=True)
        loop = asyncio.get_running_loop()
        prior = loop.get_exception_handler()
        callbacks = []
        loop.set_exception_handler(lambda _, context: callbacks.append(context))
        self.addCleanup(loop.set_exception_handler, prior)
        await lose_monitor(self, gate)
        self.assertTrue(self.runtime.fatal_failed)
        self.assertEqual(len(signal.attempts), 1)
        self.assertFalse(callbacks, 'Raw observer exception must not escape to event-loop handler')
        with self.assertRaises(mod('remote_schemas').RemoteError) as raised:
            await self.stack.aclose()
        self.assertEqual(raised.exception.code, 'storage_error')
        self.assertNotIn('SENTINEL', str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__ or raised.exception.__context__ is None)
        self.assertEqual(len(signal.attempts), 1)
        self.assertFalse(callbacks)

    async def test_partial_startup_unwind_owns_monitor_stop(self):
        original = mod('remote_schemas').emit_remote_event
        signal = FatalCapture(self)
        def fail_ready(tx, event):
            if event.entity == 'runtime' and event.state == 'ready':
                raise sqlite3.OperationalError('SYNTHETIC startup fault')
            return original(tx, event)
        with patch.object(mod('remote_schemas'), 'emit_remote_event', fail_ready):
            with self.assertRaises(mod('remote_schemas').RemoteError):
                await self.start()
        self.assertFalse(any(call.get('kind') == 'exec' for call in self.fixture.calls()))
        # A fresh real owner can take the same lock; no leaked partial startup.
        await monitor_fixture(self)
        await self.task()


class CanonicalMonitorExam(CoreCase):
    async def sample_case(self, payload):
        self.fixture.core['limits']['workspace_poll_ms'] = 200
        await self.start()
        task = await self.task()
        signal = FatalCapture(self)
        sampled = asyncio.Event()
        original = self.runtime.capacity.measure
        def measure():
            try:
                return original()
            finally:
                sampled.set()
        with self.runtime.broker.store.transaction() as tx:
            tx.execute('UPDATE remote_tasks SET policy_json=? WHERE task_id=?',
                       (payload, task['task_id']))
        with patch.object(self.runtime.capacity, 'measure', measure):
            await asyncio.wait_for(sampled.wait(), 2)
            await event_barrier()
        return signal

    async def test_missing_nested_policy_stops_actual_monitor_before_unrelated_ipc_effect(self):
        signal = await self.sample_case('{}')
        self.assertTrue(self.runtime.fatal_failed)
        before = remote_counts(self)
        self.error(await self.call('task_create', {'request_id': uid(), 'task_group_id': uid()}),
                   'storage_error')
        self.assertEqual(remote_counts(self), before)
        signal.assert_one(self)

    async def test_malformed_scalar_policy_control_is_fatal(self):
        signal = await self.sample_case('0')
        self.assertTrue(self.runtime.fatal_failed)
        signal.assert_one(self)

    async def test_valid_policy_control_keeps_actual_monitor_and_admission(self):
        self.fixture.core['limits']['workspace_poll_ms'] = 200
        await self.start()
        task = await self.task()
        observed = asyncio.Event()
        original = self.runtime.capacity.measure
        def measure():
            result = original()
            observed.set()
            return result
        with patch.object(self.runtime.capacity, 'measure', measure):
            await asyncio.wait_for(observed.wait(), 2)
        self.assertFalse(self.runtime.fatal_failed)
        self.assertFalse(self.runtime.monitor_task.done())
        self.ok(await self.call('task_get', {'task_id': task['task_id']}))
        await self.task()


class MonitorContainmentExam(CoreCase):
    async def running_worker(self):
        self.fixture.control(hang=True)
        gate = await monitor_fixture(self)
        task = await self.task()
        job = self.ok(await self.call('delegate', self.args(task)))['job']['job_id']
        await wait_for_fixture(lambda: any(c.get('kind') == 'exec' for c in self.fixture.calls()))
        # Always release the synthetic worker even when the red assertion aborts early.
        return gate, job

    async def containment(self, sink_failure=False, unreadable=False, stop_write_failure=False):
        gate, job = await self.running_worker()
        signal = FatalCapture(self, fail=sink_failure)
        broker = self.runtime.broker
        original_send = broker.send
        sent = asyncio.Event()
        commands = []
        async def observe_send(process, command):
            if command.get('command') == 'STOP':
                commands.append(dict(command))
                sent.set()
            return await original_send(process, command)
        faults = ExitStack()
        faults.enter_context(patch.object(broker, 'send', observe_send))
        if unreadable:
            faults.enter_context(patch.object(broker.store, 'active', side_effect=
                sqlite3.DatabaseError('SYNTHETIC active enumeration failure')))
        if stop_write_failure:
            transition = broker.store.transition
            def fail_stop(job_id, state, **kwargs):
                if state == 'cancelling':
                    raise sqlite3.DatabaseError('SYNTHETIC stop-intent write failure')
                return transition(job_id, state, **kwargs)
            faults.enter_context(patch.object(broker.store, 'transition', fail_stop))
        try:
            await lose_monitor(self, gate)
            # This observes the existing stop path, never supplies an extra native grace.
            waiter = asyncio.create_task(sent.wait())
            done, pending = await asyncio.wait({waiter}, timeout=2)
            for task in pending:
                task.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            self.assertTrue(sent.is_set(), 'Monitor loss must initiate Broker STOP without later traffic')
            self.assertEqual(commands[0]['reason'], 'capacity_exhausted')
            await wait_for_fixture(lambda: all(task.done() for task in broker.tasks))
            record = broker.store.get(job)
            self.assertTrue(broker.receipt(record), 'STOP intent is not native death proof')
            if stop_write_failure:
                self.assertIsNone(record['stop_intent'], 'Failed persistence must not fabricate intent')
                self.assertNotEqual((record['state'], record['reason']), ('cancelled', 'capacity_exhausted'))
            else:
                self.assertEqual(record['stop_intent'], 'capacity_exhausted')
                self.assertEqual((record['state'], record['reason']), ('cancelled', 'capacity_exhausted'))
            self.assertTrue(self.runtime.fatal_failed)
            self.assertEqual(sum(c.get('kind') == 'exec' for c in self.fixture.calls()), 1)
            self.assertEqual(len(signal.attempts), 1)
            command_count = len(commands)
            for _ in range(2):
                with self.assertRaises(mod('remote_schemas').RemoteError) as raised:
                    self.runtime.authorize(self.runtime.config.principal, 'task_create')
                self.assertEqual(raised.exception.code, 'storage_error')
            await event_barrier()
            self.assertEqual(len(commands), command_count, 'Repeated detection cannot resend STOP')
            self.assertEqual(len(signal.attempts), 1)
        finally:
            (self.fixture.path / 'native/release-worker').touch()
            faults.close()
            signal.fail = False
            if self.runtime.fatal_signal_failed:
                with self.assertRaises(mod('remote_schemas').RemoteError) as raised:
                    await self.stack.aclose()
                self.assertEqual(raised.exception.code, 'storage_error')
                self.assertNotIn('SENTINEL', str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__ or
                                raised.exception.__context__ is None)

    async def test_monitor_loss_stops_running_worker_and_requires_actual_receipt(self):
        await self.containment()

    async def test_sink_failure_does_not_prevent_owned_worker_stop(self):
        await self.containment(sink_failure=True)

    async def test_active_record_enumeration_failure_still_stops_known_worker(self):
        await self.containment(unreadable=True)

    async def test_stop_intent_write_failure_still_stops_without_fabricated_intent(self):
        await self.containment(stop_write_failure=True)


    async def test_unavailable_receipt_retains_lock_roots_and_reservations(self):
        import os
        gate, job = await self.running_worker()
        broker = self.runtime.broker
        receipt = broker.receipt
        roots = dict(self.runtime.root_fds)
        lock = self.runtime.lock_fd
        before = self.fixture.sql('SELECT * FROM remote_reservations')
        sent = asyncio.Event()
        send = broker.send
        async def observed_send(process, command):
            if command.get('command') == 'STOP':
                sent.set()
            return await send(process, command)
        proof_fault = patch.object(broker, 'receipt', return_value=False)
        proof_fault.start()
        closed_stack = False
        try:
            with patch.object(broker, 'send', observed_send):
                await lose_monitor(self, gate)
                waiter = asyncio.create_task(sent.wait())
                done, pending = await asyncio.wait({waiter}, timeout=2)
                for task in pending:
                    task.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                self.assertTrue(sent.is_set(), 'Unknown receipt does not suppress STOP')
                await wait_for_fixture(lambda: receipt(broker.store.get(job)))
            self.assertFalse(broker.receipt(broker.store.get(job)))
            completed = asyncio.get_running_loop().create_future()
            async def observe_retention():
                # 250ms is bounded test observation only. asyncio.wait leaves the
                # actual owner continuation alive; it is not native stop grace.
                await asyncio.wait({completed}, timeout=.25)
                try:
                    self.assertEqual(self.runtime.lock_fd, lock)
                    os.fstat(lock)
                    for name, fd in roots.items():
                        self.assertEqual(self.runtime.root_fds[name], fd)
                        os.fstat(fd)
                    self.assertEqual(self.fixture.sql('SELECT * FROM remote_reservations'), before)
                    self.assertTrue((self.fixture.path / 'tasks').exists())
                finally:
                    proof_fault.stop()
            observer = asyncio.create_task(observe_retention())
            closed_stack = True
            try:
                # Only after retained-resource assertions does the observer restore
                # real proof. Five seconds bounds fixture cleanup, not product lifetime.
                async with asyncio.timeout(5):
                    await self.stack.aclose()
            except mod('remote_schemas').RemoteError as raised:
                self.assertEqual(raised.code, 'containment_failed')
            finally:
                completed.set_result(None)
                await observer
        finally:
            proof_fault.stop()
            (self.fixture.path / 'native/release-worker').touch()
            # Only the artificial proof-observation fault is restored. The actual
            # prior fake worker must finish and produce its real receipt first.
            await wait_for_fixture(lambda: all(task.done() for task in broker.tasks))
            self.assertTrue(receipt(broker.store.get(job)), 'Fixture cleanup requires real death proof')
            if closed_stack and self.runtime.lock_fd is not None:
                await self.runtime.__aexit__(None, None, None)

    async def test_read_observer_and_admission_share_one_signal_attempt(self):
        gate = await monitor_fixture(self)
        task = await self.task()
        signal = FatalCapture(self)
        db = self.runtime.broker.store.db
        original = db.execute('SELECT policy_json FROM remote_tasks').fetchone()[0]
        db.execute('UPDATE remote_tasks SET policy_json=?', ('0',))
        try:
            with self.assertRaises(mod('remote_schemas').RemoteError):
                mod('task_store').rows(db, 'remote_tasks')
            await lose_monitor(self, gate)
            with self.assertRaises(mod('remote_schemas').RemoteError) as raised:
                self.runtime.authorize(self.runtime.config.principal, 'task_create')
            self.assertEqual(raised.exception.code, 'storage_error')
            await event_barrier()
            signal.assert_one(self)
        finally:
            db.execute('UPDATE remote_tasks SET policy_json=?', (original,))


    async def test_prior_stop_reason_and_terminal_outcome_are_preserved(self):
        gate, job = await self.running_worker()
        try:
            await self.runtime.broker.stop(job, 'cancel_requested')
            await wait_for_fixture(lambda: all(task.done() for task in self.runtime.broker.tasks))
            before = self.runtime.broker.store.get(job)
            self.assertEqual((before['state'], before['stop_intent']), ('cancelled', 'cancel_requested'))
            self.assertTrue(self.runtime.broker.receipt(before))
            signal = FatalCapture(self)
            await lose_monitor(self, gate)
            self.assertEqual(self.runtime.broker.store.get(job), before)
            self.assertEqual(sum(c.get('kind') == 'exec' for c in self.fixture.calls()), 1)
            signal.assert_one(self)
        finally:
            (self.fixture.path / 'native/release-worker').touch()
