import difflib,hashlib,json,re,subprocess
from pathlib import Path
root=Path.cwd();out=root/'.cowork-core-evidence/attempt-3'
prior=json.loads((root/'.cowork-core-evidence/attempt-2/source-review.json').read_text())
sources=list(prior['sources']);old={}
patch=(root/'.cowork-core-evidence/attempt-2/candidate.patch').read_text()
for block in re.split(r'(?m)^--- ',patch)[1:]:
 lines=('--- '+block).splitlines(keepends=True);name=lines[1][6:].strip()
 base=subprocess.run(['git','show','HEAD:'+name],capture_output=True).stdout.decode().splitlines(keepends=True)
 result=[];cursor=0;i=2
 while i<len(lines):
  m=re.match(r'@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@',lines[i])
  if not m:i+=1;continue
  index=max(0,int(m[1])-1);result.extend(base[cursor:index]);cursor=index;i+=1
  while i<len(lines) and not lines[i].startswith('@@ '):
   line=lines[i]
   if line.startswith((' ','-')):
    assert base[cursor]==line[1:],(name,cursor)
    cursor+=1
   if line.startswith((' ','+')):result.append(line[1:])
   i+=1
 result.extend(base[cursor:]);old[name]=''.join(result)
 assert hashlib.sha256(old[name].encode()).hexdigest()==prior['sources'][name]['sha256'],name
assert set(old)==set(sources)
increment=[];cumulative=[];identities={}
for name in sources:
 data=(root/name).read_bytes();text=data.decode();compile(text,name,'exec')
 identities[name]={'sha256':hashlib.sha256(data).hexdigest(),'lines':len(text.splitlines()),'changed_from_attempt_2':text!=old[name]}
 assert identities[name]['lines']<500
 increment.extend(difflib.unified_diff(old[name].splitlines(True),text.splitlines(True),fromfile='a/'+name,tofile='b/'+name))
 base=subprocess.run(['git','show','HEAD:'+name],capture_output=True).stdout.decode()
 cumulative.extend(difflib.unified_diff(base.splitlines(True),text.splitlines(True),fromfile='a/'+name if base else '/dev/null',tofile='b/'+name))
(out/'correction.patch').write_text(''.join(increment));(out/'candidate.patch').write_text(''.join(cumulative))
manifest=json.loads((root/'acceptance/cowork_mcp/MANIFEST.json').read_text());checks={}
for section in ('contracts_sha256','protected_prior_sha256','fixture_files_sha256'):
 mismatches=[]
 for name,h in manifest[section].items():
  path=root/name if section!='fixture_files_sha256' else root/'acceptance/cowork_mcp'/name
  if hashlib.sha256(path.read_bytes()).hexdigest()!=h:mismatches.append(name)
 checks[section]={'checked':len(manifest[section]),'mismatches':mismatches}
 assert not mismatches
report={'sources':identities,'protected_identity':checks,'manifest_sha256':hashlib.sha256((root/'acceptance/cowork_mcp/MANIFEST.json').read_bytes()).hexdigest(),'head':subprocess.check_output(['git','rev-parse','HEAD']).decode().strip(),'correction_patch_sha256':hashlib.sha256((out/'correction.patch').read_bytes()).hexdigest(),'candidate_patch_sha256':hashlib.sha256((out/'candidate.patch').read_bytes()).hexdigest(),'compile':'18/18; no pycache','prior_reconstruction':'all18 match attempt-2 terminal source hashes'}
(out/'source-review.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='sources'},indent=2))

