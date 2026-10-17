"""Current native SIGINT behavior with per-model child creation identity evidence."""
import hashlib,json,os,shlex,signal,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,'/Users/velox/.cortados-builds/cortados-router-repo')
from integrations.mcp.codex import native_arguments,native_environment
from integrations.mcp.native_process import process_identity
config={'codex_bin':'/opt/homebrew/bin/codex','codex_home':str(Path.home()/'.codex')}
for model in ['gpt-6-luna','gpt-6.1-sol','gpt-6-astra']:
    directory=ROOT/'cancellation'/model;directory.mkdir(parents=True,exist_ok=True)
    work=directory/'workspace';work.mkdir(exist_ok=True)
    argv=native_arguments(config,{'cwd':str(work),'permissions':{'network':False,'mcp_servers':[]}}, {'chosen':{'model':model}},directory/'final.txt')
    code='import json,os,time,pathlib;pathlib.Path("child.json").write_text(json.dumps({"pid":os.getpid(),"pgid":os.getpgrp(),"sid":os.getsid(0)}));time.sleep(60)'
    prompt='Run exactly one command, then wait for it to finish. This is a cancellation boundary test. Do not shorten or replace the sleep. Command: /opt/homebrew/bin/python3.14 -c '+shlex.quote(code)
    (directory/'handoff.txt').write_text(prompt)
    child_identity=None;native_identity=None;process=None;started=time.time();record={'cli_version':'0.163.0','model':model,'argv':argv,'started_at_unix':started,'status':'failed'}
    try:
        with (directory/'stdout.jsonl').open('wb') as out,(directory/'stderr.txt').open('wb') as err:
            process=subprocess.Popen(argv,cwd=work,env=native_environment(config),stdin=subprocess.PIPE,stdout=out,stderr=err,start_new_session=True)
            native_identity=process_identity(process.pid)
            process.stdin.write(prompt.encode());process.stdin.close()
            # Forty-five seconds allows model startup; shorter flakes, longer wastes live quota on no child.
            deadline=time.monotonic()+45
            while not (work/'child.json').exists() and time.monotonic()<deadline and process.poll() is None:time.sleep(.05)
            assert (work/'child.json').exists(),'child not observed'
            child=json.loads((work/'child.json').read_text()); child_identity=process_identity(child['pid']);assert child_identity
            record.update(native_identity=native_identity,child_identity=child_identity,child_report=child)
            assert process_identity(process.pid)==native_identity
            os.killpg(native_identity['pgid'],signal.SIGINT)
            process.wait(timeout=15)
            deadline=time.monotonic()+5
            while process_identity(child['pid']) is not None and time.monotonic()<deadline:time.sleep(.05)
            after=process_identity(child['pid']);record.update(native_exit_code=process.returncode,child_after=after,signal='SIGINT original native group')
            assert process.returncode!=0,'cancel unexpectedly success'
            assert after is None,'child survived native interrupt'
            record['status']='passed'
    except BaseException as exc:
        record['error']=repr(exc);raise
    finally:
        for observed in (child_identity,native_identity):
            if observed and process_identity(observed['pid'])==observed:
                try:os.killpg(observed['pgid'],signal.SIGKILL)
                except ProcessLookupError:pass
        if process and process.poll() is None:process.wait(timeout=5)
        record.update(elapsed_s=time.time()-started,script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file() and p.name!='meta.json'})
        (directory/'meta.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps({'model':model,'status':record['status'],'elapsed_s':record['elapsed_s']}),flush=True)
