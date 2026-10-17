# PURPOSE: Compare actual original/candidate resolver observations and local latency.
# CONTRACT: RT-QUOTA-EVALUATION-004; no latency pass threshold or provider calls.
# DEPENDS: Protected deterministic fixture and exact --core source argument.
# CEILING: Local synthetic timing does not predict provider or saved-checkpoint runtime.
import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from acceptance.quota_evaluation.support import candidate, configure, resolve

parser = argparse.ArgumentParser()
parser.add_argument('--core', required=True)
parser.add_argument('--out', required=True)
args = parser.parse_args()
source = Path(args.core).resolve()
report = {'core': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
          'python': sys.version, 'platform': platform.platform(), 'cases': {}}
# 100 retained samples expose local scheduling variance without expensive I/O or model
# calls; fewer conceal outliers, more add runtime without setting a latency budget.
for name, size, value in [('small', 2, 1000.0), ('typical', 12, 1000.0),
                          ('large', 48, 1000.0), ('unknown', 12, None)]:
    case = unittest.TestCase()
    try:
        configure(case, source, False)
        case.candidates = [candidate('fixture-' + str(i), float(i + 1)) for i in range(size)]
        sequences = {row['lane']: [value, value] for row in case.candidates}
        times, counts = [], []
        for _ in range(100):
            start = time.perf_counter_ns()
            result, calls = resolve(case, sequences)
            times.append((time.perf_counter_ns() - start) / 1e6)
            counts.append(len(calls))
        report['cases'][name] = {'candidates': size, 'samples_ms': times,
            'median_ms': statistics.median(times), 'read_counts': counts,
            'chosen': result['chosen'], 'alternatives': result['alternatives']}
    finally:
        case.doCleanups()
Path(args.out).write_text(json.dumps(report, indent=2) + '\n')
