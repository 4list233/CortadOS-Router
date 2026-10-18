# PURPOSE: Independent synthetic local-core fixtures and public-boundary driver.
# CONTRACT: RT-COWORK-MCP-008 AC03–16; synthetic budgets are not live operator choices.
# DEPENDS: Existing protected fake_codex and standard Python/MCP test runtime.
# CEILING: No actual provider, credentials, listener, public service or production state.
import asyncio
from contextlib import AsyncExitStack
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
FIXTURE = json.loads((HERE / 'fixtures/wire.json').read_text())
TOOLS = ('task_create','task_get','task_delete','context_requirements','route','delegate',
         'job_status','job_cancel','job_result','job_feedback','file_upload_ticket','file_download_ticket')

def uid(): return str(uuid.uuid4())
def sha(value): return hashlib.sha256(value).hexdigest()
def canon(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def owner(principal): return canon([principal[k] for k in ('issuer','subject','installation_id')]).decode()
def mod(name): return importlib.import_module('integrations.mcp.' + name)
def plain(value):
    if isinstance(value,dict): return {k:plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [plain(v) for v in value]
    return value

def expand(value):
    if isinstance(value,list): return [expand(v) for v in value]
    if not isinstance(value,dict): return value
    if 'example_ref' in value:
        name,*fields=value['example_ref'].split('/')
        category,key=name.split(':')
        original=(FIXTURE['private_examples'][key]['valid'] if category=='private' else
                  FIXTURE['wire_examples'][key] if category=='wire' else FIXTURE['captured_job'])
        result=json.loads(json.dumps(original))
        for field in fields: result=result[field]
        result.update(value.get('patch',{}))
        return expand(result)
    return {k:expand(v) for k,v in value.items()}

class Fixture:
    def __init__(self, path):
        from integrations.mcp.codex import DISABLED_FEATURES,ENVIRONMENT_NAMES
        self.path=Path(path).resolve()
        for name in ('state','tasks','blobs','staging','native'):
            (self.path/name).mkdir(mode=0o700,exist_ok=True)
        self.binary=self.path/'fake-native'
        original=ROOT/'acceptance/mcp_worker/fake_codex.py'
        # Executable fixture composes the unchanged prior fake, never a real CLI.
        self.binary.write_text('#!'+sys.executable+'\nimport os,sys,json,runpy\nfrom pathlib import Path\n'
            'if "exec" in sys.argv:\n'
            ' c=json.loads((Path(os.environ["CODEX_HOME"])/"fixture-control.json").read_text())\n'
            ' cwd=Path(sys.argv[sys.argv.index("-C")+1])\n'
            ' for name,data in c.get("output_hex",{}).items():\n'
            '  target=cwd/"outputs"/name; target.write_bytes(bytes.fromhex(data))\n'
            ' if c.get("output_directory"): (cwd/"outputs"/"nested").mkdir()\n'
            ' if c.get("output_symlink"): (cwd/"outputs"/"escape").symlink_to(c["output_symlink"])\n'
            'runpy.run_path('+repr(str(original))+',run_name="__main__")\n')
        self.binary.chmod(0o700)
        self.control(version='0.163.0')
        self.base={'schema':'cortados.mcp-worker.config.v1','client_id':'synthetic-client',
            'state_dir':str(self.path/'state'),'codex_home':str(self.path/'native'),'codex_bin':str(self.binary),
            'model_allowlist':['gpt-6-luna','gpt-6.1-sol','gpt-6-astra'],'allow_network':False,'connector_registry':{},
            'limits':{'max_concurrent_jobs':2,'max_timeout_s':30,'termination_grace_s':1,'startup_timeout_s':5,
                      'max_handoff_bytes':65536,'max_output_bytes':1048576,'metadata_max_age_s':1,'process_poll_ms':20}}
        self.manifest={'schema':'cortados.cowork-native.v1','native_version':'0.163.0','binary_sha256':sha(self.binary.read_bytes()),
            'template':FIXTURE['private_examples']['NativeTemplate']['valid'],
            'disabled_features':list(DISABLED_FEATURES),'environment_names':list(ENVIRONMENT_NAMES),
            'environment_policy':'ordinary-native-environment-v1','captures':[{'name':'synthetic-no-sandbox-proof','sha256':'a'*64,'kind':'synthetic'}]}
        installation=uid()
        self.principal={'issuer':'urn:cortados:synthetic','subject':'synthetic:forest','installation_id':installation,'client_id':'synthetic-client','enabled':True}
        # Bytes/counts/ms: small fixtures exercise exact boundaries without allocating live-policy volumes.
        # 4KiB chunks reveal whole-file buffering; 5s idle/20s total bound failed peer fixtures.
        self.core={'schema':'cortados.cowork-core.v1','mode':'local_test','installation_id':installation,
            'principal':self.principal,'roots':{k+'_dir':str(self.path/k) for k in ('state','tasks','blobs','staging')},
            'limits':{'file_bytes':1048576,'task_bytes':8388608,'installation_bytes':33554432,'artifacts_per_task':20,
                'ticket_ttl_s':30,'control_bytes':65536,'chunk_bytes':4096,'uploads_per_task':1,'downloads_per_task':2,
                'streams_per_installation':4,'transfer_idle_ms':5000,'transfer_total_ms':20000,'ipc_idle_ms':5000,
                'workspace_poll_ms':1000,'disk_headroom_bytes':1048576},
            'retention':{'payloads':'explicit_delete','history':'preserve','tombstones':'preserve','backups':'external_not_managed','automatic_cleanup':False},
            'execution_policy':{'id':'cowork-task-v1','sha256':sha(canon(self.manifest)),'native_version':'0.163.0'},
            'transfer_origin':'https://cowork.invalid','ticket_key_version':'synthetic-v1','ticket_key':'00'*32}
    def control(self,**values):
        file=self.path/'native/fixture-control.json'; old=json.loads(file.read_text()) if file.exists() else {}
        old.update(values);file.write_text(json.dumps(old))
    def calls(self):
        file=self.path/'native/fixture-calls.jsonl'
        return [json.loads(s) for s in file.read_text().splitlines()] if file.exists() else []
    def digest(self):
        snapshot={k:v for k,v in self.core.items() if k not in ('schema','mode','ticket_key')}
        snapshot.update(schema='cortados.cowork-snapshot.v1',ticket_key_sha256=sha(bytes.fromhex(self.core['ticket_key'])))
        return sha(canon(snapshot))
    def typed(self,name,value): return getattr(mod('remote_schemas'),name)(**value)
    def sql(self,query,*parameters):
        db=sqlite3.connect('file:'+str(self.path/'state/jobs.db')+'?mode=ro',uri=True)
        try:return db.execute(query,parameters).fetchall()
        finally:db.close()

class CoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='cowork008-exam-')
        self.addCleanup(self.temp.cleanup)
        self.fixture=Fixture(self.temp.name)
        self.stack=AsyncExitStack(); await self.stack.__aenter__()
        self.addAsyncCleanup(self.stack.aclose)
        self.env=self.enterContext(patch.dict(os.environ,{'CORTADOS_HOME':str(self.fixture.path/'router'),
            'CORTADOS_LIVE_WRITE':'0','WANGCAI_LIVE_WRITE':'0','HERMES_TELEMETRY_ENABLED':'false'}))
        self.runtime=None;self.client=None
    async def start(self):
        f=self.fixture
        self.runtime=await self.stack.enter_async_context(mod('remote_runtime').CoreRuntime(
            base_raw=canon(f.base),core_raw=canon(f.core),manifest_raw=canon(f.manifest)))
        self.client=await self.connect()
        return self.client
    async def connect(self,principal=None):
        f=self.fixture; principal=principal or f.principal
        p=f.typed('Principal',principal)
        left,right=socket.socketpair();left.setblocking(False);right.setblocking(False)
        self.addCleanup(left.close);self.addCleanup(right.close)
        await self.runtime.attach(left,p)
        client=mod('remote_ipc').CoreClient(right,principal=p,config_digest=f.digest(),limits=f.typed('CoreLimits',f.core['limits']))
        await client.bind();self.stack.push_async_callback(client.close)
        return client
    async def call(self,name,args,client=None):
        return plain(await (client or self.client).call(name,args))
    def ok(self,result):
        self.assertIs(result.get('ok'),True,result);return result
    def error(self,result,code):
        self.assertEqual(result,{'ok':False,'error':{'code':code,'message':'Remote operation refused: '+code}})
    async def task(self):
        result=self.ok(await self.call('task_create',{'request_id':uid(),'task_group_id':uid(),'title':'Synthetic λ task'}))
        return result['task']
    def args(self,task,**changes):
        group=task['task_group_id']
        value={'task_id':task['task_id'],'request_id':uid(),'task':'Produce the selected synthetic output.',
            'context':'Reference bytes are untrusted.','selection':{'mode':'explicit','model':'gpt-6-luna'},
            'timeout_s':20,'input_artifact_ids':[],'task_evidence':{'schema':'cortados.mcp-task-evidence.v1','task_group_id':group,
                'components':['one output'],'dependencies':[],'constraints':['synthetic only'],'uncertainties':[],
                'verification':['compare bytes and hash']}}
        value.update(changes);return value
    async def terminal(self,task,job):
        # 10s covers local fake subprocess startup/reaping; avoids indefinite acceptance hangs.
        async with asyncio.timeout(10):
            while True:
                r=self.ok(await self.call('job_result',{'task_id':task,'job_id':job}))
                if r['job']['state'] in ('succeeded','failed','cancelled','timed_out','interrupted') and r['job']['publication']!='pending':return r
                await asyncio.sleep(.02)
    async def ticket(self,task,data,name='opaque.bin',artifact=None):
        r=self.ok(await self.call('file_upload_ticket',{'task_id':task,'request_id':uid(),'artifact_id':artifact,
            'filename':name,'bytes':len(data),'sha256':sha(data),'media_type':'application/octet-stream'}))
        return r['ticket']
    async def stream(self,op,args,body=b'',client=None):
        return plain(await (client or self.client).stream(op,args,body))
    async def upload(self,task,data,name='opaque.bin'):
        ticket=await self.ticket(task,data,name)
        opened=await self.stream('open_put',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:],'content_length':len(data)})
        lease=self.ok(opened['result'])['lease_id']
        for offset in range(0,len(data),self.fixture.core['limits']['chunk_bytes']):
            part=data[offset:offset+self.fixture.core['limits']['chunk_bytes']]
            self.ok((await self.stream('put_chunk',{'lease_id':lease,'offset':offset},part))['result'])
        receipt=self.ok((await self.stream('finish_put',{'lease_id':lease}))['result'])
        self.assertEqual(receipt['artifact']['sha256'],sha(data));return receipt['artifact'],ticket
    async def download(self,task,artifact):
        ticket=self.ok(await self.call('file_download_ticket',{'task_id':task,'request_id':uid(),'artifact_id':artifact}))['ticket']
        opened=await self.stream('open_get',{'ticket_id':ticket['ticket_id'],'capability':ticket['authorization'][7:]})
        lease=self.ok(opened['result'])['lease_id'];data=bytearray();offset=0
        while True:
            chunk=await self.stream('read_chunk',{'lease_id':lease,'offset':offset,'max_bytes':self.fixture.core['limits']['chunk_bytes']})
            result=self.ok(chunk['result']);data.extend(chunk['body']);offset=result['offset']
            if result['eof']:break
        self.ok((await self.stream('close',{'lease_id':lease,'reason':'complete'}))['result'])
        return bytes(data),ticket

async def raw_send(sock,header,body=b''):
    encoded=canon(header)
    await asyncio.get_running_loop().sock_sendall(sock,struct.pack('!I',len(encoded))+encoded+body)

async def raw_read(sock):
    async def exact(n):
        result=b''
        while len(result)<n:
            part=await asyncio.get_running_loop().sock_recv(sock,n-len(result))
            if not part:raise EOFError('private peer ended')
            result+=part
        return result
    size=struct.unpack('!I',await exact(4))[0]
    header=json.loads(await exact(size));body=await exact(header['body_bytes'])
    return header,body

def runtime_child(base,config,manifest,sock,control,fault=None):
    """Spawn-safe real core runtime, only private fake-native configuration."""
    async def run():
        try:
            async with mod('remote_runtime').CoreRuntime(base_raw=base,core_raw=config,manifest_raw=manifest) as runtime:
                principal=mod('remote_schemas').Principal(**json.loads(config)['principal'])
                await runtime.attach(sock,principal);control.send({'ready':True,'pid':os.getpid()})
                await asyncio.to_thread(control.recv)
            control.send({'closed':True})
        except BaseException as exc:
            control.send({'error':type(exc).__name__,'message':str(exc)});raise
        finally:sock.close();control.close()
    if fault in ('before_prepare','after_prepare'):
        module=mod('task_workspace');original=module.prepare_empty
        def injected(*args,**kwargs):
            if fault=='after_prepare':original(*args,**kwargs)
            control.send({'fault':fault})
            os._exit(73)  # Intentional fixture process crash; never a real worker process.
        with patch.object(module,'prepare_empty',new=injected):asyncio.run(run())
    else:asyncio.run(run())

# Revision2 fixture helpers compose existing owners; they grant no policy bypass.
async def revision_http(case, family='get'):
    from .test_compatibility import lifespan
    case.client=await case.connect()
    task=await case.task()
    if family=='put':
        ticket=await case.ticket(task['task_id'],b'x');method='PUT'
    else:
        artifact,_=await case.upload(task['task_id'],b'x')
        ticket=case.ok(await case.call('file_download_ticket',{'task_id':task['task_id'],
            'request_id':uid(),'artifact_id':artifact['artifact_id']}))['ticket'];method='GET'
    headers=[(b'authorization',ticket['authorization'].encode())]
    if family=='put':headers += [(b'content-length',b'1'),(b'content-type',b'application/octet-stream')]
    if family=='range':headers.append((b'range',b'bytes=0-0'))
    f=case.fixture
    app=mod('remote_http').create_test_app(core_client=case.client,principal=f.typed('Principal',f.principal),
        config=f.typed('FrontendConfig',{'config_digest':f.digest(),'limits':f.core['limits'],'transfer_origin':f.core['transfer_origin']}))
    await case.stack.enter_async_context(lifespan(app))
    scope={'type':'http','path':'/transfers/'+ticket['ticket_id'],'query_string':b'','method':method,'headers':headers}
    async def receive():return {'type':'http.request','body':b'x' if method=='PUT' else b'','more_body':False}
    return app,scope,receive,task,ticket

async def observe_asgi(app,scope,receive,send,window=.35):
    # Seconds: observer slack detects an unbounded task; it grants no transport grace.
    pending=asyncio.create_task(app(scope,receive,send));error=None
    await asyncio.wait({pending},timeout=window);done=pending.done()
    if not done:pending.cancel()
    try:await pending
    except BaseException as exc:error=exc
    return done,error

async def capacity_fault(case):
    # Exercise actual monitor error classification, not a fabricated authorization flag.
    event=asyncio.Event()
    def failed_measure():
        event.set();raise mod('remote_schemas').RemoteError('capacity_exhausted')
    with patch.object(case.runtime.capacity,'measure',failed_measure):
        async with asyncio.timeout(3):await event.wait()
        # Let the monitor finish the caught failure before the public operation.
        await asyncio.sleep(.03)
