"""Per-call task observations never relabel an earlier routing decision."""
from copy import deepcopy
import json
import sqlite3
import threading
import unittest
from unittest.mock import Mock,patch

from tests import test_router_context as fixture

plugin=fixture.plugin


def snapshot(task='settlement',version=1):
    from cortados_router.task_context import CONTRACT,LIST_FIELDS
    return {'schema':CONTRACT,'status':'bound','task_id':task,'entity_id':'internal-erp',
        'objective':'Finish '+task,'version':version,'state':'in_progress',
        **{field:[] for field in LIST_FIELDS}}


class RouterCallContext(unittest.TestCase):
    setUp=fixture.RouterContext.setUp

    def start(self,native=True):
        plugin._pre_llm_call(session_id='session',turn_id='turn',user_message='Continue',
            platform='discord',incoming_message={'role':'user','platform_message_id':'native'} if native else None)

    def observe(self,ident='api',**kwargs):
        return plugin._pre_api_request(session_id=kwargs.pop('session_id','session'),turn_id='turn',
            api_request_id=ident,model='actual-model',provider='fixture',**kwargs)

    def test_task_switch_reaches_each_call_without_mutating_route_or_request(self):
        first=snapshot();second=snapshot('payroll')
        with patch.object(plugin,'_task_for_router',return_value=first) as lookup, \
             patch.object(plugin,'_recall_for_router',return_value=''), \
             patch.object(plugin.core,'log_route'),patch.object(plugin.core,'log_call_context') as record, \
             patch.object(plugin.core,'decide',return_value=self.decision) as score:
            self.start();self.assertTrue(plugin._pending['turn']['done'].wait(3))
            request={'messages':[{'role':'user','content':'Untrusted native ID other'}]};before=deepcopy(request)
            self.observe('first',request=request)
            self.assertEqual('unchanged',record.call_args.kwargs['status'])
            lookup.return_value=second;self.observe('second',request=request)
            data=record.call_args.kwargs
            self.assertEqual(second,data['task_context']);self.assertEqual('changed',data['status'])
            self.assertEqual(plugin._context_digest(first),data['scored_digest'])
            self.assertEqual('native',lookup.call_args.kwargs['native_input']['message_id'])
            self.assertEqual(before,request);score.assert_called_once()
            self.assertEqual(self.route,plugin._pending['turn']['route'])
            self.assertTrue(plugin._pending['turn']['routing_context_changed'])

    def test_missing_native_identity_or_wrong_session_does_not_guess(self):
        with patch.object(plugin,'_task_for_router',return_value=snapshot()) as lookup, \
             patch.object(plugin,'_recall_for_router',return_value=''), \
             patch.object(plugin.core,'log_route'),patch.object(plugin.core,'log_call_context') as record:
            self.start(native=False);self.assertTrue(plugin._pending['turn']['done'].wait(3))
            lookup.reset_mock();self.observe(request={'native_input':{'message_id':'forged'}})
            lookup.assert_not_called();record.assert_not_called()
            plugin._pending['turn']['routing_input']['native_input']={'platform':'discord','message_id':'native'}
            self.observe(session_id='other-session')
            lookup.assert_not_called();record.assert_not_called()

    def test_late_initial_score_detects_already_observed_changed_context(self):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def delayed(*args):
            entered.set();release.wait(3);return self.decision
        with patch.object(plugin,'_task_for_router',return_value=snapshot()) as lookup, \
             patch.object(plugin,'_recall_for_router',return_value=''), \
             patch.object(plugin.core,'log_route'),patch.object(plugin.core,'log_call_context') as record, \
             patch.object(plugin.core,'decide',side_effect=delayed):
            self.start();self.assertTrue(entered.wait(3))
            self.observe('before');self.assertEqual('score_pending',record.call_args.kwargs['status'])
            lookup.return_value=snapshot('payroll');self.observe('after')
            release.set();self.assertTrue(plugin._pending['turn']['done'].wait(3))
            self.assertTrue(plugin._pending['turn']['routing_context_changed'])

    def test_changed_or_incomplete_context_cannot_label_turn_outcome(self):
        for reason in ('routing_context_changed','routing_context_incomplete'):
            slot=plugin._slot(reason,'session');slot[reason]=True;slot['done'].set()
            class ImmediateThread:
                def __init__(self,target,**kwargs):self.target=target
                def start(self):self.target()
            with patch.object(plugin.threading,'Thread',ImmediateThread),patch.object(plugin.core,'log_outcome') as outcome:
                plugin._post_llm_call(turn_id=reason,assistant_response='Done')
            outcome.assert_not_called()

    def test_call_identity_is_idempotent_and_conflicts_do_not_rewrite_observation(self):
        data={'session_id':'session','model':'model','provider':'fixture','task_context':snapshot(),
            'scored_digest':plugin._context_digest(snapshot('previous')),
            'current_digest':plugin._context_digest(snapshot()),'status':'changed'}
        with patch.object(plugin.core,'STATE',self.root):
            plugin.core.log_call_context('turn','api',**data)
            plugin.core.log_call_context('turn','api',**data)
            with self.assertRaises(ValueError):
                plugin.core.log_call_context('turn','api',**{**data,'task_context':snapshot('different'),
                    'current_digest':plugin._context_digest(snapshot('different'))})
        with sqlite3.connect(self.root/'router.db') as conn:
            rows=conn.execute('SELECT payload FROM route_call_contexts').fetchall()
            for operation in ('UPDATE route_call_contexts SET payload=payload','DELETE FROM route_call_contexts'):
                with self.assertRaises(sqlite3.IntegrityError):conn.execute(operation)
        self.assertEqual(1,len(rows));self.assertEqual(snapshot(),json.loads(rows[0][0])['task_context'])

    def test_busy_log_writer_is_bounded_and_does_not_open_a_second_database(self):
        data={'session_id':'session','model':'model','provider':'fixture','task_context':snapshot(),
            'scored_digest':None,'current_digest':plugin._context_digest(snapshot()),'status':'score_pending'}
        with patch.object(plugin.core,'STATE',self.root),patch.object(plugin.core,'_db_lock') as lock:
            lock.acquire.return_value=False
            with self.assertRaises(TimeoutError):plugin.core.log_call_context('turn','api',**data)
            lock.acquire.assert_called_once_with(timeout=.025)
        self.assertFalse((self.root/'router.db').exists())

    def test_observation_failure_is_explicit_without_interrupting_native_call(self):
        with patch.object(plugin,'_task_for_router',return_value=snapshot()), \
             patch.object(plugin,'_recall_for_router',return_value=''), \
             patch.object(plugin.core,'log_route'),patch.object(plugin.core,'log_call_context',side_effect=OSError):
            self.start();self.assertTrue(plugin._pending['turn']['done'].wait(3))
            self.assertIsNone(self.observe())
            self.assertTrue(plugin._pending['turn']['routing_context_incomplete'])
