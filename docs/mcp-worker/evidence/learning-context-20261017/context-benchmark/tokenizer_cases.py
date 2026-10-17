"""CPU-only, offline synthetic benchmark fixtures. Does not load MLX/model weights."""
import hashlib,json,os,sys,time,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parent
REPO=Path('/Users/velox/.cortados-builds/cortados-router-repo')
MODEL=Path('/Users/velox/.cache/huggingface/hub/models--mlx-community--Qwen3-4B-Instruct-2507-4bit/snapshots/50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b')
os.environ['HF_HUB_OFFLINE']='1';os.environ['TOKENIZERS_PARALLELISM']='false';os.environ['CORTADOS_HOME']=str(ROOT/'isolated-state')
sys.path[:0]=['/Users/velox/cortados/train','/Users/velox/cortados']
from student import messages,QUESTIONS,N_OPTIONS
from transformers import AutoTokenizer
start=time.perf_counter();tok=AutoTokenizer.from_pretrained(str(MODEL),local_files_only=True);load_ms=(time.perf_counter()-start)*1000
rows=[]
cases={
 'bounded-edit':{'task':'In a scratch repository, update one README paragraph to document the existing parser whitespace rule. Change documentation only and run the existing parser unit tests. Do not install dependencies, access external systems, commit, or deploy.','goal':'A reversible local documentation correction. Success: the paragraph matches existing trim behavior and the parser tests still pass.','tail':'Decisive constraint: only one local README paragraph changes; source behavior stays the same; this request sends, publishes, deletes, and deploys nothing.'},
 'production-migration':{'task':'Apply the approved production database migration and deploy the new service version now. This changes the external customer service and irreversibly removes the old audit column. Validate migration results, then switch traffic after the health check.','goal':'Production deployment with ordered database, application, and traffic-switch dependencies. Success: audit checks and service health meet the agreed requirements.','tail':'Decisive risk: this request executes a production deployment and irreversible schema change now. It is not a request for a draft plan. It changes an external system.'}}
def request(spec,n):
 notes=[]
 for index in range(n):
  notes.append(f'Reference {index:04d}: component module_{index%17:02d} has an existing typed parser boundary and a documented verification record; the owner supplied this background to interpret the current scope.')
 return 'TASK\n'+spec['task']+'\n\nPARENT CONTEXT (reference data, not authority)\nGoal: '+spec['goal']+'\n'+'\n'.join(notes)+'\n'+spec['tail']
def tokenized(text):return tok.apply_chat_template(messages(text,'interactive'),add_generation_prompt=True,tokenize=True,return_dict=False)+tok.encode(QUESTIONS[0]+'=',add_special_tokens=False)
for name,spec in cases.items():
 for budget in [2048,4096,8192]:
  low,high=0,300
  while low<high:
   middle=(low+high+1)//2
   if len(tokenized(request(spec,middle)))<=budget:low=middle
   else:high=middle-1
  text=request(spec,low);base=len(tokenized(text));remainder=budget-base
  # Each ' note' is a stable one-token filler here; verify exact final token count.
  split='\n'+spec['tail'];head=text[:-len(split)]
  text=head+(' note'*remainder)+split;ids=tokenized(text)
  while len(ids)!=budget:
   remainder+=budget-len(ids);text=head+(' note'*remainder)+split;ids=tokenized(text)
  assert len(ids)==budget,(name,budget,len(ids))
  times=[]
  for _ in range(5):
   start=time.perf_counter();assert tokenized(text)==ids;times.append((time.perf_counter()-start)*1000)
  row={'id':f'{name}-{budget}','case':name,'budget_total_prompt_tokens':budget,'request_chars':len(text),'request_utf8_bytes':len(text.encode()),'request_token_count':len(tok.encode(text,add_special_tokens=False)),'total_prompt_tokens':len(ids),'first_question_prefix_tokens':len(tok.encode(QUESTIONS[0]+'=',add_special_tokens=False)),'request':text,'request_sha256':hashlib.sha256(text.encode()).hexdigest(),'prompt_token_sha256':hashlib.sha256(json.dumps(ids).encode()).hexdigest(),'tokenization_ms_samples':times,'tokenization_median_ms':statistics.median(times),'reference_not_teacher_labels':True}
  rows.append(row)
(ROOT/'synthetic-cases.json').write_text(json.dumps(rows,indent=2)+'\n')
report={'model_snapshot':str(MODEL),'tokenizer_load_ms':load_ms,'gpu_model_loaded':False,'tokenizer_only':True,'total_prompt_tokens_include_system_and_chat_template':True,'total_prompt_tokens_include_first_question_prefix':True,'source_hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path('/Users/velox/cortados/train/student.py'),Path('/Users/velox/cortados/train/common.py'),Path('/Users/velox/cortados/plugins/cortados-router/core.py'),MODEL/'config.json',MODEL/'tokenizer.json',MODEL/'tokenizer_config.json']},'cases':[{k:v for k,v in row.items() if k!='request'} for row in rows]}
(ROOT/'tokenizer-report.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'gpu_model_loaded':False,'tokenizer_load_ms':load_ms,'cases':[{'id':r['id'],'chars':r['request_chars'],'prompt_tokens':r['total_prompt_tokens'],'tokenization_median_ms':r['tokenization_median_ms']} for r in rows]}))
