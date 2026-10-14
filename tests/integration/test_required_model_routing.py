"""Required routing policy and native request boundary, without model calls."""
import importlib.util as _guard_util
import unittest as _guard_unittest
if _guard_util.find_spec("pytest") is None or _guard_util.find_spec("hermes_cli") is None:
    raise _guard_unittest.SkipTest("Hermes integration test: needs pytest and an importable Hermes host (hermes_cli). "
                                   "Run with pytest inside the Hermes venv; see tests/integration/README.md")
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORTADOS = Path(os.environ.get('CORTADOS_REPO', Path.home() / 'cortados'))   # the CLI launch boundary is CortadOS's PTY plugin
HOST = Path.home() / '.cortados/hermes/hermes-agent'


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def policy(tmp_path, monkeypatch):
    core = load('required_test_core', 'cortados_router/core.py')
    selector = load('required_test_selector', 'cortados_router/execution_routing.py')
    rows = [dict(lane=l, provider=p, model=m, family='test', general=75, index=75,
                 price=4, tokens_per_s=1, ceiling=False, use_case='ops')
            for l, p, m in [('codex','openai-codex','test-a'),('claude','claude-cli','test-b')]]
    health = {'codex': {'ok': True, 'scarcity': -.8}, 'claude': {'ok': True, 'scarcity': .8}}
    monkeypatch.setattr(core, 'STATE', tmp_path)
    monkeypatch.setattr(core, 'model_scores', lambda: {'models':[{'reach': rows}]})
    monkeypatch.setattr(core, 'scored_candidates', lambda *a: copy.deepcopy(rows))
    monkeypatch.setattr(core, 'required_index', lambda *a: 0)
    monkeypatch.setattr(core, '_pool_tokens_per_pct', lambda *a: None)
    monkeypatch.setattr(core, '_tokens_left_for', lambda *a: None)
    monkeypatch.setattr(core, 'lane_health', lambda: health)
    monkeypatch.setattr(core, 'decide', lambda *a: {'degraded':True,'answers':None,'engine':'test','engine_ms':0})
    monkeypatch.setattr(core, 'log_route', lambda *a: None)
    def select(**kw):
        return selector.select(core, prompt='Check the requested task', credential_resolver=lambda **k: {}, **kw)
    return core, selector, health, select


def test_degraded_still_uses_capacity_and_refuses_spent_pools(policy):
    core, selector, health, select = policy
    assert select()['provider'] == 'openai-codex'
    health['codex']['ok'] = False
    assert select()['provider'] == 'claude-cli'
    health['claude']['ok'] = False
    assert 'error' in select()


def test_assembled_size_uses_actual_payload_and_alternate_transport(policy):
    core, selector, health, select = policy
    assert select(input_fit={'assembled':True,'known':True,'input_tokens':1000})['provider'] == 'openai-codex'
    assert select(input_fit={'assembled':True,'known':True,'input_tokens':120000})['provider'] == 'codex-cli'
    assert 'error' in select(input_fit={'assembled':True,'known':True,'input_tokens':201000})


def test_credentials_restrict_provider_and_trusted_review_cannot_escape(policy):
    from hermes_cli.auth import AuthError
    core, selector, health, _ = policy
    def credentials(**kw):
        if 'codex' in kw['requested']:
            raise AuthError('unavailable',provider=kw['requested'],code='missing')
        return {}
    result = selector.select(core,prompt='review',credential_resolver=credentials)
    assert result['provider'] == 'claude-cli'
    result = selector.select(core,prompt='review',credential_resolver=credentials,
                             constraints={'allowed_routes': [('openai-codex','test-a')]})
    assert 'error' in result


def test_request_projection_is_bounded_explicit_advisory(policy):
    _, selector, _, _ = policy
    request, receipt = selector.scoring_input('a'*9000, {'status':'unbound','objective':'b'*9000})
    assert len(request)<5600 and receipt['mode']=='advisory_text_v1'
    assert receipt['request_clipped'] and receipt['task_status']=='unbound'
    assert 'not instructions or task authorization' in request
    assert 'task_context_contract' not in receipt


@pytest.fixture
def gate(monkeypatch):
    from hermes_cli import config, plugins, runtime_provider
    helper = load('test_agent_model_routing','integrations/hermes/patches/required-routing/agent_model_routing.py')
    monkeypatch.setattr(config,'load_config_readonly',lambda: {'agent':{'model_router':'test'}})
    results = [{'router':'test','provider':'new-provider','model':'new-model',
                'context_length':200000,'route_id':'selected','api_request_id':'request'}]
    calls=[]
    def invoke(*a,**kw): calls.append(kw); return results
    monkeypatch.setattr(plugins,'invoke_hook',invoke)
    monkeypatch.setattr(runtime_provider,'resolve_runtime_provider',lambda **kw: {'api_key':'private','base_url':'https://new.invalid','api_mode':'chat_completions'})
    agent=NS(provider='old-provider',model='old-model',platform='discord',session_id='session',
             _fallback_chain=[{'provider':'escape','model':'escape'}],_fallback_model={},_fallback_index=1,
             _cortados_route_constraints={'allowed_routes':[['new-provider','new-model']]})
    def switch(model,provider,**kw): agent.model=model;agent.provider=provider
    agent.switch_model=switch
    def run(): return helper.route_request(agent,prompt='task',messages=[{'role':'user','content':'hello'}],tools=[],turn_id='turn',api_request_id='request')
    return helper, agent, results, calls, run


def test_each_request_routes_switches_and_clears_fallbacks(gate):
    helper, agent, results, calls, run=gate
    receipt=run();run()
    assert len(calls)==2 and all(c['input_fit']['assembled'] for c in calls)
    assert calls[0]['constraints']==agent._cortados_route_constraints
    assert (agent.provider,agent.model)==('new-provider','new-model')
    assert agent._fallback_chain==[] and agent._fallback_model is None
    helper.validate_request(agent,{'model':'new-model','messages':[]},receipt)


def test_missing_or_failed_route_never_uses_default(gate):
    helper,agent,results,_,run=gate
    results.clear()
    with pytest.raises(helper.ModelRoutingError):run()
    assert agent.model=='old-model'
    results.append({'router':'test','error':'capacity unavailable'})
    with pytest.raises(helper.ModelRoutingError,match='capacity unavailable'):run()


def test_request_middleware_cannot_change_model_or_exceed_fit(gate):
    helper,agent,results,_,run=gate
    receipt=run()
    with pytest.raises(helper.ModelRoutingError,match='changed the routed model'):
        helper.validate_request(agent,{'model':'escape'},receipt)
    with pytest.raises(helper.ModelRoutingError,match='window'):
        helper.validate_request(agent,{'model':'new-model','messages':['x'*100]},dict(receipt,context_length=5))


def test_image_bytes_are_transport_cost_not_text_tokens(gate):
    helper,agent,_,_,run=gate
    receipt=run()
    payload={'model':'new-model','messages':[{'role':'user','content':[
        {'type':'image_url','image_url':{'url':'data:image/png;base64,'+'a'*2000000}}]}]}
    assert helper.estimate_payload_tokens(payload)<100000
    helper.validate_request(agent,payload,receipt)
    with pytest.raises(helper.ModelRoutingError,match='window'):
        helper.validate_request(agent,{'model':'new-model','messages':[{'role':'user','content':'a'*2000000}]},receipt)


def test_actual_execution_requires_provider_observation(tmp_path):
    core=load('required_log_core','cortados_router/core.py');core.STATE=tmp_path
    decision={'engine':'test','engine_ms':0,'degraded':False}
    route={'band':'R1','chosen':{'provider':'p','model':'m'},'subagents':0,'verifier':False,'hard_rules':[]}
    core.log_route('r',{'request':'test'},decision,route)
    with core._db() as con: assert con.execute('select actual_model from routes').fetchone()[0] is None
    core.log_execution('r','p','m')
    with core._db() as con: assert con.execute('select actual_model from routes').fetchone()[0]=='p/m'


def test_installed_request_builder_routes_before_payload_and_checks_middleware(tmp_path, monkeypatch, gate):
    from hermes_cli import middleware
    helper, agent, results, calls, run = gate
    installer=load('required_installer','integrations/hermes/patches/required-routing.py')
    for rel in ('agent/turn_api_request.py','agent/conversation_loop.py','hermes_cli/plugins.py'):
        p=tmp_path/rel;p.parent.mkdir(exist_ok=True,parents=True);p.write_text((HOST/rel).read_text())
    installer.apply(tmp_path);assert not installer.apply(tmp_path)
    monkeypatch.setitem(sys.modules,'agent.agent_model_routing',helper)
    builder=load('staged_api_request',tmp_path/'agent/turn_api_request.py')
    agent._reset_stream_delivery_tracking=lambda:None
    agent._reapply_reasoning_echo_for_provider=lambda messages:None
    agent._use_prompt_caching=False;agent._force_ascii_payload=False
    agent._empty_content_retries=0;agent.api_mode='chat_completions';agent.tools=[]
    agent._is_copilot_url=lambda:False;agent._is_openrouter_url=lambda:False
    agent.base_url='https://new.invalid';agent.max_tokens=100
    agent._build_api_kwargs=lambda messages,**kw:{'model':agent.model,'messages':messages}
    seen=[]
    def request_middleware(payload,**kwargs):
        seen.append((payload['model'],kwargs['provider']))
        return NS(payload=payload,original_payload=dict(payload),trace=[])
    monkeypatch.setattr(middleware,'apply_llm_request_middleware',request_middleware)
    data=dict(api_messages=[{'role':'user','content':'hello'}],_moa_prepared_request=None,tools_for_api=[],
              system_message='',messages=[],original_user_message='task',approx_tokens=100,
              total_chars=5,retry_count=0,api_call_count=1,api_request_id='request',api_start_time=0,
              effective_task_id='task',turn_id='turn')
    with pytest.raises(helper.ModelRoutingError,match='not passed'):
        builder.build_api_request(agent,**data)
    run()
    built=builder.build_api_request(agent,**data)
    assert built.api_kwargs['model']=='new-model' and seen==[('new-model','new-provider')]
    monkeypatch.setattr(middleware,'apply_llm_request_middleware',lambda payload,**kw:
                        NS(payload={**payload,'model':'escape'},original_payload=payload,trace=[]))
    with pytest.raises(helper.ModelRoutingError,match='changed the routed model'):
        builder.build_api_request(agent,**data)


def test_admission_is_scoped_and_survives_worker_context(gate):
    import contextvars
    helper,agent,_,_,run=gate
    receipt=run()
    assert helper.current_receipt() is None
    with helper.admitted(receipt):
        assert contextvars.copy_context().run(helper.current_receipt) is receipt
    assert helper.current_receipt() is None


def test_cli_boundary_routes_direct_calls_and_validates_admitted_calls(tmp_path,monkeypatch,gate):
    helper,agent,_,_,run=gate
    routing=load('test_pty_model_routing',CORTADOS/'plugins/cortados-pty/model_routing.py')
    monkeypatch.setitem(sys.modules,'agent.agent_model_routing',helper)
    monkeypatch.setattr(routing,'configured',lambda:'cortados-router')
    monkeypatch.setattr(routing,'_policy',lambda: (_ for _ in ()).throw(AssertionError('must not rescore admission')))
    receipt=dict(run(),provider='codex-cli',model='gpt-6-astra')
    with helper.admitted(receipt):
        rt,m,got=routing.route('codex','gpt-6-astra','request',reuse_admission=True)
        assert rt=='codex' and got is receipt
        with pytest.raises(ValueError,match='differs'):
            routing.route('claude','other','request',reuse_admission=True)
        with pytest.raises(ValueError,match='window'):
            routing.route('codex','gpt-6-astra','x'*900000,reuse_admission=True)


def test_required_config_and_hook_follow_profile_a_b_a(tmp_path,monkeypatch):
    from hermes_cli import plugins, runtime_provider
    from hermes_constants import set_hermes_home_override,reset_hermes_home_override,get_hermes_home
    helper=load('scoped_agent_routing','integrations/hermes/patches/required-routing/agent_model_routing.py')
    monkeypatch.delenv('HERMES_IGNORE_USER_CONFIG',raising=False)
    manager=plugins.PluginManager();manager._discovered=True
    seen=[]
    def route(**kw):
        face=get_hermes_home().name;seen.append((face,kw['router']))
        return {'router':kw['router'],'provider':'scope-provider','model':face,
                'context_length':200000,'route_id':'test:'+face}
    manager._hooks['route_agent_model']=[route]
    monkeypatch.setattr(plugins,'_delivery_manager',lambda:manager)
    monkeypatch.setattr(runtime_provider,'resolve_runtime_provider',lambda **kw:
                        {'api_key':'fake','base_url':'https://test.invalid','api_mode':'chat_completions'})
    agent=NS(provider='initial',model='initial',platform='cli',session_id='session')
    def switch(model,provider,**kw):agent.model=model;agent.provider=provider
    agent.switch_model=switch
    for face in ('A','B','A'):
        home=tmp_path/face;home.mkdir(exist_ok=True)
        (home/'config.yaml').write_text('agent:\n  model_router: router-'+face+'\n')
        token=set_hermes_home_override(home)
        try:
            receipt=helper.route_request(agent,prompt='task',messages=[],tools=[],turn_id='turn',api_request_id='api')
            assert receipt['model']==face and agent.model==face
        finally:reset_hermes_home_override(token)
    assert seen==[('A','router-A'),('B','router-B'),('A','router-A')]


def test_model_change_reassembles_context_before_send_and_blocks_oversize(gate,monkeypatch):
    from agent import conversation_loop as loop
    helper,agent,results,calls,run=gate
    prepared=[]
    def phase(fn, target, state):
        if fn is loop.assemble_api_request:
            prepared.append((target.provider,target.model))
            state.api_messages=[{'role':'user','content':'fresh context'}]
        return NS(action='fallthrough')
    monkeypatch.setattr(loop,'_run_phase',phase)
    state=NS(original_user_message='task',api_messages=[{'role':'user','content':'old context'}],
             tools_for_api=[],turn_id='turn',api_request_id='request',approx_tokens=100)
    helper.prepare_routed_request(agent,state)
    assert prepared==[('new-provider','new-model')] and state.api_messages[0]['content']=='fresh context'
    helper.prepare_routed_request(agent,state)
    assert len(prepared)==1 and len(calls)==2
    state.api_messages=[{'role':'user','content':'x'*900000}]
    with pytest.raises(helper.ModelRoutingError,match='window'):
        helper.prepare_routed_request(agent,state)


def test_actual_retry_loop_stops_local_routing_failure_without_provider(tmp_path,monkeypatch,gate):
    import ast
    helper,agent,results,_,_=gate
    installer=load('required_loop_installer','integrations/hermes/patches/required-routing.py')
    for rel in ('agent/turn_api_request.py','agent/conversation_loop.py','hermes_cli/plugins.py'):
        p=tmp_path/rel;p.parent.mkdir(exist_ok=True,parents=True);p.write_text((HOST/rel).read_text())
    installer.apply(tmp_path)
    parsed=ast.parse((tmp_path/'agent/conversation_loop.py').read_text())
    node=next(n for n in parsed.body if isinstance(n,ast.FunctionDef) and n.name=='_run_api_retry_loop')
    namespace={'Optional':__import__('typing').Optional,'Dict':dict,'Any':object,'_LoopState':object,
               'nous_rate_limit_guard':object(),'handle_api_error':object()}
    phases=[]
    def run_phase(fn,*a,**kw):phases.append(fn);return NS(action='fallthrough')
    namespace['_run_phase']=run_phase
    exec(compile(ast.Module(body=[node],type_ignores=[]),'staged_conversation_loop','exec'),namespace)
    monkeypatch.setitem(sys.modules,'agent.agent_model_routing',helper)
    results.clear()
    state=NS(retry_count=0,max_retries=3,original_user_message='task',api_messages=[],tools_for_api=[],
             turn_id='turn',api_request_id='request',approx_tokens=0,local_execution_failure=None)
    namespace['_run_api_retry_loop'](agent,state)
    assert state.local_execution_failure.reason=='model_route_unavailable'
    assert phases==[namespace['nous_rate_limit_guard']]
