"""Router identity regressions using fake recall; never import the live Context engine."""
import contextvars
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

os.environ['CORTADOS_LIVE_WRITE'] = os.environ['WANGCAI_LIVE_WRITE'] = '0'
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('router_context_test_plugin', ROOT/'integrations/hermes/__init__.py')
plugin = importlib.util.module_from_spec(spec); spec.loader.exec_module(plugin)
TASK_LOOKUP = plugin._task_for_router


class RouterContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'faces.json').write_text(json.dumps({'faces': {
            'harvey': {'home': str(self.root/'hermes'), 'alias_homes': [str(self.root/'hermes/profiles/harvey')]},
            'donna': {'home': str(self.root/'hermes/profiles/donna')},
        }}))
        self.home = contextvars.ContextVar('test_home', default=str(self.root/'unmapped'))
        constants = ModuleType('hermes_constants')
        constants.get_hermes_home = self.home.get
        self.decision = {'answers': {}, 'degraded': False, 'engine_ms': 1}
        self.route = {'chosen': {'provider': 'openai-codex', 'model': 'gpt-5.6-luna', 'lane': 'codex', 'index': 45.8},
                      'band': 'R0', 'required_index': 0}
        for p in [patch.dict(sys.modules, {'hermes_constants': constants}), patch.object(plugin.core, 'REPO', self.root),
                  patch.object(plugin, '_task_for_router', return_value=None),
                  patch.object(plugin, '_live_enabled', return_value=True), patch.object(plugin.reviews, 'ticket_for', return_value=None),
                  patch.object(plugin, '_override_for', return_value={'provider': 'openai-codex', 'model': 'gpt-5.6-luna'}),
                  patch.object(plugin, '_esc_for', return_value=None), patch.object(plugin.core, 'decide_s1', return_value=self.decision),
                  patch.object(plugin.core, 'decide', return_value=self.decision), patch.object(plugin.core, 'resolve', return_value=self.route)]:
            p.start(); self.addCleanup(p.stop)
        plugin._pending.clear(); plugin._session_ctx.clear(); plugin._live_by_session.clear(); plugin._image_routes.clear()

    def gateway(self, home, namespace):
        self.home.set(str(self.root/home))
        key = f'agent:{namespace}:discord:group:123:456'
        event = NS(text='Please check the client proposal details', media_urls=[], media_types=[],
                   source=NS(platform=NS(value='discord'), user_id='456'))
        gateway = NS(_session_key_for_source=lambda s:key, _claim_one_turn_restore=Mock(),
                     _session_state=lambda k:NS(conversation=NS(model_override=None)), _evict_cached_agent=Mock())
        plugin._pre_gateway_dispatch(event, gateway)
        return key

    def test_main_key_uses_harvey_but_preserves_session_key(self):
        with patch.object(plugin, '_recall_for_router', return_value='client fact') as recall:
            key = self.gateway('hermes', 'main')
        self.assertEqual(recall.call_args.args[0], key)
        self.assertEqual(recall.call_args.args[2], 'harvey')
        self.assertEqual(plugin._live_by_session[key]['context'], 'client fact')

    def test_local_context_stop_is_not_provider_authorship_or_training_outcome(self):
        with patch.object(plugin.reviews, 'record_author') as author, \
             patch.object(plugin.core, 'log_outcome') as outcome:
            plugin._post_api_request(turn_id='blocked-context', session_id='session',
                model='routed-model', provider='openai-codex',
                response_model='cortados-local-context-guard')
            slot = plugin._pending['blocked-context']
            self.assertTrue(slot['context_blocked'])
            self.assertEqual([], slot['calls'])
            author.assert_not_called()
            slot['done'].set()
            workers = []
            class ImmediateThread:
                def __init__(self, target, **kwargs): self.target = target
                def start(self): workers.append(self.target); self.target()
            with patch.object(plugin.threading, 'Thread', ImmediateThread):
                plugin._post_llm_call(turn_id='blocked-context', assistant_response='Local context stop')
            outcome.assert_not_called()
            self.assertEqual(1, len(workers))
            self.assertNotIn('blocked-context', plugin._pending)

    def test_ordinary_provider_response_still_records_author_and_call(self):
        with patch.object(plugin.reviews, 'record_author') as author:
            plugin._post_api_request(turn_id='normal-context', session_id='session',
                model='routed-model', provider='openai-codex', response_model='routed-model')
            self.assertEqual(1, len(plugin._pending['normal-context']['calls']))
            author.assert_called_once()

    def test_live_scores_and_shadow_logs_the_same_task_snapshot(self):
        task={'schema':'cortados.routing-task.v1','status':'bound','version':3}
        with patch.object(plugin,'_task_for_router',return_value=task), \
             patch.object(plugin,'_recall_for_router',return_value='fact'), \
             patch.object(plugin.core,'decide_s1',return_value=self.decision) as score, \
             patch.object(plugin.core,'log_route') as log:
            key=self.gateway('hermes','main')
            self.assertEqual(task,score.call_args.kwargs['task_context'])
            self.assertEqual(task,plugin._live_by_session[key]['task_context'])
            plugin._pre_llm_call(session_id='actual-session',turn_id='same-task',
                                 user_message='Please check the client proposal details',platform='discord')
            self.assertTrue(plugin._pending['same-task']['done'].wait(3))
            self.assertEqual(1,score.call_count)
            self.assertEqual(task,log.call_args.args[1]['task_context'])

    def test_cold_gateway_resolves_actual_session_before_context_lookup(self):
        hook=Mock(return_value={'status':'bound'})
        module=ModuleType('context.hermes_engine');module.task_context_for_router=hook
        gateway=NS(session_store=NS(lookup_by_session_key=Mock(return_value=NS(session_id='actual-session'))))
        with patch.dict(sys.modules,{'context.hermes_engine':module}):
            TASK_LOOKUP('agent:main:discord:123','harvey',gateway)
        hook.assert_called_once_with('actual-session',face='harvey')

    def test_shadow_cannot_reuse_same_text_from_an_older_task_snapshot(self):
        self.home.set(str(self.root/'hermes'))
        old={'schema':'cortados.routing-task.v1','status':'bound','version':2}
        new={**old,'version':3}
        plugin._live_by_session['other-key']={'face':'harvey','ts':time.time(),'text':'Continue',
            'text_raw':'Continue','decision':self.decision,'route':self.route,'task_context':old}
        with patch.object(plugin,'_task_for_router',return_value=new), \
             patch.object(plugin,'_recall_for_router',return_value='fact'), \
             patch.object(plugin.core,'decide',return_value=self.decision) as score, \
             patch.object(plugin.core,'log_route') as log:
            plugin._pre_llm_call(session_id='new-task',turn_id='new-task-turn',user_message='Continue',platform='discord')
            self.assertTrue(plugin._pending['new-task-turn']['done'].wait(3))
            self.assertEqual(new,score.call_args.args[1]['task_context'])
            self.assertEqual(new,log.call_args.args[1]['task_context'])

    def test_shadow_passes_task_to_scorer_before_logging(self):
        self.home.set(str(self.root/'hermes'))
        task={'schema':'cortados.routing-task.v1','status':'bound','version':4}
        with patch.object(plugin,'_task_for_router',return_value=task), \
             patch.object(plugin,'_recall_for_router',return_value='fact'), \
             patch.object(plugin.core,'decide',return_value=self.decision) as score, \
             patch.object(plugin.core,'log_route') as log:
            plugin._pre_llm_call(session_id='task-session',turn_id='task-turn',
                                 user_message='Continue',platform='discord')
            self.assertTrue(plugin._pending['task-turn']['done'].wait(3))
            self.assertEqual(task,score.call_args.args[1]['task_context'])
            self.assertEqual(task,log.call_args.args[1]['task_context'])

    def test_alias_home_and_named_face(self):
        for home, namespace, expected in [('hermes/profiles/harvey', 'harvey', 'harvey'),
                                          ('hermes/profiles/donna', 'donna', 'donna')]:
            with self.subTest(home=home), patch.object(plugin, '_recall_for_router', return_value='') as recall:
                self.gateway(home, namespace)
                self.assertEqual(recall.call_args.args[2], expected)

    def test_unknown_home_does_not_inherit_key_grants(self):
        with patch.object(plugin, '_recall_for_router', return_value='') as recall:
            self.gateway('unmapped', 'harvey')
        self.assertEqual(recall.call_args.args[2], 'default')
        self.assertNotIn('default', json.loads((self.root/'faces.json').read_text())['faces'])

    def test_shadow_keeps_each_concurrent_turn_profile(self):
        barrier = threading.Barrier(2)
        seen = []
        def recall(session, text, face, images):
            barrier.wait(timeout=3)
            seen.append((session, face, plugin._profile_name()))
            return face + ' facts'
        with patch.object(plugin, '_recall_for_router', side_effect=recall), patch.object(plugin.core, 'log_route') as log:
            for face, home in [('harvey', 'hermes'), ('donna', 'hermes/profiles/donna')]:
                self.home.set(str(self.root/home))
                plugin._pre_llm_call(session_id=face+'-session', turn_id=face, user_message='Check the client proposal', platform='discord')
            self.home.set(str(self.root/'unmapped'))
            for face in ('harvey', 'donna'):
                self.assertTrue(plugin._pending[face]['done'].wait(5))
            self.assertEqual(log.call_count, 2)
        self.assertCountEqual(seen, [('harvey-session', 'harvey', 'harvey'), ('donna-session', 'donna', 'donna')])

    def test_shadow_does_not_reuse_another_faces_same_request(self):
        self.home.set(str(self.root/'hermes/profiles/donna'))
        text = 'Check the client proposal'
        other = {'face': 'harvey', 'ts': time.time(), 'text': text, 'text_raw': text,
                 'decision': self.decision, 'route': self.route, 'context': 'Harvey private fact'}
        plugin._live_by_session['agent:main:discord:group:1'] = other
        plugin._image_routes['a'*32] = other
        for turn, raw in [('plain', text), ('image', text+' [cortados-image-route:'+'a'*32+']')]:
            with patch.object(plugin, '_recall_for_router', return_value='Donna fact') as recall, patch.object(plugin.core, 'log_route') as log:
                plugin._pre_llm_call(session_id='donna-session', turn_id=turn, user_message=raw, platform='discord')
                self.assertTrue(plugin._pending[turn]['done'].wait(3))
                self.assertEqual(recall.call_args.args[2], 'donna')
                self.assertEqual(log.call_args.args[1]['context'], 'Donna fact')


if __name__ == '__main__':
    unittest.main()
