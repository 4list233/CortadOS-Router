"""Incident regressions: fake gateways/temp SQLite only; no inference or live writes."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, Mock

os.environ['CORTADOS_LIVE_WRITE'] = os.environ['WANGCAI_LIVE_WRITE'] = '0'
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('router_transport_test', ROOT/'integrations/hermes/__init__.py')
plugin = importlib.util.module_from_spec(spec); spec.loader.exec_module(plugin)
transport, core = plugin.transport, plugin.core


class IdentityAndSize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = sqlite3.connect(self.home/'state.db'); self.addCleanup(self.db.close)
        self.db.executescript('''CREATE TABLE sessions(id TEXT, end_reason TEXT);
            CREATE TABLE messages(session_id TEXT, content TEXT, tool_calls TEXT,
                api_content TEXT, active INTEGER DEFAULT 1, observed INTEGER DEFAULT 0);''')
        self.key = 'agent:donna:discord:dm:100000000000000001'
        self.sid = '20260101_000000_0000abcd'
        self.db.execute('INSERT INTO sessions VALUES (?,NULL)', (self.sid,))
        self.db.execute('INSERT INTO messages(session_id,content,tool_calls) VALUES (?,?,?)',
                        (self.sid, 'earlier exchanges ' * 18000, '{"arguments":"tool-result-evidence"}'))
        self.db.commit()
        self.gateway = NS(session_store=NS(lookup_by_session_key=Mock(
            return_value=NS(session_id=self.sid, last_prompt_tokens=0))))
        self.config = {'harness_tokens':31000, 'headroom':1.2}

    def measure(self, cache=None, current='What is on the agenda?'):
        return transport.measure(self.gateway,self.key,current,'meeting preference',cache or {},self.home,self.config)

    def test_real_identifier_pair_and_cold_start_read_exact_history(self):
        fit = self.measure({self.sid+'-other':{'history_bytes':9999999}})
        self.gateway.session_store.lookup_by_session_key.assert_called_once_with(self.key)
        self.assertTrue(fit['known']); self.assertEqual(self.sid,fit['session_id'])
        self.assertGreater(fit['history_bytes'],300000); self.assertLess(fit['history_bytes'],400000)
        self.assertGreater(fit['input_bytes'],180000)

    def test_current_multibyte_request_recall_and_tool_envelope_count(self):
        base = self.measure(); current = '飯' * 50000
        large = self.measure({self.sid:{'envelope_bytes':250000}},current=current)
        self.assertGreaterEqual(large['current_and_recall_bytes'],150000)
        self.assertEqual(250000,large['envelope_bytes'])
        self.assertGreater(large['input_tokens'],base['input_tokens'])

    def test_observer_includes_tool_arguments_and_does_not_mutate_tool_loop(self):
        messages = [{'role':'user','content':'when is the review'},
                    {'role':'assistant','tool_calls':[{'id':'q','function':{'name':'read_notes','arguments':'x'*5000}}]},
                    {'role':'tool','tool_call_id':'q','content':'COURSE101 notes'}]
        original = json.dumps(messages)
        plugin._pre_api_request(session_id=self.sid,conversation_history=messages,
                                system_prompt='system',request={'tools':[{'schema':'x'*200000}]})
        cached = plugin._session_ctx[self.sid]
        self.assertGreater(cached['history_bytes'],5000); self.assertGreater(cached['envelope_bytes'],200000)
        self.assertEqual(original,json.dumps(messages))

    def test_unknown_or_stale_compression_is_not_known_empty(self):
        self.gateway.session_store.lookup_by_session_key.return_value = None
        self.assertFalse(self.measure()['known'])
        self.gateway.session_store.lookup_by_session_key.return_value = NS(session_id=self.sid)
        self.db.execute("UPDATE sessions SET end_reason='compression'");self.db.commit()
        self.assertFalse(self.measure()['known'])

    def test_session_reset_does_not_reuse_previous_session_cache(self):
        self.db.execute('INSERT INTO sessions VALUES (?,NULL)',('new',));self.db.commit()
        self.gateway.session_store.lookup_by_session_key.return_value = NS(session_id='new')
        fit = self.measure({self.sid:{'history_bytes':900000,'envelope_bytes':700000}})
        self.assertTrue(fit['known']);self.assertEqual(0,fit['history_bytes'])
        self.assertEqual(124000,fit['envelope_bytes'])

    def test_read_only_scan_keeps_omitted_rows_and_database_unchanged(self):
        self.db.execute('INSERT INTO messages VALUES (?,?,?,NULL,0,0)',(self.sid,'x'*900000,None));self.db.commit()
        before=(self.home/'state.db').read_bytes();fit=self.measure()
        self.assertLess(fit['history_bytes'],400000)
        self.assertEqual(before,(self.home/'state.db').read_bytes())

    def test_display_and_api_content_are_alternatives_not_double_history(self):
        before=self.measure()['history_bytes']
        self.db.execute('UPDATE messages SET api_content=content');self.db.commit()
        self.assertEqual(before,self.measure()['history_bytes'])


class TransportGates(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.policy=Path(self.tmp.name)/'budgets.json'
        budgets=json.loads(core.BUDGETS_PATH.read_text());budgets['context']['ceiling_tokens']['antigravity']=40000
        self.policy.write_text(json.dumps(budgets))
        for p in [patch.object(core,'BUDGETS_PATH',self.policy),patch.object(core,'lane_health',return_value={}),
                  patch.object(core,'scored_candidates',return_value=None)]:
            p.start();self.addCleanup(p.stop)
        self.degraded={'answers':None,'degraded':True}
        # Use tiny deterministic table candidates; model availability/scarcity
        # never launches a probe in these tests.
        self.table=core.table();self.table=json.loads(json.dumps(self.table))
        tiny=[{'lane':'antigravity','provider':'agy-cli','model':'gemini-test'},
              {'lane':'claude','provider':'claude-cli','model':'claude-test'}]
        for band in self.table['candidates']:
            self.table['candidates'][band]=[dict(c) for c in tiny]
        # A degraded decision no longer reaches the band candidates: since the configured scorer-unavailable fallback
        # (tables/routing-table.json fallback.routes = codex gpt-5.6-terra; CortadOS contracts CTX-ADM-103 AC-01 and CTX-ADM-001
        # "scorer-unavailable fallback remains configured GPT Terra family constrained by capability, availability, permissions
        # and fit", pinned by tests/test_router_terra_fallback.py) it selects among fallback.routes. Copying the live table carried
        # that Terra route in, so these gates were never applied to the tiny candidates. The deterministic table therefore also
        # configures the tiny pair as its fallback routes: the same transport gates, on the path degraded turns really take.
        self.table['fallback']['routes']=[dict(c) for c in tiny]
        p=patch.object(core,'table',return_value=self.table);p.start();self.addCleanup(p.stop)

    def test_oversized_current_request_excludes_agy_even_with_zero_history(self):
        r=core.resolve(self.degraded,input_fit={'known':True,'input_tokens':80000,'input_bytes':270000})
        self.assertEqual('claude',r['chosen']['lane'])

    def test_utf8_byte_gate_applies_even_below_token_ceiling(self):
        r=core.resolve(self.degraded,input_fit={'known':True,'input_tokens':39000,'input_bytes':180001})
        self.assertEqual('claude',r['chosen']['lane'])
        self.assertTrue(any('byte ceiling' in x['why'] for x in r['skipped']))

    def test_unknown_history_excludes_constrained_route(self):
        r=core.resolve(self.degraded,input_fit={'known':False,'input_tokens':38000,'input_bytes':125000})
        self.assertEqual('claude',r['chosen']['lane'])

    def test_all_transports_too_small_never_rescues_first_candidate(self):
        r=core.resolve(self.degraded,input_fit={'known':True,'input_tokens':250000,'input_bytes':1000000})
        self.assertIsNone(r['chosen']);self.assertTrue(r['fit_blocked'])

    def test_zero_ceiling_containment_excludes_empty_history_on_degraded_route(self):
        d=json.loads(self.policy.read_text());d['context']['ceiling_tokens']['antigravity']=0;self.policy.write_text(json.dumps(d))
        self.assertEqual('claude',core.resolve(self.degraded)['chosen']['lane'])

    def test_scored_unfitting_stronger_model_cannot_hide_fitting_same_pool_model(self):
        # This one pins the SCORED path. With configured fallback routes a degraded decision never reaches scored candidates (see
        # setUp), so this table configures none: core.resolve then scores a degraded decision in the fallback band, as before.
        self.table['fallback'].pop('routes')
        candidates=[{'lane':'claude','provider':'claude-cli','model':model,
                     'family':model,'index':index,'general':index,'price':1,
                     'tokens_per_s':10,'ceiling':False,'use_case':'conversation'}
                    for model,index in [('large-small-window',90),('small-large-window',80)]]
        with patch.object(core,'scored_candidates',return_value=candidates), \
             patch.object(core,'required_index',return_value=50), \
             patch.object(core,'lane_ceiling',side_effect=lambda lane,model:30000 if model=='large-small-window' else 200000), \
             patch.object(core,'_pool_tokens_per_pct',return_value=None), \
             patch.object(core,'_tokens_left_for',return_value=None):
            r=core.resolve(self.degraded,input_fit={'known':True,'input_tokens':80000,'input_bytes':280000})
        self.assertEqual('small-large-window',r['chosen']['model'])


class GatewayPreservation(unittest.TestCase):
    def test_no_estimated_fit_preserves_unadmitted_request_for_host_preflight(self):
        event=NS(text='when is the review',media_urls=[],media_types=[],
                 source=NS(platform=NS(value='discord'),user_id='user'))
        gateway=NS(_session_key_for_source=lambda _: 'agent:tutor:discord:group:study:user',
                   _claim_one_turn_restore=Mock(),_evict_cached_agent=Mock())
        fit={'session_id':'course-session','known':True,'history_bytes':900000,
             'input_bytes':1100000,'input_tokens':300000,'reason':None}
        with patch.object(plugin,'_live_enabled',return_value=True), \
             patch.object(plugin,'_profile_name',return_value='tutor'), \
             patch.object(plugin.reviews,'ticket_for',return_value=None), \
             patch.object(plugin,'_recall_for_router',return_value='COURSE101'), \
             patch.object(plugin,'_task_for_router',return_value=None,create=True), \
             patch.object(plugin,'_dispatch_fit',return_value=fit), \
             patch.object(core,'decide_s1',return_value={'answers':{},'degraded':False,'engine_ms':1}), \
             patch.object(core,'resolve',return_value={'fit_blocked':True,'chosen':None}):
            result=plugin._pre_gateway_dispatch(event,gateway)
        self.assertIsNone(result)  # NOT a pre-admission skip/drop.
        self.assertEqual('when is the review',event.text)
        gateway._claim_one_turn_restore.assert_not_called()

    def test_escalation_never_matches_a_similar_session_id(self):
        import time
        plugin._session_ctx['course-session-other']={'escalate':{'D_floor':11,'ts':time.time()}}
        plugin._session_ctx.pop('course-session',None)
        self.assertIsNone(plugin._esc_for('course-session'))

if __name__=='__main__':unittest.main()
