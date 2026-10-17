"""Independent candidate actual-source measurements at frozen external seams.

No provider requests or worker turns. Preserve real collector, context, protocol,
cache and cleanup. Pure native capture additionally uses installed native bytes
and synthetic stores, without spawning the native reader.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--source', required=True)
parser.add_argument('--out', required=True)
args = parser.parse_args()
source, out = Path(args.source).resolve(), Path(args.out).resolve()
out.mkdir(parents=True, exist_ok=False)
os.environ['CORTADOS_HOME'] = str(out / 'state')
os.environ['CORTADOS_HERMES_HOME'] = str(out / 'hermes')
sys.path.insert(0, str(source))
from acceptance.quota_sources.test_actual_protocol import ActualSourceProtocol
from acceptance.quota_sources.test_native_profile import CursorNativeProfile


def stats(name, rows):
    values = sorted(row['ms'] for row in rows)
    result = {'name': name, 'samples': rows, 'n': len(rows),
              'median_ms': statistics.median(values),
              'min_ms': values[0], 'max_ms': values[-1],
              'p95_ms': values[min(len(values) - 1, int(len(values) * .95))]}
    (out / (name + '.json')).write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result[k] for k in ('name', 'n', 'median_ms', 'p95_ms')}), flush=True)
    return result


def measure(name, count):
    rows = []
    for index in range(count):
        case = ActualSourceProtocol()
        case.setUp()
        try:
            if name == 'expiry':
                case.config['limits']['metadata_max_age_s'] = 1
            actual = case.prepare()
            if name in ('cache_hit', 'expiry'):
                assert case.check(actual)['status'] == 'available'
            if name == 'expiry':
                time.sleep(1.05)  # Outside measured interval; crosses fixture one-second lifetime.
            if name == 'unknown_failure':
                case.control['mode'] = 'failure'
            if name == 'spent_refusal':
                case.control['used'] = 100
            if name == 'same_source_concurrency':
                case.control['delay'] = .02  # Controlled I/O overlap, not vendor latency.
            case.write_control()
            before = len(case.launches)
            started = time.perf_counter_ns()
            if name == 'same_source_concurrency':
                second = case.prepare()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(case.check, (actual, second)))
            else:
                results = [case.check(actual)]
            elapsed = (time.perf_counter_ns() - started) / 1e6
            expected = {'unknown_failure': 'unknown', 'spent_refusal': 'spent'}.get(name, 'available')
            assert all(row['status'] == expected for row in results), (name, results)
            reads = len(case.launches) - before
            assert reads == (0 if name == 'cache_hit' else 1), (name, reads)
            rows.append({'index': index, 'ms': elapsed, 'source_reads': reads,
                         'statuses': [row['status'] for row in results],
                         'provider_requests': 0, 'worker_turns': 0})
        finally:
            case.doCleanups()
    return stats(name, rows)


def capture_measure():
    case = CursorNativeProfile()
    case.setUp()
    rows = []
    try:
        for index in range(8):
            started = time.perf_counter_ns()
            capture = case.capture()
            elapsed = (time.perf_counter_ns() - started) / 1e6
            assert capture.profile == 'cursor-2026.10.16-e3cb207-file-v1'
            rows.append({'index': index, 'ms': elapsed, 'files': len(capture.files),
                         'provider_requests': 0, 'worker_turns': 0})
    finally:
        case.doCleanups()
    return stats('native_file_capture', rows)


report = {'source': str(source),
          'scope': 'New actual-source owner with frozen synthetic child and real pure native file capture; no provider network or S1 inference.',
          'comparison_limit': 'Baseline has no actual-source owner and real Cursor admission fails containment. No successful old/new native latency ratio is claimed.',
          'source_hashes': {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in (source / 'cortados_router').glob('quota_*.py')},
          'workloads': []}
for name, count in (('uncached_success', 8), ('cache_hit', 60), ('expiry', 8),
                    ('unknown_failure', 8), ('same_source_concurrency', 8), ('spent_refusal', 8)):
    report['workloads'].append(measure(name, count))
report['workloads'].append(capture_measure())
report['pass'] = True
(out / 'report.json').write_text(json.dumps(report, indent=2))
