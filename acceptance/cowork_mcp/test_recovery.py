# PURPOSE: Pin sole-writer lifetime and deterministic actual-process crash recovery.
# CONTRACT: RT-COWORK-MCP-008 AC07,08,13,15. DEPENDS: public API plus actual journal rows.
# CEILING: Private synthetic processes/SQLite only; no production state.
import asyncio
import json
import multiprocessing
import socket
import sqlite3
from unittest.mock import patch
from .support import CoreCase,canon,mod,runtime_child,uid

class RecoveryExam(CoreCase):
    async def test_second_runtime_refuses_before_second_jobstore(self):
        await self.start();f=self.fixture
        with patch.object(mod('job_store'),'JobStore',side_effect=AssertionError('Second DB constructor reached')):
            with self.assertRaises(mod('remote_schemas').RemoteError) as error:
                async with mod('remote_runtime').CoreRuntime(base_raw=canon(f.base),core_raw=canon(f.core),manifest_raw=canon(f.manifest)):pass
        self.assertEqual(error.exception.code,'storage_error')
    async def test_invalid_core_refuses_before_db_and_native_preflight(self):
        f=self.fixture;bad=dict(f.core,unexpected=True)
        with patch.object(mod('job_store'),'JobStore',side_effect=AssertionError('DB reached')),patch.object(mod('native_metadata'),'NativeMetadata',side_effect=AssertionError('Native reached')):
            with self.assertRaises(mod('remote_schemas').RemoteError):
                async with mod('remote_runtime').CoreRuntime(base_raw=canon(f.base),core_raw=canon(bad),manifest_raw=canon(f.manifest)):pass
        self.assertFalse((f.path/'state/jobs.db').exists())
    async def test_accept_binding_failure_rolls_back_all_accepted_rows(self):
        await self.start();task=await self.task();module=mod('task_store');original=module.accept_binding
        def injected(*args,**kwargs):
            original(*args,**kwargs)
            raise mod('remote_schemas').RemoteError('storage_error','Remote operation refused: storage_error')
        with patch.object(module,'accept_binding',new=injected):
            self.assertFalse((await self.call('delegate',self.args(task)))['ok'])
        for table in ('jobs','remote_job_bindings','remote_publications','remote_reservations'):
            self.assertEqual(self.fixture.sql('SELECT count(*) FROM '+table)[0][0],0,table)
        self.assertEqual(self.fixture.sql("SELECT count(*) FROM remote_operations WHERE operation='delegate'")[0][0],0)
        self.assertEqual(self.ok(await self.call('task_get',{'task_id':task['task_id']}))['task']['state'],'ready')
    async def test_restart_revokes_ticket_without_erasing_blob_or_operation(self):
        await self.start();task=await self.task();artifact,_=await self.upload(task['task_id'],b'preserve')
        ticket=self.ok(await self.call('file_download_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']}))['ticket']
        before=self.fixture.sql('SELECT count(*) FROM remote_operations')[0][0]
        await self.stack.aclose();await self.start()
        self.assertFalse((await self.stream('open_get',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:]}))['result']['ok'])
        self.assertEqual(self.fixture.sql('SELECT count(*) FROM remote_operations')[0][0],before)
        data,_=await self.download(task['task_id'],artifact['artifact_id']);self.assertEqual(data,b'preserve')
    async def test_corrupt_remote_schema_refuses_without_migration_repair(self):
        await self.start();await self.stack.aclose();db=sqlite3.connect(self.fixture.path/'state/jobs.db')
        db.execute('PRAGMA ignore_check_constraints=ON');db.execute('UPDATE remote_schema SET version=999');db.commit();db.close()
        with self.assertRaises(mod('remote_schemas').RemoteError):await self.start()
        self.assertEqual(self.fixture.sql('SELECT version FROM remote_schema')[0][0],999)
    async def test_missing_sealed_blob_quarantines_instead_of_serving_stale_success(self):
        await self.start();task=await self.task();artifact,_=await self.upload(task['task_id'],b'original')
        await self.stack.aclose()
        blobs=[p for p in (self.fixture.path/'blobs').rglob('*') if p.is_file()]
        self.assertEqual(len(blobs),1);blobs[0].unlink()
        await self.start();result=await self.call('file_download_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']})
        if result['ok']:
            ticket=result['ticket'];result=(await self.stream('open_get',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:]}))['result']
        self.assertFalse(result['ok']);self.assertEqual(result['error']['code'],'artifact_corrupt')
    async def crash_preparation(self,fault):
        f=self.fixture;ctx=multiprocessing.get_context('spawn');left,right=socket.socketpair();right.setblocking(False)
        parent,child=ctx.Pipe();process=ctx.Process(target=runtime_child,args=(canon(f.base),canon(f.core),canon(f.manifest),left,child,fault))
        process.start();left.close();child.close();self.addCleanup(right.close);self.addCleanup(parent.close)
        async def clean():
            if process.is_alive():process.kill()
            await asyncio.to_thread(process.join,5)
        self.addAsyncCleanup(clean)
        async with asyncio.timeout(10):ready=await asyncio.to_thread(parent.recv)
        self.assertTrue(ready.get('ready'),ready)
        client=mod('remote_ipc').CoreClient(right,principal=f.typed('Principal',f.principal),config_digest=f.digest(),limits=f.typed('CoreLimits',f.core['limits']))
        await client.bind();self.client=client;task=await self.task();args=self.args(task)
        with self.assertRaises(mod('remote_schemas').RemoteError):await self.call('delegate',args)
        async with asyncio.timeout(5):evidence=await asyncio.to_thread(parent.recv)
        self.assertEqual(evidence,{'fault':fault});await asyncio.to_thread(process.join,5);self.assertEqual(process.exitcode,73)
        self.assertEqual(f.sql('SELECT phase FROM remote_preparations'),[('intent',)])
        self.assertEqual(f.sql('SELECT count(*) FROM jobs')[0][0],0)
        await client.close();await self.start()
        return task,args
    async def test_crash_before_mkdir_retires_intent_and_allows_explicit_retry(self):
        task,args=await self.crash_preparation('before_prepare')
        self.assertEqual(self.fixture.sql('SELECT phase FROM remote_preparations'),[('retired',)])
        job=self.ok(await self.call('delegate',args))['job'];await self.terminal(task['task_id'],job['job_id'])
        self.assertEqual(len([r for r in self.fixture.calls() if r['kind']=='exec']),1)
    async def test_crash_after_mkdir_before_inode_record_quarantines_never_infers(self):
        task,args=await self.crash_preparation('after_prepare')
        self.assertEqual(self.fixture.sql('SELECT phase FROM remote_preparations'),[('quarantined',)])
        self.assertEqual(self.ok(await self.call('task_get',{'task_id':task['task_id']}))['task']['state'],'quarantined')
        self.error(await self.call('delegate',args),'task_quarantined')
        self.assertFalse([r for r in self.fixture.calls() if r['kind']=='exec'])
    async def test_sealed_blob_symlink_swap_never_reads_outside_bytes(self):
        await self.start();task=await self.task();artifact,_=await self.upload(task['task_id'],b'original')
        outside=self.fixture.path/'outside';outside.write_bytes(b'SENTINEL-OUTSIDE')
        blobs=[p for p in (self.fixture.path/'blobs').rglob('*') if p.is_file()]
        self.assertEqual(len(blobs),1);blobs[0].unlink();blobs[0].symlink_to(outside)
        result=await self.call('file_download_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']})
        if result['ok']:
            ticket=result['ticket'];frame=await self.stream('open_get',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:]})
            self.assertEqual(frame['body'],b'');result=frame['result']
        self.assertFalse(result['ok']);self.assertEqual(outside.read_bytes(),b'SENTINEL-OUTSIDE')


class BuildIntegrityRegressionExam(CoreCase):
    async def test_binding_derived_task_index_must_equal_immutable_seed(self):
        await self.start();first=await self.task();other=await self.task()
        job=self.ok(await self.call('delegate',self.args(first)))['job']['job_id']
        await self.terminal(first['task_id'],job)
        self.runtime.broker.store.db.execute('UPDATE remote_job_bindings SET task_id=? WHERE job_id=?',(other['task_id'],job))
        await self.stack.aclose()
        f=self.fixture
        with self.assertRaises(mod('remote_schemas').RemoteError) as caught:
            async with mod('remote_runtime').CoreRuntime(base_raw=canon(f.base),core_raw=canon(f.core),manifest_raw=canon(f.manifest)):
                pass
        self.assertEqual(caught.exception.code,'policy_unavailable')

    async def test_remote_job_audit_failure_latches_admission_and_emits_fatal_signal(self):
        import asyncio,contextlib,io,sqlite3
        from unittest.mock import patch
        from .support import uid
        from integrations.mcp.schemas import WorkerError
        await self.start();task=await self.task()
        async def no_native(record):pass
        with patch.object(self.runtime.broker,'run_worker',new=no_native):
            job=self.ok(await self.call('delegate',self.args(task)))['job']['job_id']
            await asyncio.sleep(.01)
        stderr=io.StringIO();api=mod('remote_schemas');original=api.emit_remote_event
        def failing(tx,event):
            if tx is not None and event.entity=='job':raise sqlite3.OperationalError('SENTINEL audit unavailable')
            return original(tx,event)
        with contextlib.redirect_stderr(stderr),patch.object(api,'emit_remote_event',new=failing):
            with self.assertRaises(WorkerError):self.runtime.broker.store.transition(job,'failed',reason='native_failed')
        response=await self.call('task_create',{'request_id':uid(),'task_group_id':uid(),'title':'must refuse'})
        self.error(response,'storage_error')
        self.assertTrue(self.runtime.admission_failed or self.runtime.broker.store.admission_closed)
        self.assertEqual(self.runtime.broker.store.get(job)['state'],'queued')
        events=[json.loads(line) for line in stderr.getvalue().splitlines() if line.startswith('{')]
        failures=[event for event in events if event.get('schema')=='cortados.cowork-event.v1' and event.get('reason')=='storage_error']
        self.assertEqual(len(failures),1,'One fixed fatal audit signal belongs to the failed transition')
        self.assertNotIn('SENTINEL',stderr.getvalue())

    async def test_runtime_ready_and_disable_each_have_one_durable_transition(self):
        from .support import owner
        await self.start();await self.runtime.disable_owner(owner(self.fixture.principal))
        events=[json.loads(row[0]) for row in self.fixture.sql('SELECT event_json FROM remote_audit')]
        for state in ('ready','disabled'):
            self.assertEqual(sum(event['entity']=='runtime' and event['event']=='transition' and event['state']==state for event in events),1,state)

    async def test_missed_monitor_deadline_closes_admission_after_scheduler_stall(self):
        import asyncio,time
        self.fixture.core['limits']['workspace_poll_ms']=50
        await self.start();await asyncio.sleep(.06)
        # Milliseconds: 250ms deliberately spans five50ms deadlines; this is a
        # synthetic scheduler stall, not an allowed jitter or production default.
        time.sleep(.25);await asyncio.sleep(.03)
        self.assertTrue(self.runtime.admission_failed,'Elapsed gap must be checked from prior sample start')

    async def test_one_cleanup_integrity_failure_cannot_leave_another_descriptor_open(self):
        import os,asyncio
        await self.start();opened=[]
        try:
            for index in range(2):
                task=await self.task();ticket=await self.ticket(task['task_id'],b'x')
                result=await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':1})
                lease=self.ok(result['result'])['lease_id'];opened.append((task,ticket,lease,self.runtime.descriptors[lease]))
            task,ticket,lease,_=opened[0];path=self.fixture.path/'staging'/task['task_id']/ticket['artifact_id']
            path.rename(path.with_suffix('.original'));path.write_bytes(b'unknown replacement')
            import contextlib,io
            signal=io.StringIO();terminal_error=None
            with contextlib.redirect_stderr(signal):
                try:await self.stack.aclose()
                except mod('remote_schemas').RemoteError as error:terminal_error=error
            # A failed cleanup must surface even when all descriptors were retired.
            events=[json.loads(line) for line in signal.getvalue().splitlines() if line.startswith('{')]
            fatal=terminal_error is not None or any(event.get('schema')=='cortados.cowork-event.v1' and event.get('reason') in ('storage_error','task_quarantined','artifact_corrupt') for event in events)

            live=[]
            for _,_,lease,fd in opened:
                try:os.fstat(fd);live.append(lease)
                except OSError:pass
            self.assertEqual(live,[],'All owned descriptors close even after another inode refusal')
            self.assertEqual(path.read_bytes(),b'unknown replacement')
            self.assertTrue(fatal,'Cleanup integrity failure cannot be silently swallowed')
        finally:
            # Private fixture safety on the red candidate only, never product bypass.
            for fd in tuple(self.runtime.descriptors.values()):
                try:os.close(fd)
                except OSError:pass
            for timer in tuple(self.runtime.lease_timers.values()):timer.cancel()

    async def _audit_boundary(self,kind):
        import contextlib,io
        from .support import raw_send,raw_read
        await self.start();task=await self.task();ticket=None
        if kind=='lease':ticket=await self.ticket(task['task_id'],b'x')
        api=mod('remote_schemas');original=api.emit_remote_event;stderr=io.StringIO();hits=[]
        def failing(tx,event):
            selected=((kind=='acceptance' and event.entity=='job' and event.state=='queued') or
                      (kind=='lease' and event.entity=='lease' and event.state=='open') or
                      (kind=='frame' and event.entity=='runtime' and event.event=='refusal'))
            if tx is not None and selected:
                hits.append(event.entity);raise sqlite3.OperationalError('SENTINEL audit unavailable')
            return original(tx,event)
        with contextlib.redirect_stderr(stderr),patch.object(api,'emit_remote_event',new=failing):
            if kind=='acceptance':self.assertFalse((await self.call('delegate',self.args(task)))['ok'])
            elif kind=='lease':
                result=await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':1})
                self.assertFalse(result['result']['ok'])
            else:
                await raw_send(self.client.socket,{'v':1,'seq':self.client.seq,'op':'unknown','body_bytes':0,'args':{}})
                try:
                    async with asyncio.timeout(.5):await raw_read(self.client.socket)
                except EOFError:pass
        self.assertTrue(hits,'Fault must reach the actual audit persistence boundary')
        self.assertTrue(self.runtime.admission_failed or self.runtime.broker.store.admission_closed)
        events=[json.loads(line) for line in stderr.getvalue().splitlines() if line.startswith('{')]
        self.assertEqual(sum(event.get('schema')=='cortados.cowork-event.v1' and event.get('reason')=='storage_error' for event in events),1)
        self.assertNotIn('SENTINEL',stderr.getvalue());self.assertFalse(self.runtime.descriptors)
        if kind=='acceptance':self.assertEqual(self.fixture.sql('SELECT count(*) FROM jobs')[0][0],0)

    async def test_acceptance_audit_failure_is_fatal_without_job(self):await self._audit_boundary('acceptance')
    async def test_lease_audit_failure_is_fatal_without_descriptor(self):await self._audit_boundary('lease')
    async def test_malformed_frame_audit_failure_closes_endpoint_and_latches(self):await self._audit_boundary('frame')

    async def test_timely_monitor_control_preserves_admission(self):
        self.fixture.core['limits']['workspace_poll_ms']=1000
        await self.start()
        # Bounded work well before the next1s deadline proves not always-failed;
        # it does not assert exact wakeup timing or create a jitter allowance.
        await asyncio.sleep(.01)
        self.assertFalse(self.runtime.admission_failed)
        self.ok(await self.call('task_create',{'request_id':uid(),'task_group_id':uid(),'title':'timely control'}))

class Revision2CapacityRecoveryExam(CoreCase):
    async def asyncSetUp(self):
        await super().asyncSetUp();self.fixture.core['limits']['workspace_poll_ms']=1000
        self.fixture.control(hang=True);await self.start()

    async def stopped_by_capacity(self):
        import time
        task=await self.task();arguments=self.args(task)
        job=self.ok(await self.call('delegate',arguments))['job']['job_id']
        async with asyncio.timeout(5):
            while not any(c.get('kind')=='exec' for c in self.fixture.calls()):await asyncio.sleep(.01)
        # A deliberate1250ms stall exceeds the1000ms sample-start deadline; no jitter allowance.
        # One second permits fake-native startup before this deliberate fault injection.
        time.sleep(1.25);await asyncio.sleep(.03)
        async with asyncio.timeout(5):
            while self.runtime.broker.store.get(job)['state'] not in ('cancelled','failed','timed_out','interrupted','succeeded'):await asyncio.sleep(.02)
        row=self.runtime.broker.store.get(job)
        self.assertTrue(self.runtime.broker.receipt(row));self.assertEqual(row['state'],'cancelled');self.assertEqual(row['reason'],'capacity_exhausted')
        return task,job,arguments

    async def test_controlled_missed_sample_preserves_terminal_task_status_result(self):
        task,job,arguments=await self.stopped_by_capacity();self.assertFalse(self.runtime.broker.store.admission_closed)
        self.client=await self.connect()
        self.ok(await self.call('task_get',{'task_id':task['task_id']}))
        for op in ('job_status','job_result'):
            result=self.ok(await self.call(op,{'task_id':task['task_id'],'job_id':job}))
            self.assertEqual(result['job']['state'],'cancelled');self.assertEqual(result['job']['reason'],'capacity_exhausted')
        self.error(await self.call('delegate',arguments),'capacity_exhausted')
        self.assertEqual(sum(c.get('kind')=='exec' for c in self.fixture.calls()),1)

    async def test_capacity_existing_job_cancel_is_idempotent_and_owned(self):
        entered=asyncio.Event();release=asyncio.Event();finished=asyncio.Event()
        original=self.runtime.publish
        async def controlled_publish(job_id):
            entered.set();await release.wait()
            try:return await original(job_id)
            finally:finished.set()
        with patch.object(self.runtime,'publish',controlled_publish):
            try:
                task,job,_=await self.stopped_by_capacity();key={'task_id':task['task_id'],'job_id':job}
                # Five seconds observes private native terminal/publication scheduling;
                # no product wait is added, and a missing transition fails this fixture.
                async with asyncio.timeout(5):await entered.wait()
                fields=('job_id','launch_nonce','stop_intent','state','reason')
                before={name:self.runtime.broker.store.get(job)[name] for name in fields}
                pending=self.ok(await self.call('job_cancel',key))
                self.assertEqual(pending['job']['publication'],'pending')
                self.assertEqual(sum(c.get('kind')=='exec' for c in self.fixture.calls()),1)
                release.set()
                async with asyncio.timeout(5):await finished.wait()
                complete=self.ok(await self.call('job_cancel',key))
                self.assertEqual(complete['job']['publication'],'complete')
                self.assertEqual({name:self.runtime.broker.store.get(job)[name] for name in fields},before)
                self.assertEqual(self.fixture.sql('SELECT count(*) FROM jobs')[0][0],1)
                first=self.ok(await self.call('job_cancel',key));second=self.ok(await self.call('job_cancel',key))
                self.assertEqual(first,second);self.error(await self.call('job_cancel',dict(key,task_id=uid())),'job_not_found')
                self.assertEqual(sum(c.get('kind')=='exec' for c in self.fixture.calls()),1)
            finally:release.set()

    async def test_capacity_latch_never_reopens_after_good_sample_or_restart_relaunches(self):
        task,job,arguments=await self.stopped_by_capacity()
        self.runtime.capacity.measure();await asyncio.sleep(.15)
        self.error(await self.call('delegate',arguments),'capacity_exhausted')
        await self.stack.aclose();await self.start()
        result=self.ok(await self.call('job_result',{'task_id':task['task_id'],'job_id':job}))
        self.assertEqual(result['job']['state'],'cancelled')
        self.assertEqual(sum(c.get('kind')=='exec' for c in self.fixture.calls()),1)

    async def test_capacity_cleanup_and_disabled_override_retire_owned_resources(self):
        from .support import capacity_fault,owner
        task=await self.task();ticket=await self.ticket(task['task_id'],b'x')
        lease=self.ok((await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':1}))['result'])['lease_id']
        await capacity_fault(self)
        self.ok((await self.stream('close',{'lease_id':lease,'reason':'abort'}))['result'])
        self.ok((await self.stream('close',{'lease_id':lease,'reason':'abort'}))['result'])
        self.assertFalse(self.runtime.descriptors)
        await self.runtime.disable_owner(owner(self.fixture.principal));await self.runtime.disable_owner(owner(self.fixture.principal))
        self.error(await self.call('task_get',{'task_id':task['task_id']}),'permission_refused')
        with self.assertRaises(mod('remote_schemas').RemoteError):await self.connect()

    async def test_fatal_integrity_and_audit_override_capacity_observation(self):
        from .support import capacity_fault
        import contextlib,io
        task=await self.task();await capacity_fault(self)
        # Existing runtime fatal primitive is the named integrity/audit boundary.
        with contextlib.redirect_stderr(io.StringIO()):self.runtime.fatal()
        for op,args in [('task_get',{'task_id':task['task_id']}),('job_status',{'task_id':task['task_id'],'job_id':uid()})]:
            self.error(await self.call(op,args),'storage_error')
        self.assertTrue(self.runtime.broker.store.admission_closed)
        self.assertFalse(self.runtime.descriptors)

    async def test_capacity_observation_keeps_binding_corruption_refusal(self):
        from unittest.mock import patch
        task,job,_=await self.stopped_by_capacity()
        # Corrupt the actual read projection seam, without mutating the persisted evidence.
        from .support import mod
        store=mod('task_store');original=store.rows;hits=[]
        def corrupt(db,table,*args,**kwargs):
            if table=='remote_job_bindings':
                hits.append(table);raise mod('remote_schemas').RemoteError('storage_error')
            return original(db,table,*args,**kwargs)
        with patch.object(store,'rows',corrupt):
            self.error(await self.call('job_result',{'task_id':task['task_id'],'job_id':job}),'storage_error')
        self.assertTrue(hits,'Allowed capacity observation must still validate the real binding')
        self.assertEqual(sum(c.get('kind')=='exec' for c in self.fixture.calls()),1)

class CapacityTransitionAuditExam(CoreCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # 200ms samples give the no-worker fixture a short repeat window. Five-second
        # observers below bound fixture failure, not a product lateness allowance.
        self.fixture.core['limits']['workspace_poll_ms']=200
        await self.start()

    def capacity_events(self):
        events=[json.loads(row[0]) for row in self.fixture.sql('SELECT event_json FROM remote_audit')]
        return [event for event in events if event['entity']=='runtime' and
                event['event']=='transition' and event['reason']=='capacity_exhausted']

    async def samples(self, queue, count):
        async with asyncio.timeout(5):
            for _ in range(count):await queue.get()

    async def test_first_capacity_latch_persists_one_closed_runtime_transition(self):
        queue=asyncio.Queue();api=mod('remote_schemas')
        before=self.fixture.sql('SELECT count(*) FROM remote_audit')[0][0]
        def unreadable():
            queue.put_nowait(None);raise api.RemoteError('capacity_exhausted')
        with patch.object(self.runtime.capacity,'measure',unreadable):
            await self.samples(queue,1)
            self.assertTrue(self.runtime.capacity_failed)
            self.assertFalse(self.runtime.fatal_failed)
            events=self.capacity_events();self.assertEqual(len(events),1)
            event=events[0];api.RemoteEvent(**event)
            self.assertIn(event['from_state'],('starting','ready','disabled','stopping','failed','closed'))
            self.assertIn(event['state'],('starting','ready','disabled','stopping','failed','closed'))
            self.assertNotEqual(event['from_state'],event['state'])
            self.assertEqual(self.fixture.sql('SELECT count(*) FROM remote_audit')[0][0],before+1)
            self.assertEqual(self.fixture.sql('SELECT count(*) FROM jobs')[0][0],0)
            self.assertFalse(any(call.get('kind')=='exec' for call in self.fixture.calls()))

    async def test_repeated_bad_then_good_samples_never_duplicate_or_reopen(self):
        queue=asyncio.Queue();bad=True;original=self.runtime.capacity.measure
        def measured():
            queue.put_nowait(None)
            if bad:raise mod('remote_schemas').RemoteError('capacity_exhausted')
            return original()
        with patch.object(self.runtime.capacity,'measure',measured):
            await self.samples(queue,3)
            first=self.capacity_events();self.assertEqual(len(first),1)
            bad=False
            await self.samples(queue,3)
            self.assertEqual(self.capacity_events(),first)
            self.assertTrue(self.runtime.capacity_failed)
            self.assertFalse(self.runtime.fatal_failed)
            before=self.fixture.sql('SELECT count(*) FROM remote_audit')[0][0]
            self.error(await self.call('task_create',{'request_id':uid(),'task_group_id':uid()}),'capacity_exhausted')
            after=[json.loads(row[0]) for row in self.fixture.sql('SELECT event_json FROM remote_audit')]
            self.assertEqual(len(after),before+1)
            refusal=after[-1];self.assertEqual(refusal['event'],'refusal');self.assertEqual(refusal['reason'],'capacity_exhausted')
            self.assertEqual(self.capacity_events(),first)

    async def test_capacity_transition_audit_failure_is_fatal_and_signals_once(self):
        import contextlib,io
        queue=asyncio.Queue();api=mod('remote_schemas');original=api.emit_remote_event
        stderr=io.StringIO();attempts=[]
        before=self.fixture.sql('SELECT count(*) FROM remote_audit')[0][0]
        def unreadable():
            queue.put_nowait(None);raise api.RemoteError('capacity_exhausted')
        def failing(tx,event):
            if tx is not None and event.entity=='runtime' and event.reason=='capacity_exhausted':
                attempts.append(event);raise sqlite3.OperationalError('SENTINEL capacity audit unavailable')
            return original(tx,event)
        with contextlib.redirect_stderr(stderr),patch.object(api,'emit_remote_event',failing),patch.object(self.runtime.capacity,'measure',unreadable):
            await self.samples(queue,1)
            self.assertEqual(len(attempts),1,'First capacity latch must reach the durable audit owner')
            self.assertTrue(self.runtime.fatal_failed)
            self.assertTrue(self.runtime.broker.store.admission_closed)
            self.assertEqual(self.fixture.sql('SELECT count(*) FROM remote_audit')[0][0],before)
            self.assertFalse(self.capacity_events())
            # Re-entering the existing fatal primitive cannot retry the failed audit.
            self.runtime.fatal();self.runtime.fatal()
            # 250ms observes more than one200ms monitor interval after fatal state;
            # it cannot prolong the failed operation or grant a persistence retry.
            await asyncio.sleep(.25)
            self.assertEqual(len(attempts),1)
            with self.assertRaises(api.RemoteError) as error:await self.connect()
            self.assertEqual(error.exception.code,'storage_error')
        events=[json.loads(line) for line in stderr.getvalue().splitlines() if line.startswith('{')]
        self.assertEqual(len(events),1);api.RemoteEvent(**events[0])
        self.assertEqual((events[0]['entity'],events[0]['event'],events[0]['reason']),('runtime','refusal','storage_error'))
        self.assertNotIn('SENTINEL',stderr.getvalue())
        self.assertFalse(self.runtime.descriptors);self.assertFalse(self.runtime.leases)
