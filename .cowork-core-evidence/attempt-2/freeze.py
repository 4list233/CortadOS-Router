import difflib, hashlib, json, re, subprocess
from pathlib import Path
root=Path.cwd(); out=root/'.cowork-core-evidence/attempt-2'
prior=json.loads(Path('/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-1/B-readiness.json').read_text())
sources=list(prior['candidate_sources'])
patch=(root/'.cowork-core-evidence/attempt-1/candidate.patch').read_text()
reconstructed={}
for block in patch.split('diff --git ')[1:]:
 lines=block.splitlines(keepends=True);name=lines[0].split(' b/')[1].strip()
 if name not in sources:continue
 base=[] if '--- /dev/null\n' in lines else subprocess.check_output(['git','show','HEAD:'+name]).decode().splitlines(keepends=True)
 result=[];cursor=0;i=0
 while i<len(lines):
  match=re.match(r'@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@',lines[i])
  if not match:i+=1;continue
  index=max(0,int(match[1])-1);result.extend(base[cursor:index]);cursor=index;i+=1
  while i<len(lines) and not lines[i].startswith('@@ '):
   line=lines[i]
   if line.startswith((' ','-')):
    assert base[cursor]==line[1:],(name,cursor)
    cursor+=1
   if line.startswith((' ','+')):result.append(line[1:])
   i+=1
 result.extend(base[cursor:]);text=''.join(result)
 assert hashlib.sha256(text.encode()).hexdigest()==prior['candidate_sources'][name]['sha256'],name
 reconstructed[name]=text
assert set(reconstructed)==set(sources)
increment=[];cumulative=[];identities={}
for name in sources:
 data=(root/name).read_bytes();text=data.decode();compile(text,name,'exec')
 identities[name]={'sha256':hashlib.sha256(data).hexdigest(),'lines':len(text.splitlines()),'changed_from_attempt_1':text!=reconstructed[name]}
 assert identities[name]['lines']<500
 increment.extend(difflib.unified_diff(reconstructed[name].splitlines(keepends=True),text.splitlines(keepends=True),fromfile='a/'+name,tofile='b/'+name))
 base=subprocess.run(['git','show','HEAD:'+name],capture_output=True).stdout.decode()
 cumulative.extend(difflib.unified_diff(base.splitlines(keepends=True),text.splitlines(keepends=True),fromfile='a/'+name if base else '/dev/null',tofile='b/'+name))
(out/'correction.patch').write_text(''.join(increment));(out/'candidate.patch').write_text(''.join(cumulative))
manifest=json.loads((root/'acceptance/cowork_mcp/MANIFEST.json').read_text());checks={}
for section in ('contracts_sha256','protected_prior_sha256','fixture_files_sha256'):
 mismatches=[]
 for name,h in manifest[section].items():
  path=root/name if section!='fixture_files_sha256' else root/'acceptance/cowork_mcp'/name
  if hashlib.sha256(path.read_bytes()).hexdigest()!=h:mismatches.append(name)
 checks[section]={'checked':len(manifest[section]),'mismatches':mismatches}
report={'sources':identities,'protected_identity':checks,'manifest_sha256':hashlib.sha256((root/'acceptance/cowork_mcp/MANIFEST.json').read_bytes()).hexdigest(),'head':subprocess.check_output(['git','rev-parse','HEAD']).decode().strip(),'correction_patch_sha256':hashlib.sha256((out/'correction.patch').read_bytes()).hexdigest(),'candidate_patch_sha256':hashlib.sha256((out/'candidate.patch').read_bytes()).hexdigest(),'attempt_1_reconstruction':'All18 reconstructed from preserved patch and verified against independent B source hashes','compile':'18/18 passed; no pycache written','verification_script_setup_error':'Initial fixture paths mistakenly treated as repo relative; corrected prefix only'}
(out/'source-review.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='sources'},indent=2))
