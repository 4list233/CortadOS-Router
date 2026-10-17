"""Independent component fixture: old routing exams retain their exact assertions.

Only their mutable-health quota boundary is synthetic. The new independent suite
separately rejects native OAuth and unknown real dispatch scopes.
"""
from copy import deepcopy
from types import SimpleNamespace
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    outcome = yield
    if fixturedef.argname != 'policy' or not request.module.__name__.endswith('test_required_model_routing'):
        return
    core, selector, health, _ = outcome.get_result()
    monkeypatch = request.getfixturevalue('monkeypatch')
    install_boundary(monkeypatch, lambda: health)


@pytest.fixture(autouse=True)
def cron_component_boundary(request, monkeypatch):
    if not request.module.__name__.endswith('test_cron_dispatch_routing'):
        return
    original = request.module.load
    def load(name, relative):
        module = original(name, relative)
        if relative == 'cortados_router/core.py':
            install_boundary(monkeypatch, lambda: module.lane_health())
        return module
    monkeypatch.setattr(request.module, 'load', load)


def install_boundary(monkeypatch, health_read):
    from cortados_router import quota_admission
    from integrations.mcp import config

    def resources(candidates, *, config):
        captured = deepcopy(health_read())
        allowed = tuple(sorted({(c['provider'], c['model']) for c in candidates
                                if captured.get(c['lane'], {}).get('ok', False)}))
        return SimpleNamespace(allowed_routes=allowed, health=lambda: deepcopy(captured),
                               tokens_per_pct=lambda c: None, tokens_left=lambda c: None)

    def preflight(provider, model, *, config):
        lane = 'codex' if 'codex' in provider else 'claude'
        status = 'available' if health_read()[lane]['ok'] else 'spent'
        return {'quota_stage': 'source_preflight',
                'auth_scope': {'kind': 'cli_store', 'lane': lane, 'scope_id': 'a' * 64},
                'quota': {'status': status, 'kind': 'subscription', 'lane': lane,
                          'pool': 'all', 'observed_at': 2000000000, 'age_s': 0,
                          'source': 'preserved-policy-fixture', 'reason': 'Synthetic component input'}}

    monkeypatch.setattr(config, 'load_config', lambda *args, **kwargs: {'fixture': True})
    monkeypatch.setattr(quota_admission, 'current_resources', resources)
    monkeypatch.setattr(quota_admission, 'source_preflight', preflight)
