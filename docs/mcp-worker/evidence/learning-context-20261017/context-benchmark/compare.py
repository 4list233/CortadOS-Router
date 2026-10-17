"""Bounded independent S1 experiment. Never imports a server or changes model promotion."""
import argparse,hashlib,json,math,os,re,resource,signal,statistics,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
PYTHON=Path('/Users/velox/cortados/.venv-mlx/bin/python')
# Seconds: enough for twelve short paired calls plus load; lower yields partial evidence,
# higher monopolizes shared GPU longer while the authorized nightly trainer is active.
WALL_SECONDS=180
# Percent: roughly 8GiB on this32GiB host remains available; lower risks memory pressure,
# higher can refuse a feasible brief run. This is checked between paired examples.
FREE_FLOOR_PERCENT=25
# Bytes: bound this additional model process near its expected model+KV+logits peak;
# too low aborts useful8k cases, too high risks compression/pressure in the live trainer.
RSS_LIMIT_BYTES=6*1024**3
# Seconds: resource sampling catches sustained growth without excessive subprocess churn;
# lower spends CPU polling, higher reacts later to benchmark process growth.
RESOURCE_POLL_SECONDS=1
# Probability units: one output rounding unit; lower can reject harmless float-kernel noise,
# higher could hide routing confidence changes. Answers and fail-up answers must be identical.
PROBABILITY_TOLERANCE=0.0001

def write(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp');temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(path)

def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def memory():
    raw=subprocess.check_output(['/usr/bin/memory_pressure','-Q'],text=True)
    percent=int(re.search(r'System-wide memory free percentage:\s*(\d+)%',raw)[1])
    return {'free_percent':percent,'vm_swapusage':subprocess.check_output(['/usr/sbin/sysctl','-n','vm.swapusage'],text=True).strip()}

def training_progress():
    path=Path('/Users/velox/cortados/train/runs/2026-10-17-0130/nightly.log')
    with path.open('rb') as stream:
        stream.seek(max(0,path.stat().st_size-65536))  # Bytes: bounded tail covers recent progress without loading private historical labels.
        text=stream.read().decode('utf-8','replace')
    matches=re.findall(r'Iter (\d+): Train loss ([0-9.]+).*?It/sec ([0-9.]+)',text)
    return {'run':path.parent.name,'last_reported_iteration':int(matches[-1][0]),'last_reported_train_loss':float(matches[-1][1]),'last_reported_iters_per_second':float(matches[-1][2])} if matches else {'run':path.parent.name,'last_reported_iteration':None}

def identities():
    rows=subprocess.check_output(['/bin/ps','-axo','pid=,ppid=,lstart=,args='],text=True).splitlines()
    return [row for row in rows if ('-m mlx_lm lora' in row or '/train/serve.py' in row or '/s1/serve.py' in row or 'overnight.py -- /bin/bash' in row)]

def verify_sources(manifest):
    for name,expected in manifest['source_sha256'].items():
        assert sha(Path(name))==expected,'source changed: '+name
    for name,expected in manifest['adapter_sha256'].items():
        assert sha(ROOT/'adapter-snapshot'/name)==expected,'adapter snapshot changed: '+name

def probabilities(raw,questions,options,temperatures):
    result={}
    for question,logits in zip(questions,raw):
        values=[x/temperatures.get(question,1) for x in logits[:options[question]]]
        peak=max(values);values=[math.exp(x-peak) for x in values];total=sum(values)
        result[question]=[x/total for x in values]
    return result

def fail_up(result,features,inference):
    answers=dict(result['answers']);probs=result['probs']
    if probs['outward'][1]>=inference['outward_at']:answers['outward']=True
    for name in features:
        if max(probs[name])<inference['fail_up_below']:
            top=sorted(range(len(probs[name])),key=lambda i:-probs[name][i])[:2];answers[name]=max(top)
    return answers

class RecordingModel:
    """Both paths collect the same seven digit logits; only projection placement differs."""
    def __init__(self,model,digits,strategy):self.wrapped,self.digits,self.strategy,self.rows,self.feed_lengths=model,digits,strategy,[],[]
    def __getattr__(self,name):return getattr(self.wrapped,name)
    def __call__(self,inputs,cache=None):
        self.feed_lengths.append(inputs.shape[-1])
        if self.strategy=='baseline':
            output=self.wrapped(inputs,cache=cache)
        else:
            hidden=self.wrapped.model(inputs,cache=cache)[:, -1:, :]
            output=(self.wrapped.model.embed_tokens.as_linear(hidden) if self.wrapped.args.tie_word_embeddings else self.wrapped.lm_head(hidden))
        self.rows.append(output[0,-1,self.digits].tolist())
        return output

def child():
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TOKENIZERS_PARALLELISM']='false'
    os.environ['CORTADOS_HOME']=str(ROOT/'isolated-state')
    manifest=json.loads((ROOT/'manifest.json').read_text());verify_sources(manifest)
    sys.path[:0]=['/Users/velox/cortados/train','/Users/velox/cortados']
    from student import Scorer,messages,QUESTIONS,N_OPTIONS,core
    import mlx.core as mx
    cfg=manifest['promoted_record'];load_start=time.perf_counter()
    scorer=Scorer(manifest['base_snapshot'],str(ROOT/'adapter-snapshot'),cfg['temperatures'])
    underlying=scorer.model;mx.synchronize()
    # MLX's limit is a guideline, not a hard memory guarantee. Parent enforces observed RSS.
    mx.set_memory_limit(RSS_LIMIT_BYTES)
    report={'status':'running','model_load_ms':(time.perf_counter()-load_start)*1000,'pairs':[],
            'baseline':'Unmodified live Scorer.score with identical raw-digit instrumentation in both strategies',
            'candidate':'Same autoregressive answers/fresh perrequest KV; project vocabulary only for last hidden position',
            'concurrent_nightly_training':any('-m mlx_lm lora' in row for row in json.loads((ROOT/'run-guard.json').read_text())['pre_live_identities']),'quality_claim':'Synthetic latency/boundary/equivalence evidence; not labeled accuracy or promotion evidence'}
    write(ROOT/'result.json',report)
    rows=json.loads((ROOT/'synthetic-cases.json').read_text())
    rows.sort(key=lambda row:(row['total_prompt_tokens'],row['case']))
    try:
        for index,row in enumerate(rows):
            observation=memory()
            if observation['free_percent']<FREE_FLOOR_PERCENT:
                report.update(status='blocked_pressure',blocked_case=row['id'],memory=observation);break
            request=row['request'];prompt=scorer.tok.apply_chat_template(messages(request,'interactive'),add_generation_prompt=True,tokenize=True)
            first=prompt+scorer.tok.encode(QUESTIONS[0]+'=',add_special_tokens=False)
            assert len(first)==row['total_prompt_tokens'],'first model input token count changed'
            assert hashlib.sha256(json.dumps(first).encode()).hexdigest()==row['prompt_token_sha256'],'prompt token bytes changed'
            assert request.endswith('this request sends, publishes, deletes, and deploys nothing.') or request.endswith('It changes an external system.')
            pair={'id':row['id'],'case':row['case'],'total_prompt_tokens':len(first),'request_chars':len(request),'request_sha256':row['request_sha256'],
                  'terminal_cue_retained':True,'pre_memory':observation,'strategies':{}}
            order=['baseline','last_position_projection'] if index%2==0 else ['last_position_projection','baseline']
            pair['order']=order
            pair['warm_state']='first measured pair after model load; no warmup' if index==0 else 'model already executed earlier synthetic pairs; each call uses fresh KV'
            core.lane_health=lambda: {}  # Existing evaluation isolation: no subscription observations are read for band comparison.
            for strategy in order:
                strategy_memory=memory();pair.setdefault('strategy_pre_memory',{})[strategy]=strategy_memory
                if strategy_memory['free_percent']<FREE_FLOOR_PERCENT:
                    report.update(status='blocked_pressure',blocked_case=row['id'],blocked_strategy=strategy,partial_pair=pair);break
                wrapper=RecordingModel(underlying,mx.array(scorer.digit_ids),strategy);scorer.model=wrapper
                mx.synchronize();mx.reset_peak_memory();before_active=mx.get_active_memory();start=time.perf_counter()
                result=scorer.score(request,'interactive');mx.synchronize();elapsed=(time.perf_counter()-start)*1000
                assert len(wrapper.rows)==12 and len(wrapper.feed_lengths)==12
                assert wrapper.feed_lengths[0]==len(first)
                full=probabilities(wrapper.rows,QUESTIONS,N_OPTIONS,scorer.T)
                pair['strategies'][strategy]={'wall_ms':elapsed,'scorer_ms':result['ms'],'result':result,'full_probabilities':full,
                   'raw_digit_logits':wrapper.rows,'model_feed_lengths':wrapper.feed_lengths,'mlx_active_before_bytes':before_active,
                   'mlx_peak_bytes':mx.get_peak_memory(),'mlx_active_after_bytes':mx.get_active_memory(),'mlx_cache_bytes':mx.get_cache_memory(),
                   'process_highwater_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
                write(ROOT/'partial-pair.json',pair)
                print(json.dumps({'id':row['id'],'strategy':strategy,'wall_ms':elapsed,'mlx_peak_bytes':mx.get_peak_memory()}),flush=True)
            if len(pair['strategies'])!=2:break
            a,b=[pair['strategies'][name] for name in ['baseline','last_position_projection']]
            pair['max_probability_abs_difference']=max(abs(x-y) for question in QUESTIONS for x,y in zip(a['full_probabilities'][question],b['full_probabilities'][question]))
            pair['max_digit_logit_abs_difference']=max(abs(x-y) for aa,bb in zip(a['raw_digit_logits'],b['raw_digit_logits']) for x,y in zip(aa,bb))
            pair['answers_equal']=a['result']['answers']==b['result']['answers']
            pair['fail_up_answers_equal']=fail_up(a['result'],core.FEATURES,cfg['inference'])==fail_up(b['result'],core.FEATURES,cfg['inference'])
            pair['resolved']={}
            for label,observation in [('baseline',a),('last_position_projection',b)]:
                resolved=core.resolve({'answers':fail_up(observation['result'],core.FEATURES,cfg['inference']),'degraded':False,'engine':'isolated_benchmark','engine_ms':observation['scorer_ms']})
                pair['resolved'][label]={key:resolved[key] for key in ['band','D']}
            pair['bands_equal']=pair['resolved']['baseline']==pair['resolved']['last_position_projection']
            pair['equivalent']=pair['bands_equal'] and pair['answers_equal'] and pair['fail_up_answers_equal'] and pair['max_probability_abs_difference']<=PROBABILITY_TOLERANCE
            pair['speedup_ratio']=a['wall_ms']/b['wall_ms'];report['pairs'].append(pair);write(ROOT/'result.json',report)
            if not pair['equivalent']:report['status']='failed_equivalence';break
        else:report['status']='completed'
        verify_sources(manifest)
    except BaseException as exc:report.update(status='failed',error=repr(exc));raise
    finally:
        scorer.model=underlying;mx.synchronize();mx.clear_cache();write(ROOT/'result.json',report)
    return 0 if report['status']=='completed' and len(report['pairs'])==6 and all(pair['equivalent'] for pair in report['pairs']) else 2

def stop_owned(process,pgid):
    if process.poll() is None and os.getpgid(process.pid)==pgid:
        os.killpg(pgid,signal.SIGTERM)
        try:process.wait(timeout=3)  # Seconds: brief owned-process cleanup; longer extends contention, shorter interrupts orderly exit.
        except subprocess.TimeoutExpired:
            if process.poll() is None and os.getpgid(process.pid)==pgid:os.killpg(pgid,signal.SIGKILL)
            process.wait()

def parent():
    assert not (ROOT/'run-guard.json').exists(),'Do not overwrite an earlier benchmark run'
    manifest=json.loads((ROOT/'manifest.json').read_text());verify_sources(manifest)
    guard={'started_at_unix':time.time(),'pre_memory':memory(),'pre_live_identities':identities(),'pre_training_progress':training_progress(),'driver_sha256':sha(Path(__file__)),
           'wall_limit_seconds':WALL_SECONDS,'rss_limit_bytes':RSS_LIMIT_BYTES,'free_floor_percent':FREE_FLOOR_PERCENT,'status':'running','resource_samples':[]}
    write(ROOT/'run-guard.json',guard)
    if guard['pre_memory']['free_percent']<FREE_FLOOR_PERCENT:
        guard.update(status='blocked_pressure',post_live_identities=identities());write(ROOT/'run-guard.json',guard);return 2
    env=dict(os.environ);env.update(HF_HUB_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1',TOKENIZERS_PARALLELISM='false')
    started=time.monotonic()
    with (ROOT/'stdout.log').open('w') as out,(ROOT/'stderr.log').open('w') as err:
        process=subprocess.Popen(['/usr/bin/nice','-n','10',str(PYTHON),'-B',str(Path(__file__).resolve()),'--child'],cwd=ROOT,env=env,stdout=out,stderr=err,start_new_session=True)
        guard['owned_pid']=process.pid;guard['owned_pgid']=os.getpgid(process.pid);assert guard['owned_pgid']==process.pid
        def interrupted(signum,frame):raise KeyboardInterrupt('benchmark parent signal '+str(signum))
        signal.signal(signal.SIGTERM,interrupted)
        try:
            while process.poll() is None:
                current=subprocess.run(['/bin/ps','-p',str(process.pid),'-o','rss='],capture_output=True,text=True).stdout.strip()
                rss=int(current)*1024 if current else 0;elapsed=time.monotonic()-started
                guard['resource_samples'].append({'elapsed_s':elapsed,'rss_bytes':rss})
                reason='blocked_deadline' if elapsed>WALL_SECONDS else 'blocked_rss' if rss>RSS_LIMIT_BYTES else None
                if reason:
                    guard['status']=reason;stop_owned(process,guard['owned_pgid']);break
                time.sleep(RESOURCE_POLL_SECONDS)
        except BaseException as exc:
            guard.update(status='interrupted',error=repr(exc));stop_owned(process,guard['owned_pgid'])
        finally:
            stop_owned(process,guard['owned_pgid'])
        guard.update(native_exit_code=process.returncode,elapsed_s=time.monotonic()-started,post_memory=memory(),post_live_identities=identities(),post_training_progress=training_progress())
        trainers=[row for row in guard['pre_live_identities'] if '-m mlx_lm lora' in row]
        guard['same_trainers_observed_after']=bool(trainers) and all(row in guard['post_live_identities'] for row in trainers)
        before=guard['pre_training_progress']['last_reported_iteration'];after=guard['post_training_progress']['last_reported_iteration']
        guard['new_training_progress_observed']=before is not None and after is not None and after>before
        if guard['status']=='running':
            child_status=json.loads((ROOT/'result.json').read_text()).get('status') if (ROOT/'result.json').exists() else None
            guard['status']='completed' if process.returncode==0 and child_status=='completed' else child_status or 'failed'
        if trainers and not guard['same_trainers_observed_after']:
            guard['benchmark_result_status']=guard['status'];guard['status']='needs_review_trainer_identity_changed'
        write(ROOT/'run-guard.json',guard)
    print(json.dumps({'status':guard['status'],'elapsed_s':guard['elapsed_s'],'result':str(ROOT/'result.json')}))
    return 0 if guard['status']=='completed' else 2

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--child',action='store_true');args=parser.parse_args()
    if args.child:raise SystemExit(child())
    else:raise SystemExit(parent())
