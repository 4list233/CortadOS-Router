"""Offline policy and gateway integration tests. No model, Discord or live-state calls."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
os.environ['CORTADOS_LIVE_WRITE'] = os.environ['WANGCAI_LIVE_WRITE'] = '0'

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

plugin = load('review_test_plugin', ROOT / 'integrations/hermes/__init__.py')
reviews, core = plugin.reviews, plugin.core
# Pinned benchmark catalog. tables/model-scores.generated.json is rewritten by the nightly tier build (bin/refresh-tiers.sh →
# tables/benchmarks/build_tiers.py), so reading it made these policy tests follow the benchmarks: by 2026-10-13 claude-opus-5-5 had become a
# ceiling model at general 99.8 (it was non-ceiling at 84.3 on 2026-09-29, Fable ceiling at 94.6), so it, not Fable, became Astra's closest
# frontier Anthropic peer and also the eligible Samantha when Fable's pool or the native Claude lane is spent. This fixture is CortadOS
# router/model-scores.generated.json at commit 1b54a924 (nightly 2026-09-29), the catalog these tests were written and passed (23/23) against.
SCORES = json.loads((ROOT / 'tests/fixtures/model-scores.2026-09-29.generated.json').read_text())

# test_mail_read_guard_covers_review_dispatch and test_profile_instructions_are_opt_in_and_dynamic need the private
# CortadOS tree (plugins/cortados-pty/mailguard.py, faces.json): they live in tests/integration/test_router_reviews_cortados.py.
class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.stack = []
        for owner, name, value in [(core, 'STATE', self.state), (core, 'lane_health', lambda: {}),
                                    (core, 'model_scores', lambda: SCORES),
                                    (plugin, '_profile_name', lambda: 'samantha')]:
            p = patch.object(owner, name, value); p.start(); self.addCleanup(p.stop)
        plugin._pending.clear(); plugin._review_pending.clear(); plugin._session_ctx.clear()
        plugin._by_session.clear()
        self.author('turn-astra', 'gpt-6-astra')

    def author(self, turn, model):
        reviews.record_author(self.state, turn, 'author-session', 'robert-zane', model, 'openai-codex')

    def plan(self, model, **kw):
        return reviews.plan(core, {'model': model}, 'general', **kw)

    def request(self, post=None, **kw):
        args = {'author_turn_id': 'turn-astra', 'brief': 'Review /tmp/original.py for correctness.', 'use_case': 'general', **kw}
        return reviews.dispatch(core, args, 'jessica', '123', post or (lambda a: json.dumps({'success': True, 'message_id': a['to']})))

    def test_astra_gets_fable_and_smaller_claude(self):
        p = self.plan('gpt-6-astra')['reviewers']
        self.assertEqual(p['samantha']['model'], 'claude-fable-5-1')
        self.assertEqual(p['rachel']['vendor'], 'anthropic')
        self.assertFalse(p['rachel']['ceiling'])
        self.assertIn('opus', p['rachel']['model'])

    def test_fable_reverses_to_openai(self):
        p = self.plan('claude-fable-5-1')['reviewers']
        self.assertEqual(p['samantha']['model'], 'gpt-6-astra')
        self.assertEqual(p['rachel']['vendor'], 'openai')
        self.assertFalse(p['rachel']['ceiling'])

    def test_small_author_matches_small_and_gets_frontier_adversary(self):
        for model, other in [('gpt-5.6-luna', 'anthropic'), ('claude-sonnet-5', 'openai'), ('gpt-5.6-sol', 'anthropic')]:
            with self.subTest(model=model):
                p = self.plan(model)['reviewers']
                self.assertFalse(p['samantha']['ceiling'])
                self.assertTrue(p['rachel']['ceiling'])
                self.assertEqual(p['samantha']['vendor'], other)
                self.assertEqual(p['rachel']['vendor'], other)

    def test_small_worker_below_interactive_floor_can_get_comparable_review(self):
        p = self.plan('claude-haiku-4-5-20251001')['reviewers']
        self.assertLess(p['samantha']['general'], 50)
        self.assertTrue(p['rachel']['ceiling'])

    def test_other_vendors_supported(self):
        p = self.plan('gemini-3.8-flash-high')['reviewers']
        self.assertNotEqual(p['samantha']['vendor'], 'google')
        self.assertNotEqual(p['rachel']['vendor'], 'google')
        self.assertFalse(p['samantha']['ceiling']); self.assertTrue(p['rachel']['ceiling'])

    def test_cursor_is_not_a_new_vendor(self):
        p = self.plan('claude-opus-5-5-high')['reviewers']
        self.assertTrue(all(c['vendor'] != 'anthropic' for c in p.values()))

    def test_spent_frontier_does_not_silently_use_same_vendor_or_small(self):
        with self.assertRaisesRegex(ValueError, 'No eligible samantha'):
            self.plan('gpt-6-astra', health={'claude': {'ok': False}})

    def test_named_fable_pool_is_checked(self):
        with self.assertRaisesRegex(ValueError, 'No eligible samantha'):
            self.plan('gpt-6-astra', health={'claude': {'ok': True, 'pools': {'fable': {'ok': False}}}})

    def test_unknown_author_and_oversize_context_block(self):
        with self.assertRaises(ValueError): self.plan('unknown-author')
        with self.assertRaises(ValueError): self.plan('gpt-6-astra', history_chars=1000000)
        with self.assertRaises(ValueError): reviews.author_for(core, {'author_turn_id': 'missing'})

    def test_dispatch_both_and_retry_without_duplicates(self):
        posted = []
        def post(a):
            posted.append(a)
            return json.dumps({'success': True, 'message_id': a['to']})
        one = self.request(post)
        two = self.request(post)
        self.assertEqual(one['request_id'], two['request_id'])
        self.assertEqual([a['to'] for a in posted], ['samantha', 'rachel'])
        self.assertNotEqual(posted[0]['thread'], posted[1]['thread'])
        for a in posted:
            self.assertIn('/tmp/original.py', a['text'])
            self.assertIn('Do not read the other review', a['text'])
            self.assertLessEqual(len(a['text']), 1800)

    def test_partial_failure_retry_only_failed_leg(self):
        first = []
        def post(a):
            first.append(a['to'])
            return json.dumps({'success': a['to'] == 'samantha', 'message_id': a['to'], 'error': 'rate limit'})
        self.assertFalse(self.request(post)['success'])
        second = []
        self.request(lambda a: (second.append(a['to']) or json.dumps({'success': True})))
        self.assertEqual(second, ['rachel'])

    def test_unknown_delivery_not_repeated(self):
        def post(a): raise TimeoutError('timed out')
        self.request(post)
        again = Mock()
        self.request(again)
        again.assert_not_called()

    def test_explicit_blocked_retry_only_sends_that_role(self):
        packet = self.request()
        reviews.mark(core, packet['request_id'], 'samantha', 'reviewed')
        reviews.mark(core, packet['request_id'], 'rachel', 'blocked', 'pool spent')
        calls = []
        self.request(lambda a:(calls.append(a['to']) or json.dumps({'success':True})), retry_blocked=True)
        self.assertEqual(calls, ['rachel'])

    def test_receive_rechecks_only_assigned_reviewer_and_keeps_announced_model(self):
        p = self.plan('gpt-6-astra')
        health = {'claude':{'ok':True, 'pools':{'fable':{'ok':False}}}}
        got = reviews.plan(core, {'model':'gpt-6-astra'}, 'general', health=health, roles=('rachel',), locked_routes=p['reviewers'])
        self.assertEqual(got['reviewers']['rachel']['model'], p['reviewers']['rachel']['model'])
        with self.assertRaises(ValueError):
            reviews.plan(core, {'model':'gpt-6-astra'}, 'general', health=health, roles=('samantha',), locked_routes=p['reviewers'])

    def test_missing_metadata_dispatches_nothing(self):
        post = Mock()
        with self.assertRaises(ValueError): self.request(post, author_turn_id='missing')
        post.assert_not_called()

    def test_worker_provenance_and_path_validation(self):
        p = self.state / 'jobs' / 'job123'; p.mkdir(parents=True)
        (p / 'meta.json').write_text(json.dumps({'status': 'exited', 'exit_code': 0, 'model': 'claude-fable-5-1', 'runtime': 'claude'}))
        a = reviews.author_for(core, {'author_job_id': 'job123'})
        self.assertEqual(a['model'], 'claude-fable-5-1')
        with self.assertRaises(ValueError): reviews.author_for(core, {'author_job_id': '../jobs/job123'})
        with self.assertRaises(ValueError): reviews.author_for(core, {'author_job_id': 'job123', 'author_turn_id': 'turn-astra'})

    def event(self, role='samantha'):
        packet = self.request()
        text = f"[ask#abcd] <@999> [cortados-review:{packet['request_id']}:{role}] Review /tmp/original.py"
        source = SimpleNamespace(platform=SimpleNamespace(value='discord'), user_id='123', message_id=role)
        return packet, SimpleNamespace(text=text, source=source)

    def test_ticket_rejects_wrong_sender_profile_and_quoted_history(self):
        _, e = self.event()
        with self.assertRaises(ValueError): reviews.ticket_for(core, e.text, 'samantha', 'attacker')
        with self.assertRaises(ValueError): reviews.ticket_for(core, e.text, 'rachel', '123')
        self.assertIsNone(reviews.ticket_for(core, '[Recent channel messages]\n'+e.text+'\n[New message]\nhello', 'samantha', '123'))
        self.assertIsNone(reviews.ticket_for(core, 'Quote: '+e.text, 'samantha', '123'))

    def test_delivery_can_only_be_claimed_once_and_by_original_message(self):
        packet, e = self.event()
        with self.assertRaises(ValueError): reviews.claim(core, packet['request_id'], 'samantha', 'wrong-message')
        reviews.claim(core, packet['request_id'], 'samantha', e.source.message_id)
        with self.assertRaises(ValueError): reviews.claim(core, packet['request_id'], 'samantha', e.source.message_id)

    def test_gateway_override_and_shadow_bypass(self):
        _, e = self.event()
        conv = SimpleNamespace(model_override=None)
        gw = SimpleNamespace(_session_key_for_source=lambda s:'key', _claim_one_turn_restore=Mock(),
                             _session_state=lambda k:SimpleNamespace(conversation=conv), _evict_cached_agent=Mock())
        # Since CortadOS 18ff46a0 ("fix(router): account for exact session input and contain AGY truncation") the review branch measures
        # the session through _dispatch_fit (Hermes session store + hermes_constants) before choosing the reviewer. The fake gateway has
        # neither, so the measurement is supplied here, as GatewayPreservation in test_router_transport.py does: a small, known fit.
        fit = {'session_id': 'review-session', 'known': True, 'reason': None, 'history_bytes': 0,
               'current_and_recall_bytes': len(e.text.encode()), 'input_bytes': 124000 + len(e.text.encode()), 'input_tokens': 37500}
        with patch.object(plugin, '_live_enabled', return_value=True), patch.object(plugin, '_override_for', side_effect=lambda p,m:{'model':m,'provider':p}), \
             patch.object(plugin, '_dispatch_fit', return_value=fit), patch.object(core, 'decide_s1') as score:
            plugin._pre_gateway_dispatch(e, gw)
            self.assertEqual(conv.model_override['model'], 'claude-fable-5-1')
            plugin._pre_llm_call(session_id='review-session', turn_id='rev1', user_message=e.text, platform='discord', model='claude-fable-5-1')
            self.assertEqual(plugin._pending['rev1']['review']['role'], 'samantha')
            score.assert_not_called()
        self.assertEqual(plugin._pre_tool_call('agent_run', {'pin_model': True}, turn_id='rev1')['action'], 'block')
        plugin._post_api_request(turn_id='rev1', session_id='review-session', model='gpt-6-astra', provider='openai-codex')
        self.assertIn('Review blocked', plugin._transform_llm_output('looks fine', turn_id='rev1', platform='discord'))
        self.assertEqual(plugin._pre_tool_call('bus_post', {'to':'jessica','tag':'done'}, turn_id='rev1')['action'], 'block')

    def test_ordinary_turn_and_fixed_default_do_not_start_review(self):
        with patch.object(reviews, 'dispatch') as dispatch:
            plugin._pre_tool_call('terminal', {'command':'pwd'})
            self.assertIsNone(reviews.ticket_for(core, 'Please research this', 'samantha', '123'))
            dispatch.assert_not_called()
        _, e = self.event()
        with patch.object(plugin, '_live_enabled', return_value=False):
            self.assertEqual(plugin._pre_gateway_dispatch(e, object())['action'], 'skip')

    def test_actual_model_observation_not_configured_route(self):
        plugin._post_api_request(turn_id='fallback-turn', session_id='s', model='gpt-5.6-luna', provider='openai-codex')
        self.assertEqual(reviews.author_for(core, {'author_turn_id':'fallback-turn'})['model'], 'gpt-5.6-luna')

if __name__ == '__main__': unittest.main()
