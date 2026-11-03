import hashlib,json
from pathlib import Path
out=Path('.cowork-core-evidence/attempt-3')
source=json.loads((out/'source-review.json').read_text())
for name,item in source['sources'].items():
 assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==item['sha256'],name
report=json.loads((out/'full-final-frozen/report.json').read_text())
lines=['# Terminal results','',
 'Source remained frozen throughout full-final-frozen; all18 hashes rechecked after it.',
 'Interpreter: /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python (-B); umask022; TMPDIR='+str((out/'tmp').resolve())+'.','',
 'Full command: /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/cowork_mcp/evidence/run-full-suite.py '+str((out/'full-final-frozen').resolve()),'',
 'Core command: /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/cowork_mcp -t . -p '+repr('test_*.py')+' -v','',
 'Full raw summary: '+json.dumps(report['totals'])+'. Lanes: '+str(len(report['lanes']))+'; green lanes: '+str(sum(x['pass'] for x in report['lanes']))+'. Overall pass: '+str(report['pass'])+'.',
 'Core:111 methods/3 failure events/89 error events/0skips. Event totals overlap methods. All28 lanes collected; no missing lane is treated as passing. Failed benchmarks have zero methods and remain failed; successful benchmark lanes are measurements, not unit-method counts.','',
 '| Lane | Methods | Failure events | Error events | Skips | Exit | Green |',
 '|---|---:|---:|---:|---:|---:|---|']
for item in report['lanes']:
 c=item['counts'];lines.append('| '+item['name']+' | '+str(c['tests'])+' | '+str(c['failures'])+' | '+str(c['errors'])+' | '+str(c['skipped'])+' | '+str(item['exit_code'])+' | '+str(item['pass'])+' |')
lines+=['','The native sysctl boot-identity call and /bin/ps are denied by the builder sandbox. Ordinary MCP assertion failures report storage_error before intended preflight or a synthetic HTTP fixture failing to become ready; core crash/separate-process assertions follow blocked runtime startup. No protected assertions were weakened. The raw failures remain red pending host rerun.','',
 'The preliminary full-suite invocation had an additional training-teacher mode assertion; rerunning with the prescribed umask022 passes that lane. This does not erase the preliminary result. full-terminal predates the final IPC retained-lease change; full-final-frozen is the sole terminal-source accumulated run.','',
 'Transfer/performance benchmark-core cannot construct CoreRuntime, so matrix latency, memory scaling, SQL workload and stop-overshoot measurements remain BLOCKED. Historical baseline785 red and historical007 parity remain separately reported by the unchanged driver.','',
 'No cortados-workers tool was exposed; this direct builder did not invoke nested workers. No permission escalation or host-process workaround was attempted.','',
 'Terminal correction SHA256: '+source['correction_patch_sha256'],
 'Cumulative candidate SHA256: '+source['candidate_patch_sha256'],
 'Manifest SHA256: '+source['manifest_sha256'],
 'Protected inputs:3 contracts +641 prior files +18 core files verified unchanged; all18 source files compile and remain below500 physical lines.','',
 'Acceptance disposition: BLOCKED here. Root host rerun, complete measurements and independent B remain mandatory.']
(out/'TERMINAL-RESULTS.md').write_text('\n'.join(lines)+'\n')
hashes={}
for path in sorted(out.rglob('*')):
 if path.is_file() and 'tmp' not in path.relative_to(out).parts and path.name!='evidence-hashes.json':
  hashes[str(path.relative_to(out))]=hashlib.sha256(path.read_bytes()).hexdigest()
(out/'evidence-hashes.json').write_text(json.dumps(hashes,indent=2)+'\n')
print(json.dumps({'totals':report['totals'],'lanes':len(report['lanes']),'green_lanes':sum(x['pass'] for x in report['lanes']),'evidence_files_hashed':len(hashes),'source_hashes_unchanged':18}))

