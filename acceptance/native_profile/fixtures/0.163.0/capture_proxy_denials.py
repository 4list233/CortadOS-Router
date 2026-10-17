"""Explicit grant boundary checks using MCP1.30.0, no model turn or external connector."""
import asyncio,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent;REPO=Path('/Users/velox/.cortados-builds/cortados-router-repo');sys.path.insert(0,str(REPO))
from integrations.mcp.grant_proxy import GrantPayloadServer
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
async def main():
    directory=ROOT/'proxy-denials';directory.mkdir(exist_ok=True);events=directory/'upstream.jsonl'
    config={'limits':{'startup_timeout_s':30},'connector_registry':{'fixture':{'transport':'stdio','command':sys.executable,'args':[str(REPO/'acceptance/mcp_worker/fake_mcp_server.py'),str(events)],'env_vars':[],'tools':['visible_marker','hidden_marker']}}}
    proxy=GrantPayloadServer(config,{'mcp_servers':[{'name':'fixture','tools':['visible_marker']}]});record={}
    try:
        params=StdioServerParameters(command=sys.executable,args=['-B',str(REPO/'integrations/mcp/grant_proxy.py'),'--socket',str(proxy.path),'--server','fixture'])
        with (directory/'stderr.log').open('w') as err:
            async with stdio_client(params,errlog=err) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize();record['tools']=[t.name for t in (await client.list_tools()).tools];assert record['tools']==['visible_marker']
                    for name,operation in [('hidden_tool',lambda:client.call_tool('hidden_marker',{})),('resources',client.list_resources),('read_resource',lambda:client.read_resource('fixture://secret')),('prompts',client.list_prompts),('get_prompt',lambda:client.get_prompt('hidden_prompt'))]:
                        try:
                            value=await operation();record[name]={'returned':value.model_dump(mode='json')}
                            assert ((name=='resources' and value.resources==[]) or (name=='prompts' and value.prompts==[]) or getattr(value,'isError',False)),name+' exceeded grant'
                        except McpError as exc:record[name]={'error_code':exc.error.code,'message':exc.error.message}
                    result=await client.call_tool('visible_marker',{});record['visible_result']=result.model_dump(mode='json');assert not result.isError
        rows=[json.loads(x) for x in events.read_text().splitlines()];assert [r['kind'] for r in rows if r['kind']!='started']==['visible_tool'],rows
        record.update(status='passed',upstream_events=rows,script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest());(directory/'result.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps({'proxy_denials':'passed'}))
    finally:proxy.close()
asyncio.run(main())
