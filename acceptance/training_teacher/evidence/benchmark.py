"""Synthetic labeling overhead; provider inference is replaced by fixed valid answers."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
import tracemalloc
import types
import unittest
from unittest.mock import patch

ROOT = Path('/Users/velox/.cortados-builds/cortados-router-repo')
BASELINE = Path(__file__).resolve().parent / 'baseline-label.py'
sys.path.insert(0, str(ROOT))
from acceptance.training_teacher.support import TeacherCase, COMMON, SYSTEM, ANSWER, answers, rows
from cortados_router import teacher


def legacy_module():
    spec = importlib.util.spec_from_file_location('benchmark_legacy_label', BASELINE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'common': COMMON}):
        spec.loader.exec_module(module)
    return module


def one_run(kind, count, historical, samples):
    case = TeacherCase()
    case.setUp()
    try:
        corpus = rows(count)
        snapshot = case.learning.bind_snapshot(case.data, requests=corpus)
        case.snapshot = snapshot
        (case.data / 'requests.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in corpus))
        existing = [{'id': r['id'], 'sample': n, 'teacher': 'claude-opus-5' if r['split'] == 'holdout' else 'claude-sonnet-5',
                     'ts': 1, 'answers': ANSWER} for r in corpus[:historical] for n in range(samples)]
        initial = ''.join(json.dumps(r) + '\n' for r in existing).encode()
        (case.data / 'labels.jsonl').write_bytes(initial)
        calls = []
        replacements = []
        original_replace = os.replace

        def available(model, batch, **kwargs):
            calls.append(len(batch))
            return answers(batch), {'usage': {}, 'session_id': 'synthetic'}

        def counted_replace(source, target, *args, **kwargs):
            replacements.append(Path(source).stat().st_size)
            return original_replace(source, target, *args, **kwargs)

        if kind == 'legacy':
            module = legacy_module()
            module.DATA = case.data
            module.LABELS = case.data / 'labels.jsonl'
            module._stop.clear()
            contexts = [patch.object(module, 'call_teacher', side_effect=available),
                        patch.object(module, '_scarce', return_value=0.0),
                        patch.object(sys, 'argv', ['label.py', '--k', str(samples), '--workers', '3'])]
            action = module.main
        else:
            case.transport(claude=available if kind == 'candidate-primary' else None)
            contexts = []
            action = lambda: case.run_labels(k=samples)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {'usage': types.SimpleNamespace(record=lambda row: None)}))
            for context in contexts:
                stack.enter_context(context)
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(os, 'replace', side_effect=counted_replace))
            tracemalloc.start()
            started = time.perf_counter()
            action()
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        final = (case.data / 'labels.jsonl').read_bytes()
        assert final.startswith(initial), 'Benchmark changed historical labels'
        parsed = [json.loads(line) for line in final.splitlines()]
        signature = hashlib.sha256(json.dumps(sorted((r['id'], r['sample'], r['answers']) for r in parsed),
                                            sort_keys=True).encode()).hexdigest()
        return {'kind': kind, 'requests': count, 'historical_ids': historical, 'samples': samples,
                'wall_s': elapsed, 'python_peak_bytes': peak, 'primary_calls': len(calls),
                'primary_batch_sizes': calls, 'codex_starts': case.broker.native_starts if kind != 'legacy' else 0,
                'new_labels': len(parsed) - len(existing), 'label_signature': signature,
                'new_label_bytes': len(final) - len(initial), 'atomic_replace_count': len(replacements),
                'atomic_replace_logical_bytes': sum(replacements)}
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        case.doCleanups()


report = {'schema': 'cortados.teacher-benchmark.v1', 'provider_calls': 0,
          'scope': 'Synthetic complete labeling phase; excludes provider latency and actual model quality.',
          'baseline_source_sha256': hashlib.sha256(BASELINE.read_bytes()).hexdigest(), 'runs': []}
# Three repetitions expose local scheduling variance without generating large synthetic I/O repeatedly.
for workload in [(12, 0, 1), (4554, 4071, 3)]:
    for repetition in range(3):
        pair = [one_run(kind, *workload) for kind in ('legacy', 'candidate-primary')]
        assert pair[0]['label_signature'] == pair[1]['label_signature'], 'Candidate primary labels differ'
        for row in pair:
            row['repetition'] = repetition
            report['runs'].append(row)
    report['runs'].append(one_run('candidate-fallback', *workload))
report['boundary_measurements'] = []
for suffix in ('test_second_writer_refuses_while_first_context_owns_lock',
               'test_crash_after_labels_replace_reconciles_without_duplicate_lines'):
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'acceptance/training_teacher'),
                                              pattern='test_attempts.py', top_level_dir=str(ROOT))
    def members(group):
        for member in group:
            if isinstance(member, unittest.TestSuite):
                yield from members(member)
            else:
                yield member
    selected = [test for test in members(suite) if test.id().endswith('.' + suffix)]
    assert len(selected) == 1, 'Protected boundary measurement is missing'
    started = time.perf_counter()
    result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite(selected))
    assert result.wasSuccessful() and not result.skipped, 'Protected boundary check failed'
    report['boundary_measurements'].append({'check': suffix, 'wall_s': time.perf_counter() - started})
output = Path(sys.argv[1])
report['pass'] = True
output.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'report': str(output), 'runs': len(report['runs']), 'provider_calls': 0}))
