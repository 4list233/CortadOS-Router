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
