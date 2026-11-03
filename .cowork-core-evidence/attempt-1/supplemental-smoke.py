# Diagnostic only: replaces host_boot_id with an invented value; not protected acceptance.
import asyncio,tempfile,json,os,sys
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from acceptance.cowork_mcp.support import Fixture,canon,mod,uid
async def run():
 with tempfile.TemporaryDirectory() as path:
  f=Fixture(path)
  with patch('integrations.mcp.native_process.host_boot_id',return_value='synthetic-diagnostic-boot'),patch.dict(os.environ,{'CORTADOS_HOME':str(f.path/'router'),'CORTADOS_LIVE_WRITE':'0'}):
   async with mod('remote_runtime').CoreRuntime(base_raw=canon(f.base),core_raw=canon(f.core),manifest_raw=canon(f.manifest)) as rt:
    print('ready',flush=True)
    import socket
    left,right=socket.socketpair();left.setblocking(False);right.setblocking(False)
    p=f.typed('Principal',f.principal)
    await rt.attach(left,p)
    client=mod('remote_ipc').CoreClient(right,principal=p,config_digest=f.digest(),limits=f.typed('CoreLimits',f.core['limits']))
    print('bind',await client.bind(),flush=True)
    created=await client.call('task_create',dict(request_id=uid(),task_group_id=uid()))
    print('create',created,flush=True)
    if created['ok']:
     task=created['task']['task_id']
     print('get',await client.call('task_get',dict(task_id=task)),flush=True)
     import hashlib
     data=bytes([0,255])+b'opaque'
     ticket=await client.call('file_upload_ticket',dict(task_id=task,request_id=uid(),artifact_id=None,filename='a.bin',bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),media_type='application/octet-stream'))
     print('ticket',ticket,flush=True)
     if ticket['ok']:
      t=ticket['ticket'];opened=await client.stream('open_put',dict(ticket_id=t['ticket_id'],capability=t['authorization'][7:],content_length=len(data)))
      print('open',opened,flush=True)
      if opened['result']['ok']:
       lease=opened['result']['lease_id']
       print('put',await client.stream('put_chunk',dict(lease_id=lease,offset=0),data),flush=True)
       print('seal',await client.stream('finish_put',dict(lease_id=lease)),flush=True)
    if created['ok'] and ticket['ok']:
     aid=ticket['ticket']['artifact_id']
     get=await client.call('file_download_ticket',dict(task_id=task,request_id=uid(),artifact_id=aid))
     print('download-ticket',get,flush=True)
     t=get['ticket'];opened=await client.stream('open_get',dict(ticket_id=t['ticket_id'],capability=t['authorization'][7:]))
     lease=opened['result']['lease_id']
     result=await client.stream('read_chunk',dict(lease_id=lease,offset=0,max_bytes=4096))
     assert result['body']==data, result
     print('binary-verified',hashlib.sha256(result['body']).hexdigest(),flush=True)
     print('close',await client.stream('close',dict(lease_id=lease,reason='complete')),flush=True)
     app=mod('remote_http').create_test_app(core_client=client,principal=p,config=f.typed('FrontendConfig',dict(config_digest=f.digest(),limits=f.core['limits'],transfer_origin=f.core['transfer_origin'])))
     from acceptance.cowork_mcp.test_compatibility import lifespan
     import httpx
     async with lifespan(app):
      async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://cowork.invalid') as http:
       for method,path in [('GET','/mcp'),('GET','/health')]:
        result=await http.request(method,path)
        print('http',path,result.status_code,flush=True)
       result=await http.post('/mcp',headers={'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'diagnostic','version':'1'}}})
       print('mcp',result.status_code,result.text,flush=True)
       get=await client.call('file_download_ticket',dict(task_id=task,request_id=uid(),artifact_id=aid))
       t=get['ticket']
       result=await http.get(t['url'],headers={'Authorization':t['authorization']})
       assert result.status_code==200 and result.content==data,(result.status_code,result.content)
       print('http-bytes-verified',len(result.content),flush=True)
    await client.close()
asyncio.run(run())
