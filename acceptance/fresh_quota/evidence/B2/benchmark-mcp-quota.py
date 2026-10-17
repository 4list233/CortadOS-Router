"""Independent before/after workloads using the unchanged native protocol fixture."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument('--source', required=True)
parser.add_argument('--out', required=True)
args = parser.parse_args()
source, out = Path(args.source).resolve(), Path(args.out).resolve()
out.mkdir(parents=True, exist_ok=False)
os.environ['CORTADOS_HOME'] = str(out / 'private-state')
os.environ['CORTADOS_HERMES_HOME'] = str(out / 'private-hermes')
sys.path.insert(0, str(source))
from tests.test_mcp_route_task import RouteContract, decision
from integrations.mcp.native_metadata import NativeMetadata
from integrations.mcp.schemas import WorkerError
from cortados_router import core, route_task


def calls(case):
    path = case.path / 'fixture-calls.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def measure(name, count):
    samples = []
    for index in range(count):
        case = RouteContract()
        case.setUp()
        try:
            case.config['limits']['metadata_max_age_s'] = 30
            reader = NativeMetadata(case.config)
            request = deepcopy(case.request)
            request['selection'] = {'mode': 's1'}
            if name in ('fresh_cache_selection', 'expired_source_refresh'):
                reader.snapshot()
            if name == 'expired_source_refresh':
                reader._cached['sampled_monotonic'] -= 31
            if name in ('unknown_failure', 'zero_dispatch_refusal'):
                state = 'unknown' if name == 'unknown_failure' else 'spent'
                (case.path / 'fixture-control.json').write_text(json.dumps({'quota': state}))
            before = len(calls(case))
            error, results = None, []
            started = time.perf_counter_ns()
            with patch.object(core, 'decide', return_value=decision()):
                try:
                    if name == 'concurrent_same_source':
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            futures = [pool.submit(reader.snapshot) for _ in range(2)]
                            results = [{'models': future.result()['models']} for future in futures]
                    elif name == 'uncached_success':
                        results = [{'models': reader.snapshot()['models']}]
                    else:
                        result = route_task.route_task(request, case.config, reader)
                        results = [{'chosen': result['chosen'], 'band': result['band'], 'D': result['D']}]
                except WorkerError as exc:
                    error = exc.code
            elapsed = (time.perf_counter_ns() - started) / 1e6
            observed = calls(case)[before:]
            methods = [row['method'] for row in observed if row.get('kind') == 'metadata']
            turns = [row for row in observed if row.get('kind') not in ('metadata', 'version')]
            expected_error = {'unknown_failure': 'quota_unknown', 'zero_dispatch_refusal': 'quota_spent'}.get(name)
            assert error == expected_error, (name, error, expected_error)
            assert not turns, ('Unexpected native model execution', turns)
            if name == 'fresh_cache_selection':
                assert not methods, 'A fresh cache unexpectedly repeated a source read'
            if name in ('uncached_success', 'expired_source_refresh', 'concurrent_same_source'):
                assert methods.count('account/rateLimits/read') == 1, methods
            samples.append({'index': index, 'ms': elapsed, 'rpc_methods': methods,
                            'quota_reads': methods.count('account/rateLimits/read'),
                            'native_turns': len(turns), 'error': error, 'results': results})
        finally:
            case.doCleanups()
    times = sorted(sample['ms'] for sample in samples)
    result = {'name': name, 'samples': samples, 'n': count, 'median_ms': statistics.median(times),
              'min_ms': times[0], 'max_ms': times[-1],
              'p95_ms': times[min(count - 1, int(count * .95))]}
    (out / (name + '.json')).write_text(json.dumps(result, indent=2))
    print(json.dumps({key: result[key] for key in ('name', 'n', 'median_ms', 'p95_ms')}), flush=True)
    return result


report = {'source': str(source), 'scope': 'Synthetic native protocol; no real provider/model inference',
          'source_sha256': {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                            for name in ('integrations/mcp/native_metadata.py', 'cortados_router/route_task.py',
                                         'cortados_router/worker_admission.py', 'cortados_router/scarcity_codex_native.py')},
          'workloads': []}
for name, count in (('uncached_success', 8), ('fresh_cache_selection', 60),
                    ('expired_source_refresh', 8), ('unknown_failure', 8),
                    ('concurrent_same_source', 8), ('zero_dispatch_refusal', 8)):
    report['workloads'].append(measure(name, count))
(out / 'report.json').write_text(json.dumps(report, indent=2))
