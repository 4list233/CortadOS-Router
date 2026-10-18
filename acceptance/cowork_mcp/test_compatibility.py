# PURPOSE: Exercise synthetic HTTP composition and preserve ordinary/default ownership.
# CONTRACT: RT-COWORK-MCP-008 AC04,08–10,15. No public listener or authenticated deployment.
import asyncio
from contextlib import asynccontextmanager
import json
import socket
from pathlib import Path
from unittest.mock import patch
import httpx
from .support import CoreCase,TOOLS,canon,mod,uid,observe_endpoint_cleanup

@asynccontextmanager
async def lifespan(app):
    incoming=asyncio.Queue();outgoing=asyncio.Queue()
    task=asyncio.create_task(app({'type':'lifespan','asgi':{'version':'3.0'}},incoming.get,outgoing.put))
    await incoming.put({'type':'lifespan.startup'})
    async with asyncio.timeout(10):message=await outgoing.get()
    if message['type']!='lifespan.startup.complete':
        task.cancel();raise AssertionError(message)
    try:yield
    finally:
        await incoming.put({'type':'lifespan.shutdown'})
        async with asyncio.timeout(10):message=await outgoing.get();await task
        if message['type']!='lifespan.shutdown.complete':raise AssertionError(message)

class CompatibilityExam(CoreCase):
    def app(self):
        f=self.fixture
        return mod('remote_http').create_test_app(core_client=self.client,principal=f.typed('Principal',f.principal),
            config=f.typed('FrontendConfig',{'config_digest':f.digest(),'limits':f.core['limits'],'transfer_origin':f.core['transfer_origin']}))
    async def http(self,app):
        await self.stack.enter_async_context(lifespan(app))
        return await self.stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://cowork.invalid'))
    async def test_exact_routes_methods_queries_no_redirect_or_listener(self):
        await self.start()
        with patch.object(socket.socket,'bind',side_effect=AssertionError('App must not bind listener')):app=self.app()
        client=await self.http(app)
        for method,url,status,allow in [('GET','/mcp',405,'POST'),('DELETE','/mcp',405,'POST'),
            ('POST','/mcp/',404,None),('GET','/health',404,None),('GET','/mcp?q=1',400,None),
            ('GET','/transfers/not-a-uuid',404,None),('HEAD','/transfers/'+uid(),405,'GET, PUT')]:
            with self.subTest(method=method,url=url):
                response=await client.request(method,url)
                self.assertEqual(response.status_code,status);self.assertEqual(response.headers.get('cache-control'),'no-store')
                self.assertNotIn('location',response.headers)
                if allow:self.assertEqual(response.headers.get('allow'),allow)
    async def test_mcp_initialization_tools_and_failure_preserve_structured_json(self):
        await self.start();client=await self.http(self.app());headers={'Accept':'application/json, text/event-stream'}
        async def rpc(method,params,identity):
            response=await client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':identity,'method':method,'params':params})
            self.assertEqual(response.status_code,200,response.text);self.assertNotIn('mcp-session-id',response.headers)
            return response.json()['result']
        initial=await rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'independent-exam','version':'1'}},1)
        self.assertEqual(initial['serverInfo'],{'name':'cortados-studio-workers','version':'1.0.0'})
        self.assertIn('Workers do not inherit your conversation',initial['instructions'])
        listed=await rpc('tools/list',{},2);self.assertEqual({t['name'] for t in listed['tools']},set(TOOLS))
        self.assertTrue(all(t.get('description') and t.get('inputSchema') for t in listed['tools']))
        failed=await rpc('tools/call',{'name':'task_get','arguments':{'task_id':uid()}},3)
        self.assertIs(failed['isError'],True);self.assertEqual(json.loads(failed['content'][0]['text']),failed['structuredContent'])
        self.error(failed['structuredContent'],'task_not_found')
    async def test_http_raw_binary_and_unicode_disposition(self):
        await self.start();task=await self.task();data=b'\x00\xffbinary\x00';ticket=await self.ticket(task['task_id'],data,'résumé "Q1".bin')
        client=await self.http(self.app())
        response=await client.put(ticket['url'],headers={'Authorization':ticket['authorization'],'Content-Type':'application/octet-stream'},content=data)
        self.assertEqual(response.status_code,201,response.text);artifact=response.json()['artifact']
        ticket=self.ok(await self.call('file_download_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']}))['ticket']
        response=await client.get(ticket['url'],headers={'Authorization':ticket['authorization']})
        self.assertEqual(response.status_code,200);self.assertEqual(response.content,data)
        self.assertIn("filename*=UTF-8''",response.headers['content-disposition'])
        self.assertIn('r%C3%A9sum%C3%A9',response.headers['content-disposition'])
        self.assertEqual(response.headers['cache-control'],'no-store')
        ranged=await client.get(ticket['url'],headers={'Authorization':ticket['authorization'],'Range':'bytes=0-1'})
        self.assertEqual(ranged.status_code,416)
    async def test_oversized_mcp_body_never_calls_core(self):
        await self.start();client=await self.http(self.app())
        with patch.object(type(self.client),'call',side_effect=AssertionError('Oversize reached core')):
            response=await client.post('/mcp',content=b'x'*(self.fixture.core['limits']['control_bytes']+1))
        self.assertEqual(response.status_code,413)
    async def test_app_rejects_double_adoption_and_wrong_principal(self):
        await self.start();self.app()
        with self.assertRaises(mod('remote_schemas').RemoteError):self.app()
    async def test_frontend_disconnect_preserves_accepted_job_and_reconnect(self):
        self.fixture.control(hang=True);await self.start();task=await self.task();job=self.ok(await self.call('delegate',self.args(task)))['job']
        app=self.app()
        async with lifespan(app):pass
        self.client=await self.connect()
        state=self.ok(await self.call('job_status',{'task_id':task['task_id'],'job_id':job['job_id']}))['job']['state']
        self.assertIn(state,('queued','running'))
        (self.fixture.path/'native/release-worker').touch()
        result=await self.terminal(task['task_id'],job['job_id']);self.assertEqual(result['job']['state'],'succeeded')
    async def test_handoff_override_reaches_existing_s1_fit_and_default_is_unchanged(self):
        from tests.test_mcp_route_task import RouteContract,decision
        from cortados_router import route_task,core
        fixture=RouteContract();fixture.setUp();self.addCleanup(fixture.doCleanups)
        request=dict(fixture.request,selection={'mode':'s1'})
        ordinary=route_task.handoff(request);packet=ordinary+'\nindependent appendix λ\n'
        chosen={'band':'R0','D':1,'subagents':0,'verifier':False,'skipped':[],
            'chosen':{'provider':'codex-cli','model':'gpt-6-luna'}}
        with patch.object(core,'decide',return_value=decision()),patch.object(core,'resolve',return_value=chosen) as resolve:
            result=route_task.route_task(request,fixture.config,fixture.metadata,handoff_packet=packet)
            self.assertEqual(resolve.call_args.kwargs['input_fit']['input_bytes'],len(packet.encode()))
            self.assertEqual(result['context']['worker_bytes'],len(packet.encode()))
            result=route_task.route_task(request,fixture.config,fixture.metadata)
            self.assertEqual(resolve.call_args.kwargs['input_fit']['input_bytes'],len(ordinary.encode()))
            self.assertEqual(result['context']['worker_bytes'],len(ordinary.encode()))
    async def test_live_training_prefix_exam_rows_and_protected_model_inputs(self):
        # Read-only current guard, explicitly distinct from original historical007 assertions.
        from cortados_router.learning_data import bind_snapshot
        from .support import sha
        before=json.loads(Path('/Users/velox/.cortados-builds/training-teacher-verification/data-before.json').read_text())
        root=Path('/Users/velox/cortados/train');data=(root/'data/labels.jsonl').read_bytes()
        self.assertEqual(sha(data[:before['label_bytes']]),before['label_sha256'])
        snapshot=bind_snapshot(root/'data')
        exam=[line for line in data.splitlines(keepends=True) if snapshot['partitions'].get(json.loads(line)['id']) in ('valid','holdout')]
        self.assertEqual(len(exam),before['exam_label_lines']);self.assertEqual(sha(b''.join(exam)),before['exam_label_sha256'])
        for relative,expected in before['immutable_files'].items():self.assertEqual(sha((root/relative).read_bytes()),expected,relative)
    async def test_ordinary_bootstrap_has_identical_envelope_and_no_remote_discriminator(self):
        from types import SimpleNamespace
        from tests.test_mcp_route_task import RouteContract,snapshot
        from integrations.mcp import broker,native_metadata,native_process
        from integrations.mcp.schemas import validate
        from cortados_router import route_task
        from .support import FIXTURE
        class Captured(BaseException):pass
        class Writer:
            def __init__(self):self.frames=[]
            def write(self,data):self.frames.append(bytes(data))
            async def drain(self):raise Captured()
            def close(self):pass
        writer=Writer();processes=[]
        async def intercept(*args,**kwargs):processes.append(args);return SimpleNamespace(stdin=writer)
        case=RouteContract();case.setUp();self.addCleanup(case.doCleanups)
        config=dict(case.config,state_dir=str(self.fixture.path/'state'))
        metadata=SimpleNamespace(preflight=lambda:None,version='0.163.0',snapshot=snapshot)
        args=validate('delegate',dict(case.request,cwd=str(self.fixture.path),request_id=uid(),timeout_s=10),config)
        with patch.object(native_metadata,'NativeMetadata',return_value=metadata),patch.object(native_process,'host_boot_id',return_value='synthetic-boot'),patch.object(asyncio,'create_subprocess_exec',new=intercept):
            runtime=broker.Broker(config,'synthetic-digest');self.addCleanup(runtime.store.close)
            route=route_task.route_task(args,config,metadata);record,created=runtime.store.accept(args,route,cli_version='0.163.0');self.assertTrue(created)
            with self.assertRaises(Captured):await runtime.run_worker(record)
        self.assertEqual(len(writer.frames),1);value=json.loads(writer.frames[0])
        self.assertEqual(set(value),set(FIXTURE['captured_bootstrap']))
        self.assertEqual(value['arguments'],record['arguments']);self.assertEqual(value['route'],record['route'])
        self.assertNotIn('--execution-kind',processes[0]);self.assertNotIn('execution_kind',record)
    async def test_ordinary_native_argv_and_environment_are_unchanged(self):
        import os
        from .support import FIXTURE
        from integrations.mcp.codex import native_arguments,native_environment
        bootstrap=FIXTURE['captured_bootstrap']
        actual=native_arguments(bootstrap['config'],bootstrap['arguments'],bootstrap['route'],'/synthetic/final.txt')
        self.assertEqual(actual,FIXTURE['ordinary_native_argv'])
        with patch.dict(os.environ,{'HOME':'/synthetic/home','PATH':'/synthetic/bin','OPENAI_API_KEY':'NEVER-PASS','CORTADOS_TOKEN':'NEVER-PASS'},clear=True):
            self.assertEqual(native_environment(bootstrap['config']),{'HOME':'/synthetic/home','PATH':'/synthetic/bin','CODEX_HOME':bootstrap['config']['codex_home']})


class BuildHttpDeadlineRegressionExam(CoreCase):
    async def test_stalled_http_put_receive_terminates_at_idle_deadline(self):
        import asyncio
        self.fixture.core['limits'].update(transfer_idle_ms=100,transfer_total_ms=5000,ipc_idle_ms=5000)
        await self.start();task=await self.task();ticket=await self.ticket(task['task_id'],b'x')
        api=mod('remote_schemas');config=api.FrontendConfig(config_digest=self.fixture.digest(),limits=self.fixture.core['limits'],transfer_origin='https://cowork.invalid')
        app=mod('remote_http').create_test_app(core_client=self.client,principal=self.runtime.config.principal,config=config)
        entered=asyncio.Event();never=asyncio.Event();sent=[]
        async def receive():entered.set();await never.wait()
        async def send(message):sent.append(message)
        scope={'type':'http','path':'/transfers/'+ticket['ticket_id'],'query_string':b'','method':'PUT','headers':[(b'authorization',ticket['authorization'].encode()),(b'content-length',b'1'),(b'content-type',b'application/octet-stream')]}
        await self.stack.enter_async_context(lifespan(app))
        session=self.runtime.sessions[self.client.connection_id]
        pending=asyncio.create_task(app(scope,receive,send))
        try:
            await entered.wait()
            # 500ms observation window comfortably exceeds100ms synthetic idle;
            # it is only test completion slack, never a transport jitter allowance.
            try:await asyncio.wait_for(asyncio.shield(pending),.5)
            except mod('remote_schemas').RemoteError as error:
                self.assertEqual((error.code,error.message),('timeout','Remote operation refused: timeout'))
            self.assertTrue(pending.done())
            self.assertEqual([m['status'] for m in sent if m['type']=='http.response.start'],[])
            await observe_endpoint_cleanup(self,session)
            self.assertFalse(self.runtime.leases)
        finally:
            if not pending.done():pending.cancel()
            try:await pending
            except asyncio.CancelledError:pass
            except mod('remote_schemas').RemoteError as error:
                self.assertEqual((error.code,error.message),('timeout','Remote operation refused: timeout'))

    async def test_stalled_http_get_send_aborts_after_headers_at_idle_deadline(self):
        import asyncio
        self.fixture.core['limits'].update(transfer_idle_ms=100,transfer_total_ms=5000,ipc_idle_ms=5000)
        await self.start();task=await self.task();artifact,_=await self.upload(task['task_id'],b'x')
        ticket=self.ok(await self.call('file_download_ticket',{'task_id':task['task_id'],'request_id':uid(),'artifact_id':artifact['artifact_id']}))['ticket']
        api=mod('remote_schemas');config=api.FrontendConfig(config_digest=self.fixture.digest(),limits=self.fixture.core['limits'],transfer_origin='https://cowork.invalid')
        app=mod('remote_http').create_test_app(core_client=self.client,principal=self.runtime.config.principal,config=config)
        entered=asyncio.Event();never=asyncio.Event();headers=[]
        async def receive():return {'type':'http.request','body':b'','more_body':False}
        async def send(message):
            if message['type']=='http.response.start':headers.append(message['status']);return
            entered.set();await never.wait()
        scope={'type':'http','path':'/transfers/'+ticket['ticket_id'],'query_string':b'','method':'GET','headers':[(b'authorization',ticket['authorization'].encode())]}
        await self.stack.enter_async_context(lifespan(app));session=self.runtime.sessions[self.client.connection_id]
        pending=asyncio.create_task(app(scope,receive,send))
        try:
            await entered.wait();await asyncio.wait({pending},timeout=.5)
            self.assertTrue(pending.done(),'Backpressure must not outlive the100ms idle lease indefinitely')
            self.assertEqual(headers,[200])
            await observe_endpoint_cleanup(self,session)
            self.assertFalse(self.runtime.leases)
        finally:
            if not pending.done():pending.cancel()
            try:await pending
            except (asyncio.CancelledError,Exception):pass # An abort after headers is the required disposition.

class Revision2HttpExam(CoreCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fixture.core['limits'].update(transfer_idle_ms=100,transfer_total_ms=500,ipc_idle_ms=500)
        await self.start()

    async def test_all_response_families_timely_status_controls(self):
        from .support import revision_http,observe_asgi
        for family,status in [('put',201),('get',200),('range',416)]:
            with self.subTest(family=family):
                app,scope,receive,_,_=await revision_http(self,family);sent=[]
                async def send(message):sent.append(message)
                done,error=await observe_asgi(app,scope,receive,send)
                self.assertTrue(done);self.assertIsNone(error)
                self.assertEqual([m['status'] for m in sent if m['type']=='http.response.start'],[status])
        for code,status in [('invalid_request',400),('ticket_invalid',401),('permission_refused',403),
                            ('artifact_not_found',404),('transfer_busy',409),('output_limit',413),
                            ('capacity_exhausted',507),('storage_error',503)]:
            with self.subTest(code=code):
                app,scope,receive,_,_=await revision_http(self);sent=[]
                async def send(message):sent.append(message)
                async def refuse(*args,**kwargs):return {'result':mod('remote_schemas').RemoteError(code).payload(),'body':b''}
                with patch.object(self.client,'stream',refuse):done,error=await observe_asgi(app,scope,receive,send)
                self.assertTrue(done);self.assertIsNone(error)
                self.assertEqual([m['status'] for m in sent if m['type']=='http.response.start'],[status])

    async def test_all_response_families_stalled_start_or_body_abort_once(self):
        from .support import revision_http,observe_asgi
        import contextlib
        cases=[('put',None),('get',None),('range',None)]+[('get',c) for c in
            ('invalid_request','ticket_invalid','permission_refused','artifact_not_found','transfer_busy','output_limit','capacity_exhausted','storage_error')]
        for family,code in cases:
            for stage in ('http.response.start','http.response.body'):
                with self.subTest(family=family,code=code,stage=stage):
                    app,scope,receive,_,_=await revision_http(self,family);sent=[];never=asyncio.Event()
                    async def send(message):
                        sent.append(message)
                        if message['type']==stage:await never.wait()
                    async def refuse(*args,**kwargs):return {'result':mod('remote_schemas').RemoteError(code).payload(),'body':b''}
                    session=self.runtime.sessions[self.client.connection_id]
                    with patch.object(self.client,'stream',refuse) if code else contextlib.nullcontext():
                        done,error=await observe_asgi(app,scope,receive,send)
                    self.assertTrue(done,'Response backpressure exceeded original idle deadline')
                    self.assertEqual(sum(m['type']=='http.response.start' for m in sent),1)
                    await observe_endpoint_cleanup(self,session)
                    self.assertFalse(self.runtime.leases)

    async def test_header_attempt_failure_cannot_emit_replacement_start(self):
        from .support import revision_http,observe_asgi
        for family in ('put','get','range'):
            with self.subTest(family=family):
                app,scope,receive,_,_=await revision_http(self,family);sent=[]
                async def send(message):
                    sent.append(message)
                    if message['type']=='http.response.start':raise TimeoutError('synthetic partial headers')
                done,error=await observe_asgi(app,scope,receive,send)
                self.assertTrue(done);self.assertEqual(sum(m['type']=='http.response.start' for m in sent),1)

    async def test_timely_preheader_timeout_uses_positive_remaining_budget(self):
        from .support import revision_http,observe_asgi
        app,scope,_,_,_=await revision_http(self,'put');sent=[]
        async def early_timeout():raise TimeoutError('synthetic upstream wait failed before local deadline')
        async def send(message):sent.append(message)
        done,error=await observe_asgi(app,scope,early_timeout,send)
        self.assertTrue(done);self.assertIsNone(error)
        self.assertEqual([m['status'] for m in sent if m['type']=='http.response.start'],[408])
        self.assertFalse(self.runtime.leases)
