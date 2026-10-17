"""Independent public-tool timings and synthetic collection scaling; no live-state writes."""
import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import time
import uuid

ROOT = Path('/Users/velox/.cortados-builds/cortados-router-repo')
sys.path.insert(0, str(ROOT))
from acceptance.mcp_worker.support import WorkerCase


def stats(values):
    return {'samples_ms': values, 'median_ms': statistics.median(values),
            'max_ms': max(values), 'count': len(values)}


async def public(candidate, directory):
    case = WorkerCase(methodName='runTest')
    case.setUp()
    report = {}
    try:
        async with case.session() as client:
            cases = {'legacy_route': ('route', case.route_request())}
            if candidate:
                from acceptance.mcp_learning.support import evidence
                value = evidence()
                cases['rich_route'] = ('route', case.route_request(task_evidence=value))
                cases['context_questions'] = ('context_requirements', {
                    'task': 'Fix parser regression', 'selection': {'mode': 's1'}})
                cases['context_ready'] = ('context_requirements', {
                    'task': 'Fix parser regression', 'selection': {'mode': 's1'},
                    'task_evidence': value})
            for name, (tool, args) in cases.items():
                # The first call is retained separately; 30 subsequent calls expose normal IPC variance.
                values = []
                for _ in range(31):
                    start = time.perf_counter_ns()
                    response = await case.call(client, tool, args)
                    values.append((time.perf_counter_ns() - start) / 1e6)
                    assert response['ok'], response
                report[name] = {'first_ms': values[0], 'warm': stats(values[1:])}
            if candidate:
                accepted = await case.call(client, 'delegate', case.request())
                terminal = await case.terminal(client, accepted['job']['job_id'])
                assert terminal['job']['state'] == 'succeeded'
                for name in ('jobs.db', 'router/router.db'):
                    target = directory / 'source' / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = sqlite3.connect((case.state / name).as_uri() + '?mode=ro', uri=True)
                    destination = sqlite3.connect(target)
                    source.backup(destination)
                    source.close()
                    destination.close()
    finally:
        case.tearDown()
    return report


def scaling(directory):
    from cortados_router.learning_data import collect_mcp
    results = []
    source = directory / 'source'
    with sqlite3.connect(source / 'jobs.db') as db:
        job = db.execute('SELECT * FROM jobs').fetchone()
    with sqlite3.connect(source / 'router/router.db') as db:
        route_columns = [r[1] for r in db.execute('PRAGMA table_info(routes)')]
        route_values = db.execute('SELECT * FROM routes WHERE turn_id=?',
                                  (json.loads(job[7])['route_id'],)).fetchone()
    for count in (0, 1, 100, 10000):
        work = directory / str(count)
        work.mkdir()
        state, data = work / 'state', work / 'data'
        (state / 'router').mkdir(parents=True)
        data.mkdir()
        for name in ('jobs.db', 'router/router.db'):
            with sqlite3.connect(source / name) as a, sqlite3.connect(state / name) as b:
                a.backup(b)
        with sqlite3.connect(state / 'jobs.db') as db:
            db.execute('DELETE FROM jobs')
            jobs = []
            routes = []
            for i in range(count):
                row = list(job)
                record = copy.deepcopy(json.loads(job[7]))
                jid = str(uuid.UUID(int=i * 3 + 1, version=4))
                rid = str(uuid.UUID(int=i * 3 + 2, version=4))
                qid = str(uuid.UUID(int=i * 3 + 3, version=4))
                record.update(job_id=jid, route_id=rid, state_dir=str(state))
                record['arguments'].update(request_id=qid, task=f'Fix synthetic parser case {i}')
                record['route']['route_id'] = rid
                encoded = json.dumps({k: v for k, v in record['arguments'].items() if k != 'request_id'},
                                     sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
                record['argument_hash'] = hashlib.sha256(encoded).hexdigest()
                row[0], row[2], row[3], row[4], row[7] = jid, qid, record['argument_hash'], encoded, json.dumps(record)
                jobs.append(tuple(row))
                route = list(route_values)
                route[route_columns.index('turn_id')] = rid
                routes.append(tuple(route))
            db.executemany('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)', jobs)
        with sqlite3.connect(state / 'router/router.db') as db:
            db.execute('DELETE FROM routes')
            db.executemany('INSERT INTO routes VALUES (' + ','.join('?' for _ in route_columns) + ')', routes)
        reference = []
        for _ in range(3):
            start = time.perf_counter_ns()
            for name, table in (('jobs.db', 'jobs'), ('router/router.db', 'routes')):
                with sqlite3.connect((state / name).as_uri() + '?mode=ro', uri=True) as db:
                    db.execute('BEGIN')
                    read = db.execute('SELECT * FROM ' + table).fetchall()
                    assert len(read) == count
            reference.append((time.perf_counter_ns() - start) / 1e6)
        samples, values = [], []
        for _ in range(3):
            start = time.perf_counter_ns()
            result = collect_mcp(state, data)
            samples.append((time.perf_counter_ns() - start) / 1e6)
            values.append(result)
            assert result['status'] == 'ok', result
            assert result['observations'] == count, result
        assert [v['added'] for v in values] == [count, 0, 0], values
        results.append({'jobs': count, 'source_bytes': sum(p.stat().st_size for p in state.rglob('*.db')),
                        'published_bytes': sum(p.stat().st_size for p in data.glob('*.jsonl')),
                        'raw_sql_read_lower_bound': stats(reference), 'complete_collection': stats(samples),
                        'results': values})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', action='store_true')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    report = {'python': sys.version, 'platform': platform.platform(),
              'candidate': args.candidate, 'public_tools': asyncio.run(public(args.candidate, args.out))}
    if args.candidate:
        report['collection'] = scaling(args.out)
    (args.out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(args.out / 'report.json')


if __name__ == '__main__':
    main()
