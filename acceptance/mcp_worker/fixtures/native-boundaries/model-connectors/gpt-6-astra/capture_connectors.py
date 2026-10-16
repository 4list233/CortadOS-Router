import hashlib, json, os, subprocess, time
from pathlib import Path
root = Path('/private/tmp/cortados-mcp-capture-20261015/model-connectors/gpt-6-astra')
work = root/'connector-workspace'
(work/'.codex').mkdir(parents=True, exist_ok=True)
python = '/private/tmp/cortados-mcp-sdk-env/bin/python'
probe = str(root/'connector_probe.py')
(work/'.codex/config.toml').write_text('[mcp_servers.unexpected]\ncommand = '+json.dumps(python)+'\nargs = '+json.dumps([probe, 'unexpected', str(root/'unexpected-started')])+'\n')
env = dict(os.environ)
for key in ('OPENAI_API_KEY','OPENAI_BASE_URL','AZURE_OPENAI_API_KEY','ANTHROPIC_API_KEY'):
    env.pop(key,None)
cmd = ['/opt/homebrew/bin/codex','exec','--ignore-user-config','--ignore-rules','--ephemeral','--skip-git-repo-check','-C',str(work),'-s','workspace-write','-m','gpt-6-astra','-c','approval_policy="never"','-c','model_reasoning_effort="low"']
for feature in ('hooks','plugins','apps','multi_agent'):
    cmd.extend(['--disable',feature])
cmd.extend(['--strict-config','-c','projects={'+json.dumps(str(work))+'={trust_level="untrusted"}}'])
server_config='mcp_servers={"approved_probe"={command='+json.dumps(python)+',args='+json.dumps([probe,'approved_probe',str(root/'approved-started')])+',required=true,enabled_tools=["worker_probe"],tools={"worker_probe"={approval_mode="approve"}}}}'
cmd.extend(['-c',server_config])
cmd.extend(['--json','--color','never','-o',str(root/'connector-final.txt'),'-'])
prompt='Protocol test only. Call the approved_probe worker_probe MCP tool exactly once. Do not call any other tools or inspect files. Reply exactly with the marker returned by that tool.'
start=time.time()
try:
    with (root/'connector-stdout.jsonl').open('wb') as out,(root/'connector-stderr.txt').open('wb') as err:
        result=subprocess.run(cmd,input=prompt.encode(),stdout=out,stderr=err,env=env,timeout=180)
    meta={'exit_code':result.returncode,'elapsed_s':time.time()-start,'argv':cmd,'started_at_unix':start,'prompt':prompt,'version':'codex-cli 0.162.1','source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'probe_sha256':hashlib.sha256(Path(probe).read_bytes()).hexdigest(),'approved_called':(root/'approved_probe-called').exists(),'ungranted_called':(root/'ungranted-called').exists(),'approved_started':(root/'approved-started').exists(),'unexpected_started':(root/'unexpected-started').exists()}
except subprocess.TimeoutExpired:
    meta={'error':'capture_timeout','elapsed_s':time.time()-start}
(root/'connector-meta.json').write_text(json.dumps(meta,indent=2)+'\n')
print(json.dumps({k:v for k,v in meta.items() if k not in ('argv','prompt')},indent=2))
