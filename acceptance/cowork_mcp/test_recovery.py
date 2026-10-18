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
