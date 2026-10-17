import sys,importlib.util,tempfile,json,types,io,contextlib,hashlib
from pathlib import Path
from unittest.mock import patch
R=Path('/Users/velox/.cortados-builds/cortados-router-repo');sys.path.insert(0,str(R))
from acceptance.training_teacher.support import COMMON,ANSWER,rows
p=R/'acceptance/training_teacher/evidence/baseline-label.py'
with patch.dict(sys.modules,{'common':COMMON,'usage':types.SimpleNamespace(record=lambda row:None)}):
 spec=importlib.util.spec_from_file_location('baseline_branch_exam',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 result={'baseline_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'rows':{},'exceptions':{},'provider_calls':0}
 with tempfile.TemporaryDirectory() as d:
  m.LABELS=Path(d)/'labels.jsonl';batch=rows(2)
  cases={'invalid_neighbors':[dict(i=0,**ANSWER),None,'bad',{'i':1},dict(i=-1,**ANSWER),dict(i=2,**ANSWER),dict(i=1,**ANSWER)],'duplicate':[dict(i=0,**ANSWER),dict(i=0,**ANSWER),dict(i=1,**ANSWER)],'bool':[dict(i=False,**ANSWER),dict(i=True,**ANSWER)]}
  for name,answers in cases.items():
   m.LABELS.write_text('');m._stop.clear();m.ONLY=[]
   with patch.object(m,'call_teacher',return_value=(answers,{})):
    result['rows'][name]={'count':m.work(('claude-sonnet-5',0,batch))}
  m.LABELS.write_text('');m.ONLY=['outward']
  with patch.object(m,'call_teacher',return_value=([dict(i=False,outward=True),None,dict(i=9,outward=True),dict(i=True,outward=False)],{})):
   result['rows']['override_bool']={'count':m.work(('claude-sonnet-5',0,batch))}
  m.ONLY=[]
  for message in ['malformed JSON','rate limit','429','quota exhausted','overloaded']:
   m._stop.clear();m.LABELS.write_text('')
   with patch.object(m,'call_teacher',side_effect=[ValueError(message),([dict(i=0,**ANSWER)],{})]) as native,contextlib.redirect_stdout(io.StringIO()):
    counts=[m.work(('claude-sonnet-5',i,batch)) for i in range(2)]
   result['exceptions'][message]={'counts':counts,'calls':native.call_count,'stopped':m._stop.is_set()}
print(json.dumps(result,indent=2))
