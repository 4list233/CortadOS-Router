"""Operator check of the installed MCP/context/feedback chain; synthetic scratch work only."""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from cortados_router.learning_data import collect_mcp, bind_snapshot

ROOT = Path('/Users/velox/.cortados-builds/mcp-learning-verification/native-learning')
PYTHON = '/Users/velox/.local/share/cortados-workers/releases/mcp-learning-v1-20261017/venv/bin/python'
CONFIG = '/Users/velox/.config/cortados-workers/config.json'
STATE = Path('/Users/velox/.local/state/cortados-workers')
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'}


async def main():
    ROOT.mkdir(mode=0o700, exist_ok=False)
    work = ROOT / 'workspace'
    work.mkdir()
    data = ROOT / 'data'
    data.mkdir()
    report = {'started_at_ms': int(time.time() * 1000), 'pass': False, 'checks': []}
    environment = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG') if k in os.environ}
    environment.update(PYTHONDONTWRITEBYTECODE='1', CORTADOS_S1_URL='http://127.0.0.1:8431')
    params = StdioServerParameters(command=PYTHON, args=['-B', '-m', 'integrations.mcp', '--config', CONFIG],
                                  cwd=str(ROOT), env=environment)
    async def call(client, tool, arguments):
        result = await client.call_tool(tool, arguments)
        body = result.structuredContent or json.loads(next(x.text for x in result.content if x.type == 'text'))
        assert not result.isError and body['ok'], (tool, body)
        return body
    try:
        with (ROOT / 'frontend-stderr.log').open('w') as log:
            async with stdio_client(params, errlog=log) as streams:
                async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=35)) as client:
                    await client.initialize()
                    catalog = await client.list_tools()
                    assert {x.name for x in catalog.tools} == {'route', 'delegate', 'job_status', 'job_result', 'job_cancel', 'context_requirements', 'job_feedback'}
                    report['catalog'] = [x.model_dump(mode='json') for x in catalog.tools]
                    report['checks'].append('installed seven-tool catalog')
                    request = {'task': 'Write exactly MCP_LEARNING_NATIVE_OK with no newline into worker-ok.txt in this folder. Then reply exactly MCP_LEARNING_NATIVE_OK.',
                               'context': 'Temporary integration check. Use a command tool for the one file. No other changes or external actions.',
                               'selection': {'mode': 'explicit', 'model': 'gpt-6-luna'}}
                    partial = await call(client, 'context_requirements', request)
                    assert partial['context_requirements']['status'] == 'needs_context'
                    report['partial_context'] = partial
                    request['task_evidence'] = {'schema': 'cortados.mcp-task-evidence.v1',
                        'task_group_id': str(uuid.uuid4()), 'components': ['worker-ok.txt'], 'dependencies': [],
                        'constraints': ['Only create worker-ok.txt in the assigned scratch folder'], 'uncertainties': [],
                        'verification': ['Planner reads worker-ok.txt and compares exact bytes to MCP_LEARNING_NATIVE_OK'],
                        'references': [{'source': 'operator smoke specification', 'text': 'The expected marker is MCP_LEARNING_NATIVE_OK with no newline.'}]}
                    ready = await call(client, 'context_requirements', request)
                    assert ready['context_requirements']['status'] == 'ready'
                    s1 = await call(client, 'context_requirements', dict(request, selection={'mode': 's1'}))
                    assert s1['context_requirements']['status'] == 'ready' and s1['context_requirements']['scorer_chars'] > 0
                    report.update(ready_context=ready, s1_preflight=s1)
                    report['checks'].append('context questions then complete evidence; pure S1 budget check')
                    request.update(request_id=str(uuid.uuid4()), cwd=str(work), timeout_s=90,
                                   permissions={'network': False, 'mcp_servers': []})
                    (ROOT / 'request.json').write_text(json.dumps(request, indent=2))
                    accepted = await call(client, 'delegate', request)
                    report['accepted'] = accepted
                    (ROOT / 'report.json').write_text(json.dumps(report, indent=2))
                    job_id = accepted['job']['job_id']
                    deadline = time.monotonic() + 100
                    while True:
                        result = await call(client, 'job_result', {'job_id': job_id})
                        if result['job']['state'] in TERMINAL:
                            break
                        assert time.monotonic() < deadline, 'Worker exceeded check deadline'
                        await asyncio.sleep(.2)
                    report['result'] = result
                    assert result['job']['state'] == 'succeeded', result
                    marker = work / 'worker-ok.txt'
                    assert marker.read_bytes() == b'MCP_LEARNING_NATIVE_OK'
                    assert result['result']['summary'] == 'MCP_LEARNING_NATIVE_OK'
                    report['checks'].append('native subscription worker and exact independently read marker')
                    report['before_feedback_collection'] = collect_mcp(STATE, data)
                    before = bind_snapshot(data)
                    old = next(x for x in before['rows'] if x['job_id'] == job_id)
                    assert old['outcome_annotation']['verification_status'] == 'unreviewed'
                    feedback = {'job_id': job_id, 'feedback_id': str(uuid.uuid4()), 'verdict': 'accepted',
                        'summary': 'Operator independently read the exact expected marker bytes from the scratch output.',
                        'checks': [{'kind': 'artifact', 'reference': str(marker) + '#sha256=' + hashlib.sha256(marker.read_bytes()).hexdigest(), 'claim': 'passed'}]}
                    receipt = await call(client, 'job_feedback', feedback)
                    replay = await call(client, 'job_feedback', feedback)
                    assert replay == receipt
                    report.update(feedback=feedback, receipt=receipt, feedback_replay_identical=True)
                    report['checks'].append('durable planner assertion and exact idempotent receipt')
                    report['after_feedback_collection'] = collect_mcp(STATE, data)
                    after = bind_snapshot(data)
                    current = next(x for x in after['rows'] if x['job_id'] == job_id)
                    assert current['id'] != old['id']
                    assert current['outcome_annotation']['planner_verdict'] == 'accepted'
                    assert current['outcome_annotation']['feedback_ids'] == [feedback['feedback_id']]
                    assert {'id': old['id'], 'reason': 'superseded'} in after['excluded']
                    report['learning_revision'] = {'before': old['id'], 'after': current['id'],
                        'partition_before': before['partitions'][old['id']], 'partition_after': after['partitions'][current['id']],
                        'verification_status': current['outcome_annotation']['verification_status']}
                    assert report['learning_revision']['partition_before'] == report['learning_revision']['partition_after']
                    report['checks'].append('real read-only collection: late feedback revision replaces eligibility without moving partition')
                    with sqlite3.connect((STATE / 'jobs.db').as_uri() + '?mode=ro', uri=True) as db:
                        assert db.execute('SELECT COUNT(*) FROM job_feedback WHERE feedback_id=?', (feedback['feedback_id'],)).fetchone()[0] == 1
                    report['pass'] = True
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        report['finished_at_ms'] = int(time.time() * 1000)
        (ROOT / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({'pass': report['pass'], 'checks': report['checks'], 'report': str(ROOT / 'report.json')}))


asyncio.run(main())
