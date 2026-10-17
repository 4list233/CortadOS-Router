# PURPOSE: Run exact staged adapters with a relocated, origin-checked fake installation.
# CONTRACT: RT-FRESH-QUOTA-005 stage exception permits three literal path substitutions only.
# DEPENDS: Frozen staged source bytes and immutable dependency fixture.
# CEILING: Private copies and fake transports; never edits activation or live files.
import ast
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch

STAGE = Path(os.environ.get('CORTADOS_FRESH_QUOTA_STAGE',
             '/Users/velox/.cortados-builds/fresh-quota-activation')).resolve()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def relocate(source, target, constants):
    tree = ast.parse(source.read_text())
    lines = source.read_text().splitlines(True)
    replacements = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for name in targets:
                if isinstance(name, ast.Name) and name.id in constants:
                    value = repr(str(constants[name.id]))
                    if isinstance(node.value, ast.Call) and ast.unparse(node.value.func).endswith('Path'):
                        value = ast.unparse(node.value.func) + '(' + value + ')'
                    replacements.append((node.lineno - 1, node.end_lineno, f'{name.id} = {value}\n'))
    for begin, end, replacement in reversed(replacements):
        lines[begin:end] = [replacement]
    target.write_text(''.join(lines))


class StageCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fresh-quota-stage-exam-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.install = self.root / 'installed-site'
        self.config_path = self.root / 'config.json'
        self.calls_path = self.root / 'quota-calls.jsonl'
        self.quota = {'status': 'available', 'kind': 'subscription', 'lane': 'codex', 'pool': 'all',
                      'observed_at': 2000000000, 'age_s': 0, 'source': 'fixture', 'reason': 'known fixture'}
        self.write_quota()
        for directory in ('cortados_router', 'integrations', 'integrations/mcp'):
            path = self.install / directory
            path.mkdir(parents=True, exist_ok=True)
            (path / '__init__.py').write_text('')
        (self.install / 'integrations/mcp/config.py').write_text(
            'import json\nfrom pathlib import Path\ndef load_config(path):\n return json.loads(Path(path).read_text())\n')
        (self.install / 'cortados_router/quota_admission.py').write_text("""
import json
_SCOPE = {'kind':'cli_store','lane':'codex','scope_id':'a'*64}
def _record(config, stage, provider, model, scope):
 with open(config['calls'], 'a') as target:
  target.write(json.dumps([stage, provider, model, scope])+'\\n')
def check(provider, model, *, auth_scope, config, observations=None):
 _record(config, 'dispatch', provider, model, auth_scope)
 return dict(config['quota'])
def source_preflight(provider, model, *, config):
 _record(config, 'source_preflight', provider, model, _SCOPE)
 return {'quota_stage':'source_preflight','auth_scope':dict(_SCOPE),'quota':dict(config['quota'])}
def auth_scope(provider, *, environment, executable, config, argv=()):
 return dict(_SCOPE)
def current_resources(candidates, *, config):
 return None
""")
        for directory in ('plugins/cortados-router', 'plugins/cortados-pty', 'plugins/model-providers/cortados-cli', 'context', 'router'):
            shutil.copytree(STAGE / directory, self.root / directory)
        constants = {'RELEASE_SITE_PACKAGES': self.install, 'RELEASE_PYTHON': sys.executable,
                     'CONFIG_PATH': self.config_path,
                     'PROVIDER_PLUGIN_SOURCE': self.root / 'plugins/model-providers/cortados-cli/__init__.py'}
        for relative in ('plugins/cortados-router/quota_policy.py', 'plugins/cortados-pty/model_routing.py'):
            source = STAGE / relative
            if source.exists():
                relocate(source, self.root / relative, constants)
        # Preserve existing module objects across each independent fixture installation.
        self.modules = dict(sys.modules)
        self.paths = list(sys.path)
        self.addCleanup(self.restore_imports)
        for name in list(sys.modules):
            if name.split('.')[0] in ('cortados_router', 'integrations', 'context'):
                del sys.modules[name]
        sys.path.insert(0, str(self.root))
        sys.path.insert(0, str(self.install))
        self.enterContext(patch.dict(os.environ, {'HOME': str(self.root), 'CORTADOS_HOME': str(self.root), 'CORTADOS_HERMES_HOME': str(self.root),
                                                'CORTADOS_REPO': str(self.root), 'HERMES_HOME': str(self.root)}))
        config = ModuleType('hermes_cli.config')
        config.load_config_readonly = lambda: {'agent': {'model_router': self.router_mode}}
        constants_mod = ModuleType('hermes_constants')
        constants_mod.get_hermes_home = lambda: self.root
        host = ModuleType('hermes_cli')
        host.config = config
        local = ModuleType('hermes_cli.local_execution')
        local.LocalExecutionFailure = lambda **kwargs: NS(**kwargs)
        agent = ModuleType('agent')
        routing = ModuleType('agent.agent_model_routing')
        routing.admitted = lambda receipt: nullcontext()
        routing.validate_request = lambda *args: None
        routing.ModelRoutingError = ValueError
        routing.current_receipt = lambda: self.receipt
        self.receipt = {'provider': 'codex-cli', 'model': 'gpt-6.1-sol', 'context_length': 100000,
                        'api_request_id': 'exam-request', 'router': 'cortados-router'}
        self.router_mode = None
        sys.modules.update({'hermes_cli': host, 'hermes_cli.config': config,
                            'hermes_constants': constants_mod, 'hermes_cli.local_execution': local,
                            'agent': agent, 'agent.agent_model_routing': routing})

    def restore_imports(self):
        for name in list(sys.modules):
            if name not in self.modules:
                del sys.modules[name]
        sys.modules.update(self.modules)
        sys.path[:] = self.paths

    def write_quota(self, status=None):
        if status:
            self.quota.update(status=status, reason='fixture ' + status)
        self.config_path.write_text(json.dumps({'quota': self.quota, 'calls': str(self.calls_path)}))

    def calls(self):
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()] if self.calls_path.exists() else []

    def provider_plugin(self):
        """Load the complete frozen provider source with inert import dependencies."""
        providers = ModuleType('providers')
        registry = {}
        providers.register_provider = lambda profile: registry.__setitem__(profile.name, profile)
        providers.get_provider_profile = lambda name: registry.get(name)
        base = ModuleType('providers.base')
        class ProviderProfile:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
        base.ProviderProfile = ProviderProfile
        bridge = ModuleType('agent.acp_openai_bridge')
        for name in ('completion_to_stream_chunks', 'extract_tool_calls_from_text', 'tool_specs_from_openai_tools'):
            setattr(bridge, name, lambda *args, **kwargs: [])
        sys.modules.update({'providers': providers, 'providers.base': base, 'agent.acp_openai_bridge': bridge})
        pty = self.root / 'plugins/cortados-pty'
        sys.path.insert(0, str(pty))
        load('model_routing', pty / 'model_routing.py')
        load('usage', pty / 'usage.py')
        load('runner', pty / 'runner.py')
        plugin = load('fresh_quota_fixture_cli_provider', self.root / 'plugins/model-providers/cortados-cli/__init__.py')
        return plugin, registry
