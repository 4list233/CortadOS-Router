import importlib.util
from pathlib import Path
from unittest import TestCase,mock
spec=importlib.util.spec_from_file_location('terra_router',Path(__file__).resolve().parents[1]/'cortados_router/core.py')
core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)

class TerraFallback(TestCase):
    def resolve(self,**kw):
        with mock.patch.object(core,'lane_health',return_value={}), mock.patch.object(core,'scored_candidates',return_value=None):
            return core.resolve({'degraded':True,'answers':None},input_fit={'known':False,'input_tokens':1000},**kw)
    def test_degraded_selects_terra(self):
        self.assertEqual('gpt-5.6-terra',self.resolve()['chosen']['model'])
    def test_allowlist_cannot_leak_to_claude(self):
        self.assertIsNone(self.resolve(allowed_routes=(('claude-cli','claude-sonnet-5'),))['chosen'])
    def test_quota_unavailable_does_not_force_model(self):
        with mock.patch.object(core,'candidate_health',return_value={'ok':False,'why':'quota'}):
            self.assertIsNone(self.resolve()['chosen'])
    def test_context_limit_is_not_bypassed(self):
        with mock.patch.object(core,'lane_ceiling',return_value=100):
            self.assertIsNone(self.resolve()['chosen'])
