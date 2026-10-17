"""CPU-only exact installedMLX ChatDataset training tokenization on syntheticS1 examples."""
import hashlib,importlib.util,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
REPO=Path('/Users/velox/.cortados-builds/cortados-router-repo')
SITE=Path('/Users/velox/cortados/.venv-mlx/lib/python3.12/site-packages')
MODEL=Path('/Users/velox/.cache/huggingface/hub/models--mlx-community--Qwen3-4B-Instruct-2507-4bit/snapshots/50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b')
os.environ['HF_HUB_OFFLINE']='1';os.environ['TOKENIZERS_PARALLELISM']='false';os.environ['CORTADOS_HOME']=str(ROOT/'isolated-state')
sys.path[:0]=[str(REPO/'s1'),str(REPO)]
from student import messages,QUESTIONS
from transformers import AutoTokenizer

def source_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
# Loading these exact installed source modules bypath avoids importing mlx_lm package/model code.
wrappers=source_module('captured_tokenizer_utils',SITE/'mlx_lm/tokenizer_utils.py')
datasets=source_module('captured_training_datasets',SITE/'mlx_lm/tuner/datasets.py')
raw=AutoTokenizer.from_pretrained(str(MODEL),local_files_only=True)
tokenizer=wrappers.TokenizerWrapper(raw,eos_token_ids=[151645])
answers={'steps':1,'ambiguity':0,'blast_radius':1,'context_size':0,'novelty':0,'parallelizability':0,
         'outward':False,'work_kind':'code','urgency':'today','needs_clarification':False,'parts':0,'diversity':False}
base='Update one README paragraph in a local scratch repository and run the existing parser test. Reference information:'
records=[]
for limit in [1536,1537]:
    count=0
    # Ten token-count adjustments bound thisCPU fixture setup; fewer could stop near aBPEboundary, more could hide nonconvergence.
    for attempt in range(10):
        request=base+' reference'*count
        row={'messages':messages(request,'interactive',answers)}
        tokens,offset=datasets.ChatDataset([row],tokenizer,mask_prompt=True).process(row)
        if len(tokens)==limit:break
        count+=limit-len(tokens);assert count>=0
    assert len(tokens)==limit
    rendered=tokenizer.apply_chat_template(row['messages'],tools=None,return_dict=False,tokenize=False)
    assert tokenizer.encode(rendered,add_special_tokens=False)==tokens
    record={'full_training_token_count':len(tokens),'mask_prompt_offset':offset,'supervised_token_count':len(tokens)-offset,
            'messages':row['messages'],'answers':answers,'input_ids':tokens,'rendered_template':rendered,
            'input_ids_sha256':hashlib.sha256(json.dumps(tokens,separators=(',',':')).encode()).hexdigest(),
            'request_sha256':hashlib.sha256(request.encode()).hexdigest(),'fits_1536_without_truncation':len(tokens)<=1536,
            'first_token':tokens[0],'last_tokens':tokens[-10:],'native_eos_id_positions':[i for i,value in enumerate(tokens) if value==tokenizer.eos_token_id]}
    (ROOT/f'training-{limit}.json').write_text(json.dumps(record,indent=2)+'\n');records.append({k:v for k,v in record.items() if k not in ['messages','input_ids','rendered_template']})
paths=[Path(__file__),REPO/'s1/student.py',REPO/'s1/common.py',REPO/'cortados_router/core.py',SITE/'mlx_lm/tokenizer_utils.py',SITE/'mlx_lm/tuner/datasets.py',SITE/'mlx_lm/tuner/trainer.py',MODEL/'tokenizer.json',MODEL/'tokenizer_config.json',MODEL/'config.json']
provenance={'base_snapshot':str(MODEL),'gpu_model_loaded':False,'mlx_core_imported':'mlx.core' in sys.modules,
            'production_writes':False,'source_hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            'training_owner':'Installed mlx_lm.tuner.datasets.ChatDataset.process with actual TokenizerWrapper',
            'chat_dataset_options':{'tools':None,'return_dict':False,'mask_prompt':True},
            'wrapper_options':{'tokenize':True,'enable_thinking':tokenizer.has_thinking,'return_dict':False},
            'mask_offset_options':{'messages':'all except final assistant','add_generation_prompt':True,'tools':None,'return_dict':False},
            'bos_eos':'ChatDataset adds no extra BOS or EOS. It uses template tokens verbatim; mask offset uses prompt template with generation prefix. TextDataset EOS-appending behavior is not used.',
            'model_token_ids':{'bos':raw.bos_token_id,'eos':raw.eos_token_id},'synthetic_not_quality_labels':True,'captures':records}
assert not provenance['mlx_core_imported'],'CPU capture unexpectedly importedMLX'
(ROOT/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
print(json.dumps({'captures':[{'tokens':r['full_training_token_count'],'mask_offset':r['mask_prompt_offset'],'supervised':r['supervised_token_count'],'fits':r['fits_1536_without_truncation']} for r in records],'mlx_core_imported':False}))
