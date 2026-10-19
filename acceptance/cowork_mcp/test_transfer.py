# PURPOSE: Pin actual raw byte, lease, framing and two-process boundaries.
# CONTRACT: RT-COWORK-MCP-008 AC10–14. DEPENDS: CoreClient API and independent frame oracle.
# CEILING: Socketpair/synthetic bytes only; no HTTP listener or external request.
import asyncio
import base64
import hashlib
import hmac
import json
import multiprocessing
import socket
import struct
from .support import CoreCase,HERE,canon,mod,owner,raw_send,raw_read,runtime_child,sha,uid

class TransferExam(CoreCase):
    async def test_zero_and_non_utf8_bytes_roundtrip_hash_exact(self):
        await self.start();task=await self.task()
        for data in (b'',b'\x00', (HERE/'fixtures/binary.bin').read_bytes(),b'a'*1048576):
            artifact,_=await self.upload(task['task_id'],data)
            actual,_=await self.download(task['task_id'],artifact['artifact_id'])
            self.assertEqual(actual,data)
    async def test_hmac_matches_independent_encoding_and_full_get_retries(self):
        await self.start();task=await self.task();data=b'\x00\xfftest'
        artifact,_=await self.upload(task['task_id'],data)
        request={'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']}
        ticket=self.ok(await self.call('file_download_ticket',request))['ticket']
        fields=[self.fixture.core['ticket_key_version'],ticket['ticket_id'],owner(self.fixture.principal),task['task_id'],
            'file_download_ticket',request['request_id'],artifact['artifact_id'],'get',len(data),sha(data),ticket['expires_at_ms']]
        expected=base64.urlsafe_b64encode(hmac.digest(bytes(32),b'cortados.cowork-ticket.v1\0'+canon(fields),'sha256')).decode().rstrip('=')
        self.assertEqual(ticket['authorization'],'Bearer '+expected)
        self.assertEqual(self.ok(await self.call('file_download_ticket',request))['ticket'],ticket)
        for _ in range(2):
            opened=await self.stream('open_get',{'ticket_id':ticket['ticket_id'],'capability':expected});lease=self.ok(opened['result'])['lease_id']
            result=await self.stream('read_chunk',{'lease_id':lease,'offset':0,'max_bytes':100});self.assertEqual(result['body'],data)
            self.ok((await self.stream('close',{'lease_id':lease,'reason':'complete'}))['result'])
    async def test_put_consumed_and_wrong_hash_are_not_success(self):
        await self.start();task=await self.task();_,ticket=await self.upload(task['task_id'],b'x')
        result=await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':1})
        self.error(result['result'],'ticket_consumed')
        ticket=await self.ticket(task['task_id'],b'x');opened=await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':1})
        lease=self.ok(opened['result'])['lease_id'];await self.stream('put_chunk',{'lease_id':lease,'offset':0},b'y')
        self.error((await self.stream('finish_put',{'lease_id':lease}))['result'],'hash_mismatch')
    async def test_wrong_offset_revokes_own_lease(self):
        await self.start();task=await self.task();ticket=await self.ticket(task['task_id'],b'ab')
        lease=self.ok((await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':2}))['result'])['lease_id']
        self.error((await self.stream('put_chunk',{'lease_id':lease,'offset':1},b'a'))['result'],'length_mismatch')
        self.assertFalse((await self.stream('put_chunk',{'lease_id':lease,'offset':0},b'a'))['result']['ok'])
    async def test_foreign_connection_cannot_write_existing_lease(self):
        await self.start();task=await self.task();ticket=await self.ticket(task['task_id'],b'ab')
        lease=self.ok((await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':2}))['result'])['lease_id']
        other=await self.connect()
        self.error((await self.stream('put_chunk',{'lease_id':lease,'offset':0},b'ab',client=other))['result'],'ticket_invalid')
        self.ok((await self.stream('put_chunk',{'lease_id':lease,'offset':0},b'ab'))['result'])
        self.ok((await self.stream('finish_put',{'lease_id':lease}))['result'])
    async def test_expiry_blocks_new_open_but_open_get_completes(self):
        self.fixture.core['limits']['ticket_ttl_s']=1
        await self.start();task=await self.task();artifact,_=await self.upload(task['task_id'],b'abc')
        ticket=self.ok(await self.call('file_download_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']}))['ticket']
        lease=self.ok((await self.stream('open_get',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:]}))['result'])['lease_id']
        await asyncio.sleep(1.05)
        self.assertEqual((await self.stream('read_chunk',{'lease_id':lease,'offset':0,'max_bytes':3}))['body'],b'abc')
        await self.stream('close',{'lease_id':lease,'reason':'complete'})
        self.error((await self.stream('open_get',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:]}))['result'],'ticket_expired')
    async def test_disable_aborts_open_transfer_before_next_chunk(self):
        await self.start();task=await self.task();ticket=await self.ticket(task['task_id'],b'ab')
        lease=self.ok((await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':2}))['result'])['lease_id']
        await self.runtime.disable_owner(owner(self.fixture.principal))
        self.assertFalse((await self.stream('put_chunk',{'lease_id':lease,'offset':0},b'ab'))['result']['ok'])
        self.assertEqual(self.fixture.sql("SELECT count(*) FROM remote_artifacts WHERE state='sealed'")[0][0],0)
    async def test_bad_sequence_frame_closes_without_tool_effect(self):
        await self.start();f=self.fixture;left,right=socket.socketpair();left.setblocking(False);right.setblocking(False)
        self.addCleanup(left.close);self.addCleanup(right.close)
        await self.runtime.attach(left,f.typed('Principal',f.principal))
        await raw_send(right,{'v':1,'seq':0,'op':'bind','body_bytes':0,'args':{'config_digest':f.digest(),'principal':f.principal}})
        self.assertTrue((await raw_read(right))[0]['result']['ok'])
        await raw_send(right,{'v':1,'seq':2,'op':'tool','body_bytes':0,'args':{'tool':'task_create','arguments':{'request_id':uid(),'task_group_id':uid()}}})
        response,_=await raw_read(right);self.error(response['result'],'invalid_request')
        async with asyncio.timeout(2):self.assertEqual(await asyncio.get_running_loop().sock_recv(right,1),b'')
        self.assertEqual(f.sql('SELECT count(*) FROM remote_tasks')[0][0],0)
    async def test_declared_header_over_limit_closes_before_allocating_body(self):
        await self.start();f=self.fixture;left,right=socket.socketpair();left.setblocking(False);right.setblocking(False)
        self.addCleanup(left.close);self.addCleanup(right.close)
        await self.runtime.attach(left,f.typed('Principal',f.principal))
        await asyncio.get_running_loop().sock_sendall(right,struct.pack('!I',f.core['limits']['control_bytes']+1))
        async with asyncio.timeout(2):self.assertEqual(await asyncio.get_running_loop().sock_recv(right,1),b'')
    async def test_separate_process_runtime_owns_bytes_and_survives_frontend_loss(self):
        f=self.fixture;ctx=multiprocessing.get_context('spawn');left,right=socket.socketpair();right.setblocking(False)
        parent,child=ctx.Pipe();process=ctx.Process(target=runtime_child,args=(canon(f.base),canon(f.core),canon(f.manifest),left,child))
        process.start();left.close();child.close()
        self.addCleanup(right.close);self.addCleanup(parent.close)
        async def cleanup():
            if process.is_alive():parent.send('close')
            await asyncio.to_thread(process.join,10)
            if process.is_alive():process.kill();await asyncio.to_thread(process.join)
        self.addAsyncCleanup(cleanup)
        async with asyncio.timeout(10):ready=await asyncio.to_thread(parent.recv)
        self.assertTrue(ready.get('ready'),ready);self.assertNotEqual(ready['pid'],__import__('os').getpid())
        self.client=mod('remote_ipc').CoreClient(right,principal=f.typed('Principal',f.principal),config_digest=f.digest(),limits=f.typed('CoreLimits',f.core['limits']))
        await self.client.bind();task=await self.task();artifact,_=await self.upload(task['task_id'],(HERE/'fixtures/binary.bin').read_bytes())
        actual,_=await self.download(task['task_id'],artifact['artifact_id']);self.assertEqual(actual,(HERE/'fixtures/binary.bin').read_bytes())
        await self.client.close();self.assertTrue(process.is_alive());parent.send('close')
        async with asyncio.timeout(10):closed=await asyncio.to_thread(parent.recv)
        self.assertTrue(closed.get('closed'),closed);await asyncio.to_thread(process.join,5);self.assertEqual(process.exitcode,0)
    async def test_unsealed_renewal_requires_stopped_writer_and_exact_metadata(self):
        await self.start();task=await self.task();ticket=await self.ticket(task['task_id'],b'ab')
        artifact=ticket['artifact_id'];opened=await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':2})
        lease=self.ok(opened['result'])['lease_id']
        request={'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact,'filename':'opaque.bin','bytes':2,'sha256':sha(b'ab'),'media_type':'application/octet-stream'}
        self.assertFalse((await self.call('file_upload_ticket',request))['ok'])
        self.ok((await self.stream('close',{'lease_id':lease,'reason':'abort'}))['result'])
        self.assertFalse((await self.call('file_upload_ticket',dict(request,request_id=uid(),sha256=sha(b'xy'))))['ok'])
        renewed=self.ok(await self.call('file_upload_ticket',dict(request,request_id=uid())))['ticket']
        self.assertEqual(renewed['artifact_id'],artifact);self.assertNotEqual(renewed['ticket_id'],ticket['ticket_id'])
    async def test_lost_finish_ack_is_resolved_by_task_inspection_not_second_put(self):
        await self.start();task=await self.task();artifact,ticket=await self.upload(task['task_id'],b'committed')
        # Discard receipt and connection as a frontend would after ACK loss.
        await self.client.close();self.client=await self.connect()
        descriptors=self.ok(await self.call('task_get',{'task_id':task['task_id']}))['task']['artifacts']
        self.assertEqual([a['artifact_id'] for a in descriptors],[artifact['artifact_id']]);self.assertEqual(descriptors[0]['state'],'sealed')
        self.error((await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':9}))['result'],'ticket_consumed')
    async def test_key_rotation_restart_invalidates_prior_capability(self):
        await self.start();task=await self.task();artifact,_=await self.upload(task['task_id'],b'persist')
        ticket=self.ok(await self.call('file_download_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']}))['ticket']
        await self.stack.aclose();self.fixture.core.update(ticket_key='11'*32,ticket_key_version='synthetic-v2');await self.start()
        self.assertFalse((await self.stream('open_get',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:]}))['result']['ok'])
        data,new=await self.download(task['task_id'],artifact['artifact_id']);self.assertEqual(data,b'persist');self.assertNotEqual(new['authorization'],ticket['authorization'])
    async def test_delete_revokes_existing_get_before_next_chunk(self):
        await self.start();task=await self.task();artifact,_=await self.upload(task['task_id'],b'abc')
        ticket=self.ok(await self.call('file_download_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']}))['ticket']
        lease=self.ok((await self.stream('open_get',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:]}))['result'])['lease_id']
        self.ok(await self.call('task_delete',{'task_id':task['task_id'],'request_id':uid()}))
        self.assertFalse((await self.stream('read_chunk',{'lease_id':lease,'offset':0,'max_bytes':3}))['result']['ok'])

class Revision2TransferLifetimeExam(CoreCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fixture.core['limits'].update(transfer_idle_ms=100,transfer_total_ms=100,ipc_idle_ms=100)
        await self.start()

    async def test_exhausted_total_idle_budget_has_no_error_delivery_grace(self):
        from .support import revision_http,observe_asgi,observe_endpoint_cleanup
        app,scope,_,_,_=await revision_http(self,'put');sent=[];never=asyncio.Event()
        async def receive():await never.wait()
        async def send(message):sent.append(message);await never.wait()
        session=self.runtime.sessions[self.client.connection_id]
        done,error=await observe_asgi(app,scope,receive,send,window=.16)
        self.assertTrue(done,'No second100ms error-delivery allowance is available')
        self.assertEqual(sent,[])
        await observe_endpoint_cleanup(self,session)
        self.assertFalse(self.runtime.leases)

    async def test_empty_chunks_do_not_renew_idle(self):
        from .support import revision_http,observe_asgi,observe_endpoint_cleanup
        app,scope,_,_,_=await revision_http(self,'put');count=0;sent=[]
        async def receive():
            nonlocal count
            count+=1;await asyncio.sleep(.02)
            return {'type':'http.request','body':b'','more_body':True}
        async def send(message):sent.append(message)
        session=self.runtime.sessions[self.client.connection_id]
        done,error=await observe_asgi(app,scope,receive,send)
        self.assertTrue(done);self.assertGreater(count,1);self.assertLess(count,12)
        self.assertEqual(sent,[])
        await observe_endpoint_cleanup(self,session)
        self.assertFalse(self.runtime.leases)

    async def test_acknowledged_nonempty_progress_renews_idle_not_total(self):
        from .test_compatibility import lifespan
        from .support import observe_asgi,observe_endpoint_cleanup
        # Three80ms chunks span150ms idle.1000ms permits success;200ms total
        # must expire despite acknowledged progress. These are fixture milliseconds.
        for total,expected in ((1000,[201]),(200,[])):
            with self.subTest(total_ms=total):
                await self.stack.aclose()
                self.fixture.core['limits'].update(transfer_idle_ms=150,transfer_total_ms=total,ipc_idle_ms=total)
                await self.start();task=await self.task();ticket=await self.ticket(task['task_id'],b'abc')
                f=self.fixture;app=mod('remote_http').create_test_app(core_client=self.client,principal=self.runtime.config.principal,
                    config=f.typed('FrontendConfig',{'config_digest':f.digest(),'limits':f.core['limits'],'transfer_origin':f.core['transfer_origin']}))
                await self.stack.enter_async_context(lifespan(app));chunks=iter((b'a',b'b',b'c'));sent=[];index=0
                async def receive():
                    nonlocal index
                    await asyncio.sleep(.08);index+=1
                    return {'type':'http.request','body':next(chunks),'more_body':index<3}
                async def send(message):sent.append(message)
                scope={'type':'http','path':'/transfers/'+ticket['ticket_id'],'query_string':b'','method':'PUT',
                    'headers':[(b'authorization',ticket['authorization'].encode()),(b'content-length',b'3'),(b'content-type',b'application/octet-stream')]}
                session=self.runtime.sessions[self.client.connection_id]
                done,error=await observe_asgi(app,scope,receive,send,window=.7)
                self.assertTrue(done)
                self.assertEqual([m['status'] for m in sent if m['type']=='http.response.start'],expected)
                if expected:self.assertIsNone(error)
                else:await observe_endpoint_cleanup(self,session)
                self.assertFalse(self.runtime.leases)

    async def test_lost_http_put_receipt_exposes_seal_without_second_put(self):
        from .support import revision_http,observe_asgi
        app,scope,receive,task,ticket=await revision_http(self,'put');starts=[];never=asyncio.Event()
        async def send(message):
            if message['type']=='http.response.start':starts.append(message['status'])
            else:await never.wait()
        done,error=await observe_asgi(app,scope,receive,send)
        self.assertTrue(done);self.assertEqual(starts,[201])
        self.assertTrue(self.client.closed,'Exhausted receipt retires indeterminate shared endpoint')
        self.client=await self.connect()
        artifacts=self.ok(await self.call('task_get',{'task_id':task['task_id']}))['task']['artifacts']
        self.assertEqual([(x['artifact_id'],x['state']) for x in artifacts],[(ticket['artifact_id'],'sealed')])
        self.error((await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':1}))['result'],'ticket_consumed')

    async def test_shared_client_expiry_does_not_cancel_accepted_worker(self):
        from .support import revision_http,observe_asgi
        # IPC includes synthetic native preflight;5s permits startup while HTTP idle stays100ms.
        await self.stack.aclose();self.fixture.core['limits'].update(ipc_idle_ms=5000,transfer_total_ms=5000)
        await self.start()
        # Keep the worker alive independently of the expiring frontend operation.
        self.fixture.control(hang=True)
        task=await self.task();job=self.ok(await self.call('delegate',self.args(task)))['job']['job_id']
        async with asyncio.timeout(5):
            while not any(c.get('kind')=='exec' for c in self.fixture.calls()):await asyncio.sleep(.01)
        app,scope,receive,_,_=await revision_http(self,'get');never=asyncio.Event();headers=asyncio.Event()
        async def send(message):
            if message['type']=='http.response.start':headers.set();return
            await never.wait()
        operation=asyncio.create_task(observe_asgi(app,scope,receive,send))
        await headers.wait()
        # Hold the existing serialization primitive so the concurrent MCP call is
        # genuinely queued when the HTTP lifetime expires; no fake response codec.
        await self.client.lock.acquire()
        other=asyncio.create_task(self.client.call('job_status',{'task_id':task['task_id'],'job_id':job}))
        await asyncio.sleep(0);self.assertFalse(other.done())
        try:done,error=await operation
        finally:self.client.lock.release()
        self.assertTrue(done)
        with self.assertRaises(mod('remote_schemas').RemoteError):await other
        self.client=await self.connect()
        state=self.ok(await self.call('job_status',{'task_id':task['task_id'],'job_id':job}))['job']['state']
        self.assertNotIn(state,('cancelled','interrupted','failed','timed_out'))
        self.assertEqual(sum(c.get('kind')=='exec' for c in self.fixture.calls()),1)
        self.ok(await self.call('job_cancel',{'task_id':task['task_id'],'job_id':job}))


from unittest.mock import patch
from .support import FatalCapture, event_barrier, monitor_fixture


class MonitorStreamWaitExam(CoreCase):
    async def stream_wait(self, lose):
        gate = await monitor_fixture(self)
        task = await self.task()
        ticket = await self.ticket(task['task_id'], b'new')
        opened = await self.stream('open_put', {'ticket_id': ticket['ticket_id'],
            'capability': ticket['authorization'][7:], 'content_length': 3})
        lease_id = self.ok(opened['result'])['lease_id']
        transfer = mod('file_transfer')
        descriptor = self.runtime.descriptors[lease_id]
        identity = transfer.os.fstat(descriptor)
        writes = []
        write = transfer.os.write
        def observed_write(fd, data):
            info = transfer.os.fstat(fd)
            if fd == descriptor and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                writes.append(bytes(data))
            return write(fd, data)
        entered = asyncio.Event()
        release = asyncio.Event()
        ipc = mod('remote_ipc')
        read = ipc._read
        async def paused_read(sock, limits):
            frame = await read(sock, limits)
            if frame[0].get('op') == 'put_chunk' and 'args' in frame[0]:
                entered.set()
                await release.wait()
            return frame
        signal = FatalCapture(self)
        observations = []
        authorize = self.runtime.authorize
        def observe(principal, operation=None):
            if operation == 'put_chunk':
                observations.append((self.runtime.monitor_task.done(), len(signal.attempts)))
            return authorize(principal, operation)
        with patch.object(ipc, '_read', paused_read), patch.object(self.runtime, 'authorize', observe), \
                patch.object(transfer.os, 'write', observed_write):
            # Drain the read already pending before instrumentation; the next frame
            # is read by the same real server with the scheduling seam installed.
            self.ok(await self.call('task_get', {'task_id': task['task_id']}))
            pending = asyncio.create_task(self.stream('put_chunk',
                {'lease_id': lease_id, 'offset': 0}, b'new'))
            await asyncio.wait_for(entered.wait(), 1)
            if lose:
                gate.release.set()
            release.set()
            try:
                result = await asyncio.wait_for(pending, 2)
            except mod('remote_schemas').RemoteError as error:
                self.assertTrue(lose)
                self.assertEqual(error.code, 'storage_error')
                result = None
        if lose:
            self.assertEqual(observations[0], (True, 0))
            if result is not None:
                self.error(result['result'], 'storage_error')
            self.assertEqual(writes, [], 'Lost monitor must precede actual PUT descriptor writes')
            self.assertEqual(self.fixture.sql(
                'SELECT observed_bytes, observed_hash FROM remote_artifacts'), [(None, None)])
            await event_barrier()
            signal.assert_one(self)
            self.assertFalse(self.runtime.leases)
            self.assertFalse(self.runtime.descriptors)
        else:
            self.assertEqual(self.ok(result['result'])['offset'], 3)
            self.assertEqual(b''.join(writes), b'new', 'Healthy control must exercise the real write')
            self.assertEqual(self.fixture.sql(
                'SELECT observed_bytes, observed_hash FROM remote_artifacts'), [(None, None)])
            self.ok((await self.stream('close', {'lease_id': lease_id, 'reason': 'abort'}))['result'])
            self.assertFalse(self.runtime.fatal_failed)

    async def test_monitor_loss_while_receiving_frame_precedes_byte_effect(self):
        await self.stream_wait(True)

    async def test_healthy_frame_wait_writes_then_cleanup_only_close(self):
        await self.stream_wait(False)

    async def test_bind_post_read_checks_monitor_before_queued_observer(self):
        gate = await monitor_fixture(self)
        ipc = mod('remote_ipc')
        read = ipc._read
        entered = asyncio.Event()
        release = asyncio.Event()
        async def pause_bind(sock, limits):
            frame = await read(sock, limits)
            if frame[0].get('op') == 'bind' and 'args' in frame[0]:
                entered.set()
                await release.wait()
            return frame
        signal = FatalCapture(self)
        observed = []
        authorize = self.runtime.authorize
        def observe(principal, operation=None):
            if operation == 'bind':
                observed.append((self.runtime.monitor_task.done(), len(signal.attempts)))
            return authorize(principal, operation)
        with patch.object(ipc, '_read', pause_bind), patch.object(self.runtime, 'authorize', observe):
            pending = asyncio.create_task(self.connect())
            await asyncio.wait_for(entered.wait(), 1)
            gate.release.set()
            release.set()
            with self.assertRaises(mod('remote_schemas').RemoteError) as raised:
                await asyncio.wait_for(pending, 2)
            self.assertEqual(raised.exception.code, 'storage_error')
        self.assertEqual(observed[0], (True, 0))
        await event_barrier()
        signal.assert_one(self)
