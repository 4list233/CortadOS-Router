"""Offline regressions for media-first routing; no models or live stores are touched."""
import contextvars
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

os.environ['CORTADOS_LIVE_WRITE'] = os.environ['WANGCAI_LIVE_WRITE'] = '0'
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('router_images_test_plugin', ROOT/'integrations/hermes/__init__.py')
plugin = importlib.util.module_from_spec(spec); spec.loader.exec_module(plugin)
images, core = plugin.image_conversion, plugin.core
CONFIG = {'provider': 'openai-codex', 'model': 'gpt-5.6-luna'}


def response(text='Chicken salad, $13.50', complexity='simple', status='complete'):
    return json.dumps({'success': True, 'analysis': json.dumps({'status': status, 'complexity': complexity, 'content': text})})


def event(text='', paths=None, types=None):
    return NS(text=text, media_urls=paths or ['/tmp/salad.jpeg'], media_types=types or ['image/jpeg'],
              message_type=NS(value='photo'), source=NS(platform=NS(value='discord'), user_id='123'))


class Images(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def test_all_attachments_even_with_long_caption(self):
        e = event('caption ' * 600, [f'/tmp/{i}.png' for i in range(5)], ['image/png'] * 5)
        self.assertEqual(images.attached_images(e), e.media_urls)

    def test_mime_wins_and_history_does_not_reconvert(self):
        e = event('[Image attached at: /tmp/old.png]\n[New message]\nhi', ['/tmp/file.png', '/tmp/photo'], ['application/pdf', 'image/png'])
        self.assertEqual(images.attached_images(e), ['/tmp/photo'])

    def test_legacy_paths_are_deduplicated(self):
        e = event('[Image attached at: /tmp/salad.jpeg]')
        self.assertEqual(images.attached_images(e), ['/tmp/salad.jpeg'])

    def test_simple_image_uses_only_small_vision_and_saves_full_text(self):
        calls = []
        async def call(path, prompt, model):
            calls.append(model); return response('x' * 5000)
        r = images.convert(['/tmp/a'], CONFIG, self.path, call=call)
        self.assertTrue(r[0]['complete']); self.assertEqual(calls, ['gpt-5.6-luna'])
        self.assertEqual(len(next(self.path.glob('*.txt')).read_text()), 5000)

    def test_complex_conversion_escalates_before_completion(self):
        calls = []
        async def call(path, prompt, model):
            calls.append(model); return response('complex diagram', 'complex')
        r = images.convert(['/tmp/a'], CONFIG, self.path, call=call)
        self.assertTrue(r[0]['complete']); self.assertEqual(calls, ['gpt-5.6-luna', 'gpt-6-astra'])

    def test_failure_retries_on_stronger_vision(self):
        async def call(path, prompt, model):
            return '{broken' if model == 'gpt-5.6-luna' else response()
        self.assertTrue(images.convert(['/tmp/a'], CONFIG, self.path, call=call)[0]['complete'])

    def test_slow_stronger_conversion_gets_its_full_budget(self):
        deadlines = []
        async def call(path, prompt, model):
            return response('dense document', 'complex')
        async def wait_for(awaitable, timeout):
            deadlines.append(timeout)
            result = await awaitable
            # Simulate a strong-model result that takes 90s: the old 60s cap
            # cancelled it even though the auxiliary vision API permits 120s.
            if len(deadlines) == 2 and timeout < 90:
                raise TimeoutError
            return result
        original_join = images.threading.Thread.join
        def tracked_join(thread, timeout):
            self.assertEqual(timeout, 185)
            original_join(thread, timeout)
        with patch.object(images.asyncio, 'wait_for', side_effect=wait_for), \
                patch.object(images.threading.Thread, 'join', tracked_join):
            result = images.convert(['/tmp/a'], CONFIG, self.path, call=call)[0]
        self.assertTrue(result['complete'])
        self.assertEqual(deadlines, [60, 120])
        self.assertNotIn('error', result)

    def test_explicit_stronger_deadline_stays_bounded_and_preserves_partial_text(self):
        deadlines = []
        async def call(path, prompt, model):
            return response('partial document', 'complex')
        async def wait_for(awaitable, timeout):
            deadlines.append(timeout)
            result = await awaitable
            if len(deadlines) == 2:
                raise TimeoutError
            return result
        config = dict(CONFIG, conversion_timeout=10, conversion_escalation_timeout=20)
        with patch.object(images.asyncio, 'wait_for', side_effect=wait_for):
            result = images.convert(['/tmp/a'], config, self.path, call=call)[0]
        self.assertEqual(deadlines, [10, 20])
        self.assertFalse(result['complete'])
        self.assertEqual(result['content'], 'partial document')
        self.assertEqual(result['outcomes'][-1]['timeout_seconds'], 20)
        self.assertFalse(list(self.path.glob('*.txt')))

    def test_partial_failure_is_not_a_complete_batch(self):
        async def call(path, prompt, model):
            return response() if path.endswith('ok') else response('', status='unreadable')
        r = images.convert(['/tmp/ok', '/tmp/bad'], CONFIG, self.path, call=call)
        self.assertEqual([x['complete'] for x in r], [True, False])
        self.assertEqual(len(list(self.path.glob('*.txt'))), 1)

    def test_profile_context_reaches_vision_worker(self):
        cv = contextvars.ContextVar('face', default='wrong'); token = cv.set('donna')
        self.addCleanup(cv.reset, token)
        async def call(path, prompt, model):
            self.assertEqual(cv.get(), 'donna'); return response()
        self.assertTrue(images.convert(['/tmp/a'], CONFIG, self.path, call=call)[0]['complete'])

    def test_current_caption_reaches_both_vision_passes_without_quoted_history(self):
        calls = []
        async def call(path, prompt, model):
            calls.append(prompt)
            return response('orange fish slices', status='complete' if model == 'gpt-6-astra' else 'needs_stronger_vision')
        result = images.convert(['/tmp/a'], CONFIG, self.path, call=call,
                                request_text='old private topic\n[New message]\nI added salmon')[0]
        self.assertTrue(result['complete']); self.assertEqual(len(calls), 2)
        for prompt in calls:
            self.assertIn('I added salmon', prompt)
            self.assertNotIn('old private topic', prompt)

    def test_native_envelope_is_not_conversion(self):
        with self.assertRaises(ValueError):
            images.parse_conversion({'success': True, '_multimodal': True, 'analysis': 'image attached'})

    def test_hermes_resize_note_preserves_model_json(self):
        outer = json.loads(response('diagram with illegible corner'))
        outer['scale_note'] = 'Image resized to 1000x800'
        outer['analysis'] = '[' + outer['scale_note'] + '] ' + outer['analysis']
        self.assertEqual(images.parse_conversion(outer)['content'], 'diagram with illegible corner')
        outer.pop('scale_note')
        with self.assertRaises(json.JSONDecodeError):
            images.parse_conversion(outer)

    def test_unrecoverable_image_stays_incomplete_with_diagnostics(self):
        async def call(path, prompt, model):
            return response('Primary text cannot be read', status='unreadable')
        result = images.convert(['/tmp/a'], CONFIG, self.path, call=call)[0]
        self.assertFalse(result['complete'])
        self.assertEqual([o['status'] for o in result['outcomes']], ['unreadable', 'unreadable'])
        self.assertFalse(list(self.path.glob('*.txt')))

    def test_answerer_gets_all_text_and_no_duplicate_vision(self):
        e = event('my task', ['/tmp/a.png', '/tmp/audio'], ['image/png', 'audio/ogg'])
        e.media_text_inlined = [None, False]
        out = images.apply_conversion(e, [{'path': '/tmp/a.png', 'complete': True, 'content': 'x' * 6000}], 'a'*32)
        self.assertIn('my task', out); self.assertIn('x' * 6000, out)
        self.assertEqual(e.media_urls, ['/tmp/audio']); self.assertEqual(e.media_types, ['audio/ogg'])
        self.assertEqual(e.media_text_inlined, [False])
        self.assertNotIn('cortados-image-route', core.clean_request(out))


class Gateway(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        plugin._pending.clear(); plugin._session_ctx.clear(); plugin._live_by_session.clear(); plugin._image_routes.clear()
        self.conv = NS(model_override=None)
        self.gw = NS(_session_key_for_source=lambda s:'agent:donna:discord:dm:test', _claim_one_turn_restore=Mock(),
                     _session_state=lambda k:NS(conversation=self.conv), _evict_cached_agent=Mock(),
                     _is_user_authorized_for_source=lambda s:True)
        self.decision = {'answers': {}, 'degraded': False, 'engine_ms': 1}
        self.route = {'chosen': {'provider':'openai-codex', 'model':'gpt-5.6-luna', 'lane':'codex', 'index':45.8, 'use_case':'conversation'},
                      'band':'R0', 'required_index':0}
        for obj, name, value in [(plugin, '_live_enabled', lambda:True), (plugin, '_vision_config', lambda:CONFIG),
                                  (plugin, '_profile_name', lambda:'donna'), (plugin, '_TRANSCRIPTS_DIR', self.path),
                                  (plugin, '_task_for_router', lambda *a, **kw:None),
                                  (plugin, '_recall_for_router', lambda *a:'context'), (plugin, '_esc_for', lambda k:None),
                                  (plugin, '_override_for', lambda p,m:{'provider':p, 'model':m})]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)

    def conversion(self, paths, config, directory, **kwargs):
        return [{'path':p, 'complete':True, 'content':f'salad {i}', 'attempts':['gpt-5.6-luna']} for i,p in enumerate(paths)]

    def test_captionless_discord_is_converted_then_scored_once_and_reused(self):
        e = event('(The user sent a message with no text content)')
        with patch.object(images, 'convert', side_effect=self.conversion), patch.object(core, 'decide_s1', return_value=self.decision) as score, patch.object(core, 'resolve', return_value=self.route) as resolve:
            plugin._pre_gateway_dispatch(e, self.gw)
            self.assertIn('salad 0', score.call_args.args[0]); self.assertIsNone(resolve.call_args.kwargs['escalate'])
        self.assertEqual(self.conv.model_override['model'], 'gpt-5.6-luna')
        # Even after another same-caption image turn, the first turn keeps its own decision.
        plugin._live_by_session.clear()
        with patch.object(core, 'decide') as rescore, patch.object(core, 'log_route') as log:
            plugin._pre_llm_call(session_id='sid', turn_id='turn1', user_message='[New message]\n'+e.text, model='gpt-5.6-luna', platform='discord')
            self.assertTrue(plugin._pending['turn1']['done'].wait(3))
            rescore.assert_not_called(); self.assertEqual(log.call_args.args[3], self.route)

    def test_long_caption_and_five_images_are_all_converted(self):
        e = event('task '*150, [f'/tmp/{i}.png' for i in range(5)], ['image/png']*5)
        with patch.object(images, 'convert', side_effect=self.conversion) as convert, patch.object(core, 'decide_s1', return_value=self.decision), patch.object(core, 'resolve', return_value=self.route):
            plugin._pre_gateway_dispatch(e, self.gw)
        self.assertEqual(len(convert.call_args.args[0]), 5)
        self.assertIn('salad 4', e.text); self.assertFalse(e.media_urls)

    def test_failed_image_is_marked_and_escalated(self):
        e = event('caption '*100)
        failed = [{'path':'/tmp/salad.jpeg', 'complete':False, 'content':'', 'attempts':[]}]
        with patch.object(images, 'convert', return_value=failed), patch.object(core, 'decide_s1', return_value=self.decision), patch.object(core, 'resolve', return_value=self.route) as resolve:
            plugin._pre_gateway_dispatch(e, self.gw)
        self.assertIn('INCOMPLETE', e.text); self.assertEqual(resolve.call_args.kwargs['escalate']['D_floor'], 9)

    def test_unauthorized_image_never_calls_vision(self):
        self.gw._is_user_authorized_for_source = lambda s:False
        with patch.object(images, 'convert') as convert:
            plugin._pre_gateway_dispatch(event(), self.gw)
        convert.assert_not_called()


if __name__ == '__main__':
    unittest.main()
