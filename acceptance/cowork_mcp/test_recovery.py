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
