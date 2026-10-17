# PURPOSE: Exercise actual staged auxiliary dispatch with a controlled host hook.
# CONTRACT: RT-FRESH-QUOTA-005 AC-FQ05; router mode never bypasses quota.
# DEPENDS: Exact staged auxiliary_model_routing source and documented host hook API.
# CEILING: Fake transports count physical sends; no host imports or model calls.
import asyncio
from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
from threading import Lock
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch

from .support import scope
from .stage_support import StageCase, relocate

STAGE = Path(os.environ.get('CORTADOS_FRESH_QUOTA_STAGE',
             '/Users/velox/.cortados-builds/fresh-quota-activation')).resolve()


def quota(status='available'):
    return {'router': 'cortados-router', 'quota_stage': 'dispatch', 'quota': {
        'status': status, 'kind': 'subscription', 'lane': 'codex', 'pool': 'all',
        'observed_at': 2000000000, 'age_s': 0, 'source': 'exam', 'reason': 'exam ' + status}}


class AuxiliaryDispatch(StageCase):
    def setUp(self):
        super().setUp()
        self.provider, _ = self.provider_plugin()
        self.sent, self.hooks = [], []
        self.result, self.registered = [quota()], True
        plugins = ModuleType('hermes_cli.plugins')
        def invoke(name, **kwargs):
            self.assertEqual(name, 'admit_model_quota')
            self.hooks.append(deepcopy(kwargs))
            if isinstance(self.result, Exception):
                raise self.result
            return deepcopy(self.result)
        plugins.invoke_hook = invoke
        plugins.has_hook = lambda name: self.registered
        host = ModuleType('hermes_cli')
        host.plugins = plugins
        agent = ModuleType('agent')
        model = ModuleType('agent.agent_model_routing')
        model.admitted = lambda receipt: nullcontext()
        model.estimate_payload_tokens = lambda kwargs: 1
        clients = ModuleType('agent.auxiliary_client')
        clients._effective_provider_for_client = lambda client, fallback='': getattr(client, '_hermes_aux_effective_provider', fallback)
        modules = {'hermes_cli': host, 'hermes_cli.plugins': plugins, 'agent': agent,
                   'agent.agent_model_routing': model, 'agent.auxiliary_client': clients}
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        source = self.root / 'auxiliary_model_routing.py'
        relocate(STAGE / 'agent/auxiliary_model_routing.py', source, {
            'PROVIDER_PLUGIN_SOURCE': self.root / 'plugins/model-providers/cortados-cli/__init__.py'})
        spec = importlib.util.spec_from_file_location('fresh_quota_staged_auxiliary', source)
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.assertEqual(Path(self.adapter.__file__).resolve(), source)
        self.client = NS(_hermes_aux_effective_provider='codex-cli', _hermes_aux_auth_scope=scope(),
                         chat=NS(completions=NS(create=self.create)))
        self.kwargs = {'model': 'gpt-6.1-sol', 'messages': [{'role': 'user', 'content': 'fixture'}]}

    def create(self, **kwargs):
        self.sent.append(deepcopy(kwargs))
        return 'answer'

    def mode(self, required):
        receipt = {'provider': 'codex-cli', 'model': 'gpt-6.1-sol', 'context_length': 10000}
        return self.adapter._CALL.set({'required': required, 'receipt': receipt,
                                      'attempts': 0, 'lock': Lock()})

    def assert_refused(self, callback, code):
        with self.assertRaises(self.adapter.AuxiliaryRoutingError) as raised:
            callback()
        self.assertIn(code, str(raised.exception))
        self.assertEqual(self.sent, [], 'quota refusal must precede physical transport')

    def test_unknown_and_spent_refuse_sync_in_both_router_modes(self):
        for required in (False, True):
            for status in ('unknown', 'spent'):
                with self.subTest(required=required, status=status):
                    token = self.mode(required)
                    self.result = [quota(status)]
                    try:
                        self.assert_refused(lambda: self.adapter.required_send(self.client, self.kwargs), 'quota_' + status)
                    finally:
                        self.adapter._CALL.reset(token)

    def test_unknown_and_spent_refuse_async_in_both_router_modes(self):
        async def create(**kwargs):
            return self.create(**kwargs)
        self.client.chat.completions.create = create
        for required in (False, True):
            for status in ('unknown', 'spent'):
                with self.subTest(required=required, status=status):
                    token = self.mode(required)
                    self.result = [quota(status)]
                    try:
                        self.assert_refused(lambda: asyncio.run(self.adapter.required_asend(self.client, self.kwargs)), 'quota_' + status)
                    finally:
                        self.adapter._CALL.reset(token)

    def test_available_mode_off_checks_actual_client_identity(self):
        self.assertEqual(self.adapter.required_send(self.client, self.kwargs), 'answer')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.hooks), 1)
        self.assertEqual(self.hooks[0]['provider'], 'codex-cli')
        self.assertEqual(self.hooks[0]['model'], 'gpt-6.1-sol')
        self.assertEqual(self.hooks[0]['auth_scope'], scope())
        self.assertEqual(self.hooks[0]['quota_stage'], 'dispatch')

    def test_registered_malformed_exception_or_duplicate_results_refuse(self):
        for result in ([], [{}], [quota(), quota()], RuntimeError('fixture failure')):
            with self.subTest(result=repr(result)):
                self.result = result
                self.assert_refused(lambda: self.adapter.required_send(self.client, self.kwargs), 'quota_unknown')

    def test_registered_hook_cannot_accept_unbound_client(self):
        del self.client._hermes_aux_effective_provider
        self.assert_refused(lambda: self.adapter.required_send(self.client, self.kwargs), 'quota_unknown')

    def test_generic_host_without_callback_retains_behavior(self):
        self.registered = False
        del self.client._hermes_aux_effective_provider
        self.assertEqual(self.adapter.required_send(self.client, self.kwargs), 'answer')
        self.assertEqual(len(self.sent), 1)

    def test_deferred_sync_first_next_rechecks_after_quota_changes(self):
        def deferred(**kwargs):
            self.sent.append(kwargs)
            yield 'chunk'
        self.client.chat.completions.create = deferred
        stream = self.adapter.required_send(self.client, self.kwargs)
        self.assertEqual(self.sent, [])
        self.result = [quota('spent')]
        self.assert_refused(lambda: next(stream), 'quota_spent')

    def test_deferred_async_first_anext_rechecks_after_quota_changes(self):
        async def deferred(**kwargs):
            self.sent.append(kwargs)
            yield 'chunk'
        async def create(**kwargs):
            return deferred(**kwargs)
        self.client.chat.completions.create = create
        async def run():
            stream = await self.adapter.required_asend(self.client, self.kwargs)
            self.assertEqual(self.sent, [])
            self.result = [quota('spent')]
            await anext(stream)
        self.assert_refused(lambda: asyncio.run(run()), 'quota_spent')

    def test_source_preflight_result_never_grants_actual_dispatch_permission(self):
        wrong_stage = quota()
        wrong_stage['quota_stage'] = 'source_preflight'
        self.result = [wrong_stage]
        self.assert_refused(lambda: self.adapter.required_send(self.client, self.kwargs), 'quota_unknown')

    def test_unproved_sdk_scope_does_not_become_configured_source_preflight(self):
        del self.client._hermes_aux_auth_scope
        self.assert_refused(lambda: self.adapter.required_send(self.client, self.kwargs), 'quota_unknown')

    def test_exact_cli_source_transport_gets_source_preflight(self):
        client = self.provider.CliBrainClient(runtime='codex')
        client._hermes_aux_effective_provider = 'codex-cli'
        client._hermes_aux_quota_stage = 'source_preflight'
        response = quota()
        response['quota_stage'] = 'source_preflight'
        self.result = [response]
        # Only execute the full dispatch owner, never the real provider method.
        with self.adapter._dispatch(client, self.kwargs):
            pass
        self.assertEqual(self.hooks[-1]['quota_stage'], 'source_preflight')
        self.assertEqual(self.hooks[-1]['auth_scope']['kind'], 'unproven')

    def test_cli_source_mark_cannot_be_forged_or_survive_replaced_transport(self):
        real = self.provider.CliBrainClient(runtime='codex')
        real.chat.completions.create = self.create
        wrong_runtime = self.provider.CliBrainClient(runtime='claude')
        fake_class = type('CliBrainClient', (), {})
        fake = fake_class()
        fake.runtime = 'codex'
        fake.chat = NS(completions=NS(create=self.create))
        for client in (real, wrong_runtime, fake):
            with self.subTest(client=type(client).__name__, runtime=client.runtime):
                client._hermes_aux_effective_provider = 'codex-cli'
                client._hermes_aux_quota_stage = 'source_preflight'
                def enter_dispatch():
                    with self.adapter._dispatch(client, self.kwargs):
                        pass
                self.assert_refused(enter_dispatch, 'quota_unknown')

    def test_available_callback_with_malformed_observation_metadata_refuses(self):
        for field, value in (('age_s', None), ('age_s', float('nan')), ('age_s', True),
                             ('observed_at', None), ('observed_at', float('inf')),
                             ('lane', None), ('pool', None)):
            with self.subTest(field=field, value=value):
                result = quota()
                result['quota'][field] = value
                self.result = [result]
                self.assert_refused(lambda: self.adapter.required_send(self.client, self.kwargs), 'quota_unknown')
