# PURPOSE: Bind actual resolver source to deterministic quota observation sequences.
# CONTRACT: RT-QUOTA-EVALUATION-004; no live state or model calls.
# DEPENDS: stdlib; candidate resolver loaded by exact path.
# CEILING: Synthetic quota observations prove consistency, not provider quota freshness.
from contextlib import ExitStack
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_core(path):
    spec = importlib.util.spec_from_file_location('quota_evaluation_core', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate(lane, price):
    return {'lane': lane, 'provider': 'custom', 'model': 'fixture-' + lane,
            'family': 'fixture-' + lane, 'index': 60, 'general': 60,
            'price': price, 'ceiling': False, 'use_case': 'code'}


def configure(case, path, injected):
    case.stack = ExitStack()
    case.addCleanup(case.stack.close)
    private = case.stack.enter_context(tempfile.TemporaryDirectory(prefix='quota-exam-'))
    case.stack.enter_context(patch.dict(os.environ, {
        'CORTADOS_HOME': private, 'CORTADOS_HERMES_HOME': private,
        'CORTADOS_REPO': private, 'CORTADOS_ROUTING_TABLE': str(ROOT / 'tables/routing-table.json')}))
    case.core = load_core(path)
    case.injected = injected
    case.candidates = [candidate('codex', 2.0), candidate('claude', 4.0)]
    table = json.loads((ROOT / 'tables/routing-table.json').read_text())
    table['selection'] = {}
    for name, value in {'table': table, '_policy': {}, '_context_cfg': {},
                        'lane_health': {}, 'model_scores': {}, 'required_index': 50,
                        '_tokens_left_for': None}.items():
        case.stack.enter_context(patch.object(case.core, name, return_value=value))
    case.stack.enter_context(patch.object(case.core, 'scored_candidates',
        side_effect=lambda *args: copy.deepcopy(case.candidates)))
    case.health = {}


def resolve(case, sequences):
    calls = []
    values = {key: iter(value) for key, value in sequences.items()}

    def estimate(candidate):
        calls.append(candidate['lane'])
        return next(values[candidate['lane']])

    answer = {name: 0 for name in case.core.FEATURES}
    answer.update(steps=3, ambiguity=2, novelty=1, parts=0, work_kind='code',
                  outward=False, needs_clarification=False)
    legacy = AssertionError('injected resource reader must remain authoritative') if case.injected else estimate
    with patch.object(case.core, '_pool_tokens_per_pct', side_effect=legacy), \
            patch.object(case.core, 'lane_health', return_value=case.health):
        options = {'resources': SimpleNamespace(health=lambda: case.health,
                    tokens_per_pct=estimate, tokens_left=lambda candidate: None)} if case.injected else {}
        result = case.core.resolve({'answers': answer, 'degraded': False}, **options)
    return result, calls
