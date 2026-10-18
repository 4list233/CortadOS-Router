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
