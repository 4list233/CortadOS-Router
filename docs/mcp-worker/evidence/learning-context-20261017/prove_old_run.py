"""Prove an already-running shell keeps its environment across atomic adapter/script replacement."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path('/Users/velox/.cortados-builds/cortados-router-repo')
BEFORE = Path('/Users/velox/.cortados-builds/mcp-learning-verification/live-before/train/evaluate.py')
CHILD = r'''
from contextlib import redirect_stdout
import io,json,os,sys
from pathlib import Path
from unittest.mock import patch
from acceptance.mcp_learning.pipeline_support import fixture_row,pipeline,write_rows
data=Path(sys.argv[2]); data.mkdir(exist_ok=True)
entry=fixture_row(split='holdout')
for name,rows in [('requests',[entry[0]]),('mcp-observations',[entry[1]]),('labels',[entry[2]])]:
    write_rows(data/(name+'.jsonl'),rows)
with pipeline('evaluate',data,source=Path(sys.argv[1])) as (module,traces), patch.object(sys,'argv',['evaluate.py','--base','synthetic','--out',str(data/'report')]):
    with redirect_stdout(io.StringIO()): module.main()
report=json.loads((data/'report/report.json').read_text()); report.pop('when',None)
print(json.dumps({'flag_present':'CORTADOS_MCP_LEARNING_V1' in os.environ,'report':report,'scorer':traces['scorer']},sort_keys=True))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='mcp-old-run-proof-') as temporary:
        scratch = Path(temporary)
        child, adapter, shell = (scratch / name for name in ('probe.py', 'evaluate.py', 'nightly.sh'))
        child.write_text(CHILD)
        adapter.write_bytes(BEFORE.read_bytes())
        # Positional arguments avoid interpreting any filesystem string as shell code.
        shell.write_text('#!/bin/bash\nprintf "READY\\n"\nread -r trigger\nexec "$1" "$2" "$3" "$4"\n')
        initial_inode = shell.stat().st_ino
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'TMPDIR') if key in os.environ}
        env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1',
                   CORTADOS_HOME=str(scratch / 'state'), CORTADOS_REPO=str(ROOT))
        command = [sys.executable, str(child), str(adapter), str(scratch / 'data')]
        baseline = subprocess.run(command, env=env, cwd=scratch, text=True, capture_output=True, check=True)
        process = subprocess.Popen(['/bin/bash', str(shell), *command], env=env, cwd=scratch,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert process.stdout.readline() == 'READY\n'
            assert process.poll() is None
            replacement = scratch / 'replacement.py'
            replacement.write_bytes(args.candidate.read_bytes())
            os.replace(replacement, adapter)
            replacement = scratch / 'replacement.sh'
            replacement.write_text('#!/bin/bash\nexport CORTADOS_MCP_LEARNING_V1=1\nprintf "NEW_SCRIPT_MUST_NOT_RUN\\n"\nexit 90\n')
            os.replace(replacement, shell)
            new_inode = shell.stat().st_ino
            stdout, stderr = process.communicate('continue\n', timeout=30)
            assert process.returncode == 0, (process.returncode, stderr)
            before, after = json.loads(baseline.stdout), json.loads(stdout)
            assert before == after, (before, after)
            assert not after['flag_present']
            assert initial_inode != new_inode
            report = {'pass': True, 'old_script_inode': initial_inode, 'replacement_script_inode': new_inode,
                      'shell_pid': process.pid, 'candidate': str(args.candidate),
                      'legacy_environment_retained': True, 'inputs_outputs_identical': True,
                      'baseline': before, 'after_replacement': after}
            args.output.write_text(json.dumps(report, indent=2) + '\n')
            print(args.output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
