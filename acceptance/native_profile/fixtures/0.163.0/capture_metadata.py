"""Independent current CLI capture. Raw config stays in memory; structure-preserving redaction."""
import hashlib, json, os, re, selectors, subprocess, time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
OUT=ROOT/'metadata'; OUT.mkdir(exist_ok=True)
ENV_NAMES=('HOME','USER','LOGNAME','PATH','LANG','LC_ALL','LC_CTYPE','TMPDIR','SSL_CERT_FILE','SSL_CERT_DIR')
env={key:os.environ[key] for key in ENV_NAMES if key in os.environ}; env['CODEX_HOME']=str(Path.home()/'.codex')
argv=['/opt/homebrew/bin/codex','app-server','--listen','stdio://','--disable','hooks','--disable','plugins','--disable','apps','--disable','multi_agent']
process=subprocess.Popen(argv,cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
selector=selectors.DefaultSelector(); selector.register(process.stdout,selectors.EVENT_READ);selector.register(process.stderr,selectors.EVENT_READ)
pending=b''; count=0; stderr_size=0; frames=[]; provenance=[]
# Thirty seconds per metadata read covers slow account network access; shorter flakes, longer retains failed process.
TIMEOUT=30
JSON_STRING=re.compile(rb'"(?:[^"\\]|\\.)*"')
SOURCE_TYPES={'packagedDefaults','mdm','system','enterpriseManaged','user','project','sessionFlags','legacyManagedConfigTomlFromFile','legacyManagedConfigTomlFromMdm'}
def sanitized(line, method, body):
    # Parse token spans while keeping every non-redacted byte from the native frame.
    text=line.decode(); position=0; replacements=[]; aliases={}
    dynamic_fields={'mcp_servers','projects','model_providers','profiles','plugins','apps','headers','http_headers','env_http_headers','env','marketplaces','origins'}
    def space():
        nonlocal position
        while position<len(text) and text[position].isspace():position+=1
    def token():
        nonlocal position
        start=position; value,end=json.JSONDecoder().raw_decode(text,position);position=end
        return start,end,value
    def alias(value):
        if value not in aliases:aliases[value]='redacted_key_'+str(len(aliases)+1)
        return aliases[value]
    def value(path=(),dynamic=False):
        nonlocal position
        space()
        if text[position]=='{':
            position+=1;space()
            if text[position]=='}':position+=1;return
            while True:
                space();start,end,key=token()
                if dynamic or '/' in key or '\\' in key:replacements.append((start,end,json.dumps(alias(key))))
                space();assert text[position]==':';position+=1
                value(path+(key,), key in dynamic_fields or (key=='tools' and 'mcp_servers' in path))
                space()
                if text[position]=='}':position+=1;return
                assert text[position]==',';position+=1
        elif text[position]=='[':
            position+=1;space()
            if text[position]==']':position+=1;return
            while True:
                value(path+('[]',));space()
                if text[position]==']':position+=1;return
                assert text[position]==',';position+=1
        else:
            start,end,item=token()
            if isinstance(item,str):
                private=(method in {'config/read','configRequirements/read'} and not(path[-2:]==('name','type') and item in SOURCE_TYPES))
                private=private or (path and path[-1] in {'email','accountId','chatgptAccountId','codexHome','balance'}) or item.startswith('/Users/')
                if private:replacements.append((start,end,json.dumps('0.0000000000' if path[-1:]==('balance',) else 'redacted-string')))
    value()
    for start,end,replacement in reversed(replacements):text=text[:start]+replacement+text[end:]
    return text.encode()

def call(method,params):
    global pending,count,stderr_size
    count+=1
    process.stdin.write((json.dumps({'id':count,'method':method,'params':params})+'\n').encode());process.stdin.flush()
    deadline=time.monotonic()+TIMEOUT
    while time.monotonic()<deadline:
        while b'\n' in pending:
            line,pending=pending.split(b'\n',1); body=json.loads(line)
            if body.get('id')==count:
                raw=line+b'\n'; safe=sanitized(raw,method,body); frames.append(safe)
                name=method.replace('/','-')+'.jsonl'; (OUT/name).write_bytes(safe)
                provenance.append({'method':method,'raw_sha256':hashlib.sha256(raw).hexdigest(),'redacted_sha256':hashlib.sha256(safe).hexdigest(),'file':name})
                if 'error' in body: raise RuntimeError({'method':method,'error_code':body['error']['code']})
                return body['result']
        for key,_ in selector.select(max(0,deadline-time.monotonic())):
            chunk=os.read(key.fd,65536)
            if not chunk:
                selector.unregister(key.fileobj)
                if key.fileobj is process.stdout: raise RuntimeError('Metadata EOF')
            elif key.fileobj is process.stderr: stderr_size+=len(chunk)
            else: pending+=chunk
    raise TimeoutError(method)
try:
    initialize=call('initialize',{'clientInfo':{'name':'cortados-native-profile-capture','version':'0.1.0'},'capabilities':{'experimentalApi':True}})
    process.stdin.write(b'{"method":"initialized","params":{}}\n');process.stdin.flush()
    account=call('account/read',{'refreshToken':False})
    quota=call('account/rateLimits/read',{})
    models=[];cursor=None
    while True:
        page=call('model/list',{'includeHidden':False,'cursor':cursor});models.extend(row['id'] for row in page['data']);cursor=page['nextCursor']
        if cursor is None:break
    policy=call('config/read',{'includeLayers':True})
    requirements=call('configRequirements/read',{})
    import sys;sys.path.insert(0,'/Users/velox/.cortados-builds/cortados-router-repo')
    from integrations.mcp.native_metadata import _policy
    from cortados_router.worker_admission import validate_snapshot
    _policy(policy,requirements)
    snap={'account':account,'models':models,'rate_limits':quota,'sampled_at_ms':time.time_ns()//1000000,'sampled_monotonic':time.monotonic(),'version':'0.163.0'}
    validate_snapshot(snap)
    summary={'cli_version':subprocess.check_output(['/opt/homebrew/bin/codex','--version'],text=True).strip(),'platform':initialize.get('platformOs'),'account_type':account.get('account',{}).get('type'),'models':models,'policy_accepted':True,'quota_accepted':True,'config_layer_types':[x.get('name',{}).get('type') for x in policy['layers']],'requirements_null':requirements.get('requirements') is None,'worker_turn_started':False}
    (OUT/'summary.json').write_text(json.dumps(summary,indent=2)+'\n'); print(json.dumps(summary))
finally:
    process.stdin.close()
    try: process.wait(timeout=3)
    except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=3)
    (OUT/'wire.jsonl').write_bytes(b''.join(frames))
    (OUT/'provenance.json').write_text(json.dumps({'argv':argv,'environment_names':sorted(env),'frames':provenance,'redaction':'Only JSON string tokens replaced; other bytes, ordering, whitespace and numbers retained. No raw credentials or configuration persisted.','stderr_bytes':stderr_size,'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},indent=2)+'\n')
