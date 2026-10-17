# PURPOSE: Execute exact staged host identity-handoff functions with inert clients.
# CONTRACT: RT-FRESH-QUOTA-005 AC-FQ05; effective identity survives return/wrapping.
# DEPENDS: Actual AST function nodes from the large host module, documented binding seams.
# CEILING: Dependencies are inert transport constructors; no credential or provider reads.
import ast
import contextlib
import logging
import inspect
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch

from .stage_support import STAGE, StageCase
from .support import scope


class Wrapper:
    def __init__(self, source=None, **kwargs):
        self.source = source
        self.__dict__.update(kwargs)


class ClientBinding(StageCase):
    def setUp(self):
        super().setUp()
        self.provider, _ = self.provider_plugin()
        self.source = STAGE / 'agent/auxiliary_client.py'
        tree = ast.parse(self.source.read_text())
        names = {'_route_client', '_to_async_client', '_effective_provider_for_client'}
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual({node.name for node in nodes}, names)
        openai = ModuleType('openai')
        openai.AsyncOpenAI = Wrapper
        self.enterContext(patch.dict(sys.modules, {'openai': openai}))
        self.classes = {name: type(name, (), {}) for name in
                        ('_AuxProbeClientStub', 'CodexAuxiliaryClient', 'AnthropicAuxiliaryClient', 'BedrockAuxiliaryClient')}
        self.ns = {'contextlib': contextlib, 'inspect': inspect, 'Path': Path, 'os': os,
                   'PROVIDER_PLUGIN_SOURCE': str(self.root / 'plugins/model-providers/cortados-cli/__init__.py'), 'logger': logging.getLogger('binding-exam'), **self.classes,
                   'AsyncCodexAuxiliaryClient': Wrapper, 'AsyncAnthropicAuxiliaryClient': Wrapper,
                   'AsyncBedrockAuxiliaryClient': Wrapper, '_client_declares': lambda client, key: bool(getattr(type(client), key, False)),
                   'base_url_host_matches': lambda *args: False, '_is_official_codex_base_url': lambda *args: False,
                   '_endpoint_default_headers': lambda *args, **kwargs: {},
                   '_apply_required_codex_headers': lambda *args, **kwargs: None,
                   '_openai_http_client_kwargs': lambda *args, **kwargs: {}}
        # Include new same-module helper dependencies transitively, preserving their exact AST.
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        selected = {node.name for node in nodes}
        while True:
            needed = {node.id for function in nodes for node in ast.walk(function)
                      if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
            added = (needed & functions.keys()) - selected - self.ns.keys()
            if not added:
                break
            nodes += [functions[name] for name in sorted(added)]
            selected |= added
        future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
        module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
        exec(compile(module, str(self.source), 'exec'), self.ns)

    def request(self, provider='codex-cli', asynchronous=False):
        return NS(provider=provider, async_mode=asynchronous, is_vision=False)

    def test_explicit_client_return_gets_concrete_identity(self):
        client = NS()
        result, model = self.ns['_route_client'](self.request(), client, 'gpt-6.1-sol')
        self.assertIs(result, client)
        self.assertEqual(getattr(result, '_hermes_aux_effective_provider', None), 'codex-cli')
        self.assertEqual(model, 'gpt-6.1-sol')

    def test_existing_effective_identity_is_not_overwritten_by_auto_or_main(self):
        for provider in ('auto', 'main'):
            with self.subTest(provider=provider):
                client = NS(_hermes_aux_effective_provider='claude-cli')
                result, _ = self.ns['_route_client'](self.request(provider), client, 'claude-fable-5')
                self.assertEqual(getattr(result, '_hermes_aux_effective_provider', None), 'claude-cli')

    def test_unknown_auto_client_does_not_get_fake_concrete_identity(self):
        for provider in ('auto', 'main'):
            with self.subTest(provider=provider):
                result, _ = self.ns['_route_client'](self.request(provider), NS(), 'gpt-6.1-sol')
                self.assertNotIn(getattr(result, '_hermes_aux_effective_provider', ''), ('auto', 'main'))

    def test_specialized_async_wrappers_keep_resolved_provider(self):
        for name in ('CodexAuxiliaryClient', 'AnthropicAuxiliaryClient', 'BedrockAuxiliaryClient'):
            with self.subTest(wrapper=name):
                source = self.classes[name]()
                source._hermes_aux_effective_provider = 'codex-cli'
                source._hermes_aux_auth_scope = scope()
                result, model = self.ns['_to_async_client'](source, 'gpt-6.1-sol')
                self.assertIsNot(result, source)
                self.assertEqual(getattr(result, '_hermes_aux_effective_provider', None), 'codex-cli')
                self.assertEqual(model, 'gpt-6.1-sol')
                self.assertEqual(getattr(result, '_hermes_aux_auth_scope', None), scope())

    def test_plain_async_constructor_preserves_effective_provider(self):
        source = NS(_hermes_aux_effective_provider='codex-cli', base_url='https://fixture.invalid', api_key='inert-fixture')
        result, _ = self.ns['_to_async_client'](source, 'gpt-6.1-sol')
        self.assertEqual(getattr(result, '_hermes_aux_effective_provider', None), 'codex-cli')

    def test_async_route_return_binds_actual_returned_wrapper(self):
        source = self.classes['CodexAuxiliaryClient']()
        result, _ = self.ns['_route_client'](self.request(asynchronous=True), source, 'gpt-6.1-sol')
        self.assertIsNot(result, source)
        self.assertEqual(getattr(result, '_hermes_aux_effective_provider', None), 'codex-cli')

    def test_exact_cli_source_preserves_client_and_marks_preflight(self):
        client = self.provider.CliBrainClient(runtime='codex')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                result, _ = self.ns['_route_client'](self.request(asynchronous=asynchronous), client, 'gpt-6.1-sol')
                self.assertIs(result, client)
                self.assertEqual(getattr(result, '_hermes_aux_quota_stage', None), 'source_preflight')

    def test_impostor_or_replaced_cli_method_never_gets_preflight(self):
        client = self.provider.CliBrainClient(runtime='codex')
        client.chat.completions.create = lambda **kwargs: None
        fake_class = type('CliBrainClient', (), {})
        fake = fake_class()
        fake.runtime = 'codex'
        fake.chat = NS(completions=NS(create=lambda **kwargs: None))
        for value in (client, fake):
            result, _ = self.ns['_route_client'](self.request(), value, 'gpt-6.1-sol')
            self.assertNotEqual(getattr(result, '_hermes_aux_quota_stage', None), 'source_preflight')
