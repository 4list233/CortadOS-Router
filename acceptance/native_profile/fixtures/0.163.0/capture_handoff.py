"""Corrected native handoff observer: expected sentinels come from pinned request, not a guessed marker."""
import hashlib,json,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
REPO=Path('/Users/velox/.cortados-builds/cortados-router-repo');sys.path.insert(0,str(REPO))
from integrations.mcp.codex import native_arguments,native_environment,EventReader
config={'codex_bin':'/opt/homebrew/bin/codex','codex_home':str(Path.home()/'.codex')}
prompt=(REPO/'acceptance/mcp_worker/fixtures/native-boundaries/handoff-boundary/handoff.txt').read_bytes()
assert len(prompt)==65536
expected='FIRST_SENTINEL_9df3 LAST_SENTINEL_a712'
for model in ['gpt-6-luna','gpt-6.1-sol','gpt-6-astra']:
    directory=ROOT/'handoff-boundary'/model;directory.mkdir(parents=True,exist_ok=True)
    work=directory/'workspace';work.mkdir(exist_ok=True)
    if model=='gpt-6-luna':
        # Original native capture was correct; retain faulty observer's report and validate exact same bytes.
        reader=EventReader();reader.feed((directory/'stdout.jsonl').read_bytes());final,usage=reader.completion(0,directory/'final.txt')
        assert final==expected
        (directory/'observer-correction.json').write_text(json.dumps({'native_replayed':False,'original_native_bytes_unchanged':True,'error':'First observer expected an unrelated marker rather than the pinned handoff sentinels.','actual_expected':expected,'status':'passed','stdout_sha256':hashlib.sha256((directory/'stdout.jsonl').read_bytes()).hexdigest(),'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},indent=2)+'\n')
        print(json.dumps({'model':model,'status':'passed','recaptured':False}),flush=True);continue
    argv=native_arguments(config,{'cwd':str(work),'permissions':{'network':False,'mcp_servers':[]}}, {'chosen':{'model':model}},directory/'final.txt')
    (directory/'handoff.txt').write_bytes(prompt);started=time.time()
    with (directory/'stdout.jsonl').open('wb') as out,(directory/'stderr.txt').open('wb') as err:
        result=subprocess.run(argv,input=prompt,cwd=work,env=native_environment(config),stdout=out,stderr=err,timeout=120,start_new_session=True)
    reader=EventReader();reader.feed((directory/'stdout.jsonl').read_bytes());final,usage=reader.completion(result.returncode,directory/'final.txt')
    assert final==expected
    meta={'cli_version':'0.163.0','model':model,'argv':argv,'status':'passed','started_at_unix':started,'elapsed_s':time.time()-started,'native_exit_code':result.returncode,'handoff_utf8_bytes':len(prompt),'expected':expected,'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'hashes':{name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in ('stdout.jsonl','stderr.txt','final.txt','handoff.txt')}}
    (directory/'meta.json').write_text(json.dumps(meta,indent=2)+'\n');print(json.dumps({'model':model,'status':'passed','elapsed_s':meta['elapsed_s']}),flush=True)
