# PURPOSE: Pin capacity accounting correctness; timing/RSS are recorded separately.
# CONTRACT: RT-COWORK-MCP-008 AC13,16. Synthetic limits do not select deployed policy.
import asyncio
import json
import time
from .support import CoreCase,mod,sha,uid

class CapacityExam(CoreCase):
    async def test_file_exact_bound_allowed_one_over_refused_without_intent(self):
        self.fixture.core['limits']['file_bytes']=4096
        await self.start();task=await self.task();await self.upload(task['task_id'],b'x'*4096)
        before=self.fixture.sql('SELECT count(*) FROM remote_artifacts')[0][0]
        result=await self.call('file_upload_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':None,
            'filename':'too-large.bin','bytes':4097,'sha256':sha(b'x'*4097),'media_type':'application/octet-stream'})
        self.assertFalse(result['ok']);self.assertEqual(self.fixture.sql('SELECT count(*) FROM remote_artifacts')[0][0],before)
    async def test_zero_byte_reserved_intents_consume_artifact_slots(self):
        self.fixture.core['limits']['artifacts_per_task']=2
        await self.start();task=await self.task();await self.ticket(task['task_id'],b'');await self.ticket(task['task_id'],b'')
        result=await self.call('file_upload_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':None,
            'filename':'third.bin','bytes':0,'sha256':sha(b''),'media_type':'application/octet-stream'})
        self.error(result,'capacity_exhausted');self.assertEqual(self.fixture.sql('SELECT count(*) FROM remote_artifacts')[0][0],2)
    async def test_pending_upload_reservation_counts_and_rename_does_not_double_count(self):
        limits=self.fixture.core['limits'];limits.update(file_bytes=4096,task_bytes=4096)
        await self.start();task=await self.task();artifact,_=await self.upload(task['task_id'],b'x'*4096)
        usage=mod('remote_capacity').CapacityMonitor(self.runtime).measure()
        row=next(t for t in usage.tasks if t.task_id==task['task_id'])
        self.assertEqual(row.measured_bytes,4096);self.assertEqual(row.promised_bytes,0)
        result=await self.call('file_upload_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':None,
            'filename':'one.bin','bytes':1,'sha256':sha(b'x'),'media_type':'application/octet-stream'})
        self.error(result,'capacity_exhausted')
    async def test_selected_input_copy_reserves_destination_before_native_start(self):
        self.fixture.core['limits'].update(file_bytes=4096,task_bytes=6144)
        await self.start();task=await self.task();artifact,_=await self.upload(task['task_id'],b'x'*4096)
        self.error(await self.call('delegate',self.args(task,input_artifact_ids=[artifact['artifact_id']])),'capacity_exhausted')
        self.assertFalse([x for x in self.fixture.calls() if x['kind']=='exec'])
    async def test_workspace_file_overrun_stops_actual_fake_worker(self):
        self.fixture.core['limits'].update(file_bytes=4096,workspace_poll_ms=100)
        self.fixture.control(output_hex={'large.bin':(b'x'*8192).hex()},hang=True)
        await self.start();task=await self.task();job=self.ok(await self.call('delegate',self.args(task)))['job']
        result=await self.terminal(task['task_id'],job['job_id'])
        self.assertEqual(result['job']['state'],'cancelled');self.assertEqual(result['job']['reason'],'capacity_exhausted')
    async def test_unknown_payload_root_entry_blocks_new_reservations(self):
        await self.start();task=await self.task();(self.fixture.path/'blobs/unknown-object').write_bytes(b'x')
        result=await self.call('file_upload_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':None,
            'filename':'one.bin','bytes':1,'sha256':sha(b'x'),'media_type':'application/octet-stream'})
        self.assertFalse(result['ok']);self.assertEqual(self.fixture.sql('SELECT count(*) FROM remote_artifacts')[0][0],0)
    async def test_filesystem_available_boundary_includes_existing_promises(self):
        import os
        from unittest.mock import patch
        await self.start();first=await self.task();second=await self.task()
        original=os.statvfs(self.fixture.path);values=list(original)
        values[1]=1;values[4]=self.fixture.core['limits']['disk_headroom_bytes']+1
        boundary=os.statvfs_result(values)
        with patch.object(os,'statvfs',return_value=boundary),patch.object(os,'fstatvfs',return_value=boundary):
            await self.ticket(first['task_id'],b'x')
            result=await self.call('file_upload_ticket',{'task_id':second['task_id'],'request_id':uid(),'artifact_id':None,
                'filename':'one.bin','bytes':1,'sha256':sha(b'x'),'media_type':'application/octet-stream'})
        self.error(result,'capacity_exhausted')
