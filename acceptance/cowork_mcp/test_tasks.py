# PURPOSE: Pin task identity, ownership and atomic ordinary-job reuse.
# CONTRACT: RT-COWORK-MCP-008 AC03,06–09,15. DEPENDS: actual public core channel.
# CEILING: Synthetic native double only, no external provider.
import asyncio
import copy
import json
from .support import CoreCase,uid,owner

class TaskExam(CoreCase):
    async def test_create_replay_keeps_identity_and_current_projection(self):
        await self.start();args={'request_id':uid(),'task_group_id':uid(),'title':'Same λ'}
        first=self.ok(await self.call('task_create',args))
        results=await asyncio.gather(*(self.call('task_create',args) for _ in range(5)))
        self.assertTrue(all(r['task']['task_id']==first['task']['task_id'] for r in results))
        self.assertEqual(len(self.fixture.sql('SELECT task_id FROM remote_tasks')),1)
        self.error(await self.call('task_create',dict(args,title='changed')),'idempotency_conflict')
        task_id=first['task']['task_id'];self.ok(await self.call('task_delete',{'task_id':task_id,'request_id':uid()}))
        replay=self.ok(await self.call('task_create',args))
        self.assertIn(replay['task']['state'],('deleting','deleted'));self.assertEqual(replay['task']['task_id'],task_id)
    async def test_delete_tombstone_replay_preserves_history(self):
        await self.start();task=await self.task();args={'task_id':task['task_id'],'request_id':uid()}
        result=self.ok(await self.call('task_delete',args))
        for _ in range(100):
            result=self.ok(await self.call('task_get',{'task_id':task['task_id']}))
            if result['task']['state']=='deleted':break
            await asyncio.sleep(.01)
        self.assertEqual(result['task']['state'],'deleted');self.assertEqual(result['task']['artifacts'],[])
        self.assertEqual(self.ok(await self.call('task_delete',args))['task']['task_id'],task['task_id'])
        self.assertEqual(self.fixture.sql('SELECT count(*) FROM remote_tasks')[0][0],1)
        self.assertGreaterEqual(self.fixture.sql('SELECT count(*) FROM remote_operations')[0][0],2)
    async def test_unknown_task_and_foreign_job_are_identical_refusals(self):
        await self.start();task=await self.task()
        for tool in ('job_status','job_result','job_cancel'):
            self.error(await self.call(tool,{'task_id':task['task_id'],'job_id':uid()}),'job_not_found')
        self.error(await self.call('task_get',{'task_id':uid()}),'task_not_found')
    async def test_group_and_context_refusals_do_not_accept_job(self):
        await self.start();task=await self.task();args=self.args(task)
        args['task_evidence']['task_group_id']=uid()
        refusal=await self.call('delegate',args);self.assertFalse(refusal['ok'])
        self.assertEqual(self.fixture.sql('SELECT count(*) FROM jobs')[0][0],0)
        self.assertFalse([r for r in self.fixture.calls() if r['kind']=='exec'])
    async def test_declined_quota_retires_preparation_and_same_request_can_retry(self):
        self.fixture.control(quota='spent');await self.start();task=await self.task();args=self.args(task)
        self.error(await self.call('delegate',args),'quota_spent')
        self.assertEqual(self.fixture.sql('SELECT count(*) FROM jobs')[0][0],0)
        self.assertEqual(self.fixture.sql("SELECT count(*) FROM remote_operations WHERE operation='delegate'")[0][0],0)
        self.assertFalse([p for p in (self.fixture.path/'tasks').rglob(args['request_id'])])
        self.fixture.control(quota='fresh');await asyncio.sleep(1.05)
        job=self.ok(await self.call('delegate',args))['job'];await self.terminal(task['task_id'],job['job_id'])
        self.assertEqual(self.fixture.sql('SELECT count(*) FROM jobs')[0][0],1)
    async def test_delegate_replay_does_not_score_or_start_twice(self):
        await self.start();task=await self.task();args=self.args(task)
        job=self.ok(await self.call('delegate',args))['job']
        await self.terminal(task['task_id'],job['job_id'])
        before=len(self.fixture.calls())
        result=self.ok(await self.call('delegate',args))
        self.assertEqual(result['job']['job_id'],job['job_id']);self.assertEqual(len(self.fixture.calls()),before)
        self.assertEqual(len([r for r in self.fixture.calls() if r['kind']=='exec']),1)
        self.error(await self.call('delegate',dict(args,task='changed')),'idempotency_conflict')
    async def test_second_task_cannot_read_cancel_or_feedback_first_job(self):
        await self.start();first=await self.task();second=await self.task()
        job=self.ok(await self.call('delegate',self.args(first)))['job'];await self.terminal(first['task_id'],job['job_id'])
        for tool in ('job_status','job_result','job_cancel'):
            self.error(await self.call(tool,{'task_id':second['task_id'],'job_id':job['job_id']}),'job_not_found')
        self.error(await self.call('job_feedback',{'task_id':second['task_id'],'job_id':job['job_id'],'feedback_id':uid(),'verdict':'inconclusive','summary':'test','checks':[]}),'job_not_found')
    async def test_busy_task_blocks_delete_and_second_delegate(self):
        self.fixture.control(hang=True);await self.start();task=await self.task();args=self.args(task)
        job=self.ok(await self.call('delegate',args))['job']
        self.error(await self.call('delegate',self.args(task)),'workspace_busy')
        self.error(await self.call('task_delete',{'task_id':task['task_id'],'request_id':uid()}),'workspace_busy')
        self.ok(await self.call('job_cancel',{'task_id':task['task_id'],'job_id':job['job_id']}))
        await self.terminal(task['task_id'],job['job_id'])
    async def test_disable_latches_without_cancelling_job(self):
        self.fixture.control(hang=True);await self.start();task=await self.task()
        job=self.ok(await self.call('delegate',self.args(task)))['job']
        await self.runtime.disable_owner(owner(self.fixture.principal))
        self.error(await self.call('task_get',{'task_id':task['task_id']}),'permission_refused')
        self.assertIn(json.loads(self.fixture.sql('SELECT record FROM jobs WHERE job_id=?',job['job_id'])[0][0])['state'],('queued','running'))
        (self.fixture.path/'native/release-worker').touch()
    async def test_transition_audit_has_one_creation_event_and_no_payload_secrets(self):
        await self.start();args={'request_id':uid(),'task_group_id':uid(),'title':'SENTINEL-TITLE-PRIVATE'}
        result=self.ok(await self.call('task_create',args));await self.call('task_create',args)
        events=[json.loads(row[0]) for row in self.fixture.sql('SELECT event_json FROM remote_audit')]
        matching=[e for e in events if e['entity']=='task' and e['object_id']==result['task']['task_id'] and e['from_state'] is None]
        self.assertEqual(len(matching),1)
        serialized=json.dumps(events);self.assertNotIn(args['title'],serialized);self.assertNotIn(str(self.fixture.path),serialized)
        for event in events:self.assertEqual(set(event),{'schema','event_id','at_ms','event','entity','object_id','request_id','from_state','state','reason'})
    async def test_foreign_trusted_principal_refuses_attach(self):
        from .support import mod
        await self.start();principal=dict(self.fixture.principal,subject='synthetic:other')
        with self.assertRaises(mod('remote_schemas').RemoteError):await self.connect(principal)


from .support import mod

class BuildLifecycleRegressionExam(CoreCase):
    async def test_disable_ahead_of_queued_create_rechecks_authority_after_lock(self):
        import asyncio
        from .support import owner
        await self.start();lock=self.runtime.broker.admission_lock
        await lock.acquire()
        disable=asyncio.create_task(self.runtime.disable_owner(owner(self.fixture.principal)))
        await asyncio.sleep(.01)
        request=asyncio.create_task(self.call('task_create',{'request_id':uid(),'task_group_id':uid(),'title':'queued'}))
        await asyncio.sleep(.01);lock.release();await disable
        self.error(await request,'permission_refused')
        self.assertEqual(self.fixture.sql('SELECT count(*) FROM remote_tasks')[0][0],0)
        self.assertFalse(self.runtime.enabled)

    async def test_cancelled_ipc_call_cannot_deliver_predecessor_response(self):
        import asyncio
        await self.start();lock=self.runtime.broker.admission_lock;await lock.acquire()
        pending=asyncio.create_task(self.call('task_create',{'request_id':uid(),'task_group_id':uid(),'title':'first'}))
        await asyncio.sleep(.03);pending.cancel()
        with self.assertRaises(asyncio.CancelledError):await pending
        lock.release()
        try:
            response=await self.call('task_create',{'request_id':uid(),'task_group_id':uid(),'title':'second'})
        except mod('remote_schemas').RemoteError:
            response=None
        self.assertTrue(self.client.closed,'Cancelled indeterminate frame must retire its endpoint')
        self.assertIsNone(response,'A later call must not consume the predecessor response')
        other=await self.connect()
        self.ok(await self.call('task_create',{'request_id':uid(),'task_group_id':uid(),'title':'fresh endpoint'},other))

class Revision2CapacityTableExam(CoreCase):
    async def asyncSetUp(self):
        await super().asyncSetUp();self.fixture.core['limits']['workspace_poll_ms']=100
        await self.start()

    async def test_capacity_only_denied_tool_table_preserves_rows_and_authority(self):
        from .support import capacity_fault,sha
        task_args={'request_id':uid(),'task_group_id':uid(),'title':'retained'}
        task=self.ok(await self.call('task_create',task_args))['task']
        artifact,ticket=await self.upload(task['task_id'],b'x')
        download_args={'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']}
        self.ok(await self.call('file_download_ticket',download_args))
        upload_args={'task_id':task['task_id'],'request_id':uid(),'artifact_id':None,'filename':'pending',
            'bytes':1,'sha256':sha(b'x'),'media_type':'application/octet-stream'}
        pending=self.ok(await self.call('file_upload_ticket',upload_args))['ticket']
        args=self.args(task);route=dict(args)
        for k in ('request_id','timeout_s'):route.pop(k)
        await capacity_fault(self)
        before={t:self.fixture.sql('SELECT count(*) FROM '+t)[0][0] for t in ('remote_tasks','jobs','remote_tickets','remote_reservations','job_feedback')}
        calls=[('task_create',task_args),('task_create',dict(task_args,request_id=uid())),('route',route),('delegate',args),
            ('context_requirements',{'task':'test','context':'','selection':args['selection'],'task_evidence':args['task_evidence']}),
            ('task_delete',{'task_id':task['task_id'],'request_id':uid()}),
            ('job_feedback',{'task_id':task['task_id'],'job_id':uid(),'feedback_id':uid(),'verdict':'inconclusive','summary':'held','checks':[]}),
            ('file_download_ticket',download_args),('file_download_ticket',dict(download_args,request_id=uid())),
            ('file_upload_ticket',upload_args),
            ('file_upload_ticket',dict(upload_args,request_id=uid(),artifact_id=pending['artifact_id'])),
            ('file_upload_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':None,'filename':'denied','bytes':1,'sha256':sha(b'x'),'media_type':'application/octet-stream'})]
        for tool,arguments in calls:
            with self.subTest(tool=tool):
                audit=self.fixture.sql('SELECT count(*) FROM remote_audit')[0][0]
                self.error(await self.call(tool,arguments),'capacity_exhausted')
                self.assertEqual(self.fixture.sql('SELECT count(*) FROM remote_audit')[0][0],audit+1)
                self.assertEqual(before,{t:self.fixture.sql('SELECT count(*) FROM '+t)[0][0] for t in before})
        self.assertFalse([c for c in self.fixture.calls() if c['kind']=='exec'])

    async def test_capacity_reconnect_inspects_owned_task_and_rejects_foreign_binding(self):
        from .support import capacity_fault,mod
        task=await self.task();await capacity_fault(self)
        self.client=await self.connect();self.assertEqual(self.ok(await self.call('task_get',{'task_id':task['task_id']}))['task']['task_id'],task['task_id'])
        foreign=dict(self.fixture.principal,subject='another')
        with self.assertRaises(mod('remote_schemas').RemoteError):await self.connect(foreign)
        self.error(await self.call('task_get',{'task_id':uid()}),'task_not_found')
        import socket
        from .support import raw_send,raw_read
        left,right=socket.socketpair();left.setblocking(False);right.setblocking(False)
        self.addCleanup(left.close);self.addCleanup(right.close)
        await self.runtime.attach(left,self.runtime.config.principal)
        await raw_send(right,dict(v=1,seq=0,op='bind',body_bytes=0,args={
            'principal':self.fixture.principal,'config_digest':'f'*64}))
        self.error((await raw_read(right))[0]['result'],'permission_refused')

    async def test_capacity_only_stream_table_refuses_effects_and_retires_leases(self):
        from .support import capacity_fault
        put_task=await self.task();put_ticket=await self.ticket(put_task['task_id'],b'x')
        get_task=await self.task();artifact,_=await self.upload(get_task['task_id'],b'x')
        get_ticket=self.ok(await self.call('file_download_ticket',{'task_id':get_task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']}))['ticket']
        put_args={'ticket_id':put_ticket['ticket_id'],'capability':put_ticket['authorization'][7:],'content_length':1}
        get_args={'ticket_id':get_ticket['ticket_id'],'capability':get_ticket['authorization'][7:]}
        put_lease=self.ok((await self.stream('open_put',put_args))['result'])['lease_id']
        get_lease=self.ok((await self.stream('open_get',get_args))['result'])['lease_id']
        await capacity_fault(self)
        for op,args,body in [('open_put',put_args,b''),('open_get',get_args,b''),('put_chunk',{'lease_id':put_lease,'offset':0},b'x'),
                             ('finish_put',{'lease_id':put_lease},b''),('read_chunk',{'lease_id':get_lease,'offset':0,'max_bytes':1},b'')]:
            with self.subTest(op=op):self.error((await self.stream(op,args,body))['result'],'capacity_exhausted')
        self.assertFalse(self.runtime.leases);self.assertFalse(self.runtime.descriptors)
        self.assertEqual(self.fixture.sql("SELECT count(*) FROM remote_artifacts WHERE origin='input' AND state='sealed'")[0][0],1)


from unittest.mock import patch
from .support import FatalCapture, event_barrier, monitor_fixture, remote_counts, sha


class MonitorPostWaitExam(CoreCase):
    async def queued_operation(self, lock_kind, lose=True):
        gate = await monitor_fixture(self)
        task = await self.task()
        if lock_kind == 'admission':
            lock = self.runtime.broker.admission_lock
            tool = 'task_create'
            args = {'request_id': uid(), 'task_group_id': uid()}
        else:
            lock = self.runtime.task_locks[task['task_id']] = asyncio.Lock()
            tool = 'file_upload_ticket'
            args = {'task_id': task['task_id'], 'request_id': uid(), 'artifact_id': None,
                    'filename': 'late.bin', 'bytes': 0, 'sha256': sha(b''),
                    'media_type': 'application/octet-stream'}
        await lock.acquire()
        reached = asyncio.Event()
        acquire = lock.acquire
        async def wait():
            reached.set()
            return await acquire()
        signal = FatalCapture(self)
        before = remote_counts(self)
        observations = []
        authorize = self.runtime.authorize
        def observe(principal, operation=None):
            if reached.is_set():
                observations.append((self.runtime.monitor_task.done(), len(signal.attempts)))
            return authorize(principal, operation)
        with patch.object(lock, 'acquire', wait), patch.object(self.runtime, 'authorize', observe):
            pending = asyncio.create_task(self.runtime.core.dispatch_remote(
                self.runtime.config.principal, tool, args))
            await asyncio.wait_for(reached.wait(), 1)
            if lose:
                gate.release.set()
            lock.release()
            result = await asyncio.wait_for(pending, 2)
        if lose:
            self.assertEqual(observations[0], (True, 0), 'Post-wait guard must precede observer')
            self.error(result, 'storage_error')
            self.assertEqual(remote_counts(self), before)
            self.assertFalse(any(call.get('kind') == 'exec' for call in self.fixture.calls()))
            await event_barrier()
            signal.assert_one(self)
        else:
            self.ok(result)
            self.assertFalse(self.runtime.fatal_failed)
            self.assertFalse(self.runtime.monitor_task.done())

    async def test_monitor_loss_during_admission_wait_prevents_task_effect(self):
        await self.queued_operation('admission')

    async def test_monitor_loss_during_task_wait_prevents_ticket_and_reservation(self):
        await self.queued_operation('task')

    async def test_healthy_admission_and_task_wait_controls(self):
        await self.queued_operation('admission', lose=False)
        await self.stack.aclose()
        await self.queued_operation('task', lose=False)

    async def test_monitor_loss_after_route_wait_prevents_acceptance_and_native_launch(self):
        gate = await monitor_fixture(self)
        task = await self.task()
        started = asyncio.Event()
        release = asyncio.Event()
        original = asyncio.to_thread
        async def route_wait(function, *args, **kwargs):
            result = await original(function, *args, **kwargs)
            if function.__module__ == 'cortados_router.route_task':
                started.set()
                await release.wait()
            return result
        before = remote_counts(self)
        with patch.object(asyncio, 'to_thread', route_wait):
            pending = asyncio.create_task(self.runtime.core.dispatch_remote(
                self.runtime.config.principal, 'delegate', self.args(task)))
            await asyncio.wait_for(started.wait(), 2)
            gate.release.set()
            release.set()
            result = await asyncio.wait_for(pending, 2)
        self.error(result, 'storage_error')
        self.assertEqual(remote_counts(self), before)
        self.assertFalse(any(call.get('kind') == 'exec' for call in self.fixture.calls()))

    async def test_every_ordinary_authority_category_checks_terminal_monitor(self):
        gate = await monitor_fixture(self)
        gate.release.set()
        await gate.finished.wait()
        operations = ('attach', 'bind', 'task_get', 'job_status', 'job_result', 'job_cancel',
                      'job_feedback', 'task_delete', 'context_requirements', 'task_create',
                      'route', 'delegate', 'file_upload_ticket', 'file_download_ticket',
                      'open_put', 'open_get', 'put_chunk', 'finish_put', 'read_chunk')
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(mod('remote_schemas').RemoteError) as raised:
                    self.runtime.authorize(self.runtime.config.principal, operation)
                self.assertEqual(raised.exception.code, 'storage_error')
        self.assertEqual(remote_counts(self),
                         {'remote_tasks': 0, 'jobs': 0, 'remote_tickets': 0, 'remote_reservations': 0})


class MonitorPendingLaunchExam(CoreCase):
    async def pending_launch(self, phase, loss, unreadable):
        import contextlib
        import sqlite3
        from .support import lose_monitor, wait_for_fixture
        self.fixture.control(hang=True)
        gate = await monitor_fixture(self)
        task = await self.task()
        broker = self.runtime.broker
        signal = FatalCapture(self)
        reached, release = asyncio.Event(), asyncio.Event()
        spawn, send, admit = asyncio.create_subprocess_exec, broker.send, broker.admit_launch
        commands, processes = [], []
        job = None
        with contextlib.ExitStack() as seams:
            async def pause():
                reached.set()
                await release.wait()

            async def delayed_spawn(*args, **kwargs):
                process = await spawn(*args, **kwargs)
                if 'integrations.mcp.worker' in args:
                    processes.append(process)
                    if phase == 'spawn':
                        await pause()
                    elif phase == 'ready':
                        readline = process.stdout.readline
                        async def delayed_ready():
                            line = await readline()
                            if line and json.loads(line).get('event') == 'ready':
                                await pause()
                            return line
                        seams.enter_context(patch.object(process.stdout, 'readline', delayed_ready))
                return process

            async def delayed_admit(record):
                result = await admit(record)
                if phase == 'admitted':
                    await pause()
                return result

            async def observed_send(process, command):
                if 'command' in command:
                    commands.append(dict(command))
                return await send(process, command)

            seams.enter_context(patch.object(asyncio, 'create_subprocess_exec', delayed_spawn))
            seams.enter_context(patch.object(broker, 'admit_launch', delayed_admit))
            seams.enter_context(patch.object(broker, 'send', observed_send))
            try:
                job = self.ok(await self.call('delegate', self.args(task)))['job']['job_id']
                # Seconds: local fake startup observation only; short values test host
                # load, unbounded waits strand fixtures. No product grace is added.
                await asyncio.wait_for(reached.wait(), 3)
                self.assertEqual(broker.store.get(job)['state'], 'queued')
                self.assertFalse(any(c.get('kind') == 'exec' for c in self.fixture.calls()))
                if phase == 'spawn':
                    self.assertNotIn(job, broker.workers)
                else:
                    self.assertIn(job, broker.workers)
                if loss:
                    fault = (patch.object(broker.store, 'active', side_effect=sqlite3.DatabaseError(
                        'SYNTHETIC active enumeration')) if unreadable else contextlib.nullcontext())
                    with fault:
                        await lose_monitor(self, gate)
                        await asyncio.wait_for(asyncio.shield(self.runtime.containment_task), 2)
                    self.assertTrue(self.runtime.fatal_failed)
                    signal.assert_one(self)
                release.set()
                async with asyncio.timeout(3):
                    while not any(c.get('kind') == 'exec' for c in self.fixture.calls()) and any(
                            not pending.done() for pending in broker.tasks):
                        await asyncio.sleep(.02)
                execution_count = sum(c.get('kind') == 'exec' for c in self.fixture.calls())
                before_cleanup = broker.store.get(job)
                before_commands = list(commands)
            finally:
                release.set()
                (self.fixture.path / 'native/release-worker').touch()
                if job is not None:
                    await broker.stop(job, 'cancel_requested')
                    await wait_for_fixture(lambda: all(pending.done() for pending in broker.tasks))
                    self.assertTrue(broker.receipt(broker.store.get(job)))
                    self.assertTrue(processes)
                    self.assertTrue(all(process.returncode == 0 for process in processes))
                    self.assertFalse(broker.workers)
            self.assertEqual(execution_count, 0 if loss else 1,
                             'R098/R101: resumed accepted launch must not bypass monitor loss')
            if loss:
                self.assertFalse(any(c['command'] == 'START' for c in before_commands))
                self.assertTrue(any(c == {'command': 'STOP', 'reason': 'capacity_exhausted'}
                                    for c in before_commands))
                self.assertEqual(before_cleanup['stop_intent'], 'capacity_exhausted')
                signal.assert_one(self)
            else:
                self.assertEqual(sum(c['command'] == 'START' for c in before_commands), 1)
                self.assertFalse(self.runtime.fatal_failed)
                self.assertFalse(self.runtime.monitor_task.done())

    async def test_pending_spawn_healthy_control(self):
        await self.pending_launch('spawn', False, False)

    async def test_pending_spawn_loss_readable_control(self):
        await self.pending_launch('spawn', True, False)

    async def test_pending_spawn_loss_unreadable_active_never_starts(self):
        await self.pending_launch('spawn', True, True)

    async def test_ready_wait_healthy_control(self):
        await self.pending_launch('ready', False, False)

    async def test_ready_wait_loss_readable_control(self):
        await self.pending_launch('ready', True, False)

    async def test_ready_wait_loss_unreadable_active_never_starts(self):
        await self.pending_launch('ready', True, True)

    async def test_post_admission_wait_healthy_control(self):
        await self.pending_launch('admitted', False, False)

    async def test_post_admission_wait_loss_readable_control(self):
        await self.pending_launch('admitted', True, False)

    async def test_post_admission_wait_loss_unreadable_active_never_starts(self):
        await self.pending_launch('admitted', True, True)
