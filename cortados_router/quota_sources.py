# PURPOSE: Capture proven native source generations and interpret read-only quota facts.
# CONTRACT: RT-QUOTA-SOURCES-006; no admission, cache or credential refresh.
# DEPENDS: Captured Cursor owners, existing scarcity arithmetic and collector lifecycle.
import hashlib
import json
import math
import os
from pathlib import Path
import re
from dataclasses import dataclass
import time
from integrations.mcp.schemas import WorkerError

PROFILE = 'cursor-2026.10.16-e3cb207-file-v1'
CODEX_PROFILE = 'codex-cli-store-v005'
NATIVE = Path('/Users/velox/.local/share/cursor-agent/versions/2026.10.16-e3cb207')
MANIFEST = {'node_modules/node-gyp-build/index.js': 'a7ed0d5ae218a19bdbdf15a590d0893790ddf536313b66a787554693cfaae078', 'node_modules/node-gyp-build/node-gyp-build.js': '134f0585f7c665db89f332a379158c6f113274422e42aaf54e0aa9d5ac37f577', '7956.index.js': '67c5b06ab772041f694b72e358671fa870a238dca6b8e1e5ca2a49cd6060172e', 'node_modules/tree-sitter-bash/bindings/node/index.js': '609bdf22b8fd183025b3c24e8587f6211d50358e8f2e4c526b9217349ed9dbc0', 'node_modules/tree-sitter-bash/build/Release/tree_sitter_bash_binding.node': '852011b968215769e3e82400a71a96c70ac348c69d6ef909983f99511c1f9b15', 'node_modules/tree-sitter/build/Release/tree_sitter_runtime_binding.node': 'b838a262071563b569319dd5fa8a2a653dfbb016e1d74381308aec3e29166197', 'node_modules/tree-sitter-bash/src/node-types.json': '39763582e682b132eb5349af6e1d7be78aed98decf808ffa6bcf92cfed4ac5dc', 'cursor-agent': '2ccc9a8e167797641448b5e5c936f006ba137a2555f117f38c5eb76a5238a233', 'node_modules/tree-sitter/package.json': '8cca5044dd3381dbf958dea36ef6d9f81f379886d32ef845919cf07679c6e42c', 'node_modules/tree-sitter-bash/package.json': 'ee9d6515031266905123792f891fc9c3db75f55c5d9c1e027a315a60a09557c4', 'node_modules/tree-sitter/index.js': 'ba5e8461e5068f2b58ea860aea331ed0c6d8a25f21142d75a6145a1544fc9ffe', 'node': 'ebd2d552c7bebde593dd0390530963ad28de56bccde6ce387cdbe55fb0b6fb8e', 'index.js': 'd0e2097119ed8ef3509c6e9e4c629809f2b7345d81de33bf591875b4b6e4722c'}

@dataclass(frozen=True, kw_only=True, repr=False)
class SourceCapture:
    profile: str
    lane: str
    source_argv: tuple
    material: bytes
    files: tuple

    def __repr__(self):
        return '<SourceCapture redacted>'

    def __reduce_ex__(self, protocol):
        raise TypeError('Private source cannot be serialized')


@dataclass(frozen=True, kw_only=True, repr=False)
class SourceRead:
    record: dict
    valid_until_epoch: object

    def __repr__(self):
        return '<SourceRead redacted>'

    def __reduce_ex__(self, protocol):
        raise TypeError('Private source cannot be serialized')


def _failure():
    return WorkerError('quota_unknown', 'Native quota data is invalid')


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _milliseconds(value):
    if isinstance(value, str) and re.fullmatch(r'-?[0-9]+', value):
        value = int(value)
    if not _finite(value) or int(value) != value:
        raise _failure()
    return value


def cursor_record(payload, *, observed_at):
    from . import scarcity
    try:
        start, end = (_milliseconds(payload[k]) for k in ('billingCycleStart', 'billingCycleEnd'))
        if not _finite(observed_at) or not start <= observed_at * 1000 < end or end <= start:
            raise _failure()
        windows = []
        fields = (('totalPercentUsed', 'month (included, both pools)', 'info'),
                  ('autoPercentUsed', 'month (Cursor models: Grok, Composer, Auto)', 'cursor-models'),
                  ('apiPercentUsed', 'month (other models: Claude, GPT, Gemini …)', 'other-models'))
        for field, name, pool in fields:
            used = payload['planUsage'][field]
            if not _finite(used) or not 0 <= used <= 100:
                raise _failure()
            window = scarcity.window_view(name, used, end / 1000, (end - start) / 1000, now=observed_at)
            windows.append({**window, 'used_pct': used, 'headroom_pct': 100 - used, 'pool': pool})
        return scarcity.finish('cursor', {'source': 'cursor native getCurrentPeriodUsage', 'windows': windows})
    except (KeyError, TypeError, ValueError, OverflowError):
        raise _failure() from None


def _absolute(value):
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ValueError('Source capture unavailable')
    return Path(value).resolve()


def _file(path, *, absent=False):
    path = Path(path)
    resolved = path.resolve()
    if absent and not path.exists():
        return (str(resolved), None, None, None, None, None, None), b''
    before = resolved.stat()
    if not resolved.is_file():
        raise ValueError('Source capture unavailable')
    content = resolved.read_bytes()
    after = path.resolve().stat()
    fields = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if path.resolve() != resolved or fields(before) != fields(after):
        raise ValueError('Source capture unavailable')
    return (str(resolved), *fields(after), hashlib.sha256(content).hexdigest()), content


def _argv(argv, executable):
    if not isinstance(argv, (tuple, list)) or not argv or str(_absolute(argv[0])) != str(executable):
        raise ValueError('Source capture unavailable')
    seen, index, prompt = {}, 1, False
    operands = {'--output-format', '--model', '--resume', '--mode'}
    flags = {'-p', '--trust', '--force'}
    while index < len(argv):
        item = argv[index]
        if item in seen or not isinstance(item, str):
            raise ValueError('Source capture unavailable')
        if item in operands:
            if index + 1 >= len(argv) or not isinstance(argv[index + 1], str) or not argv[index + 1] or argv[index + 1].startswith('-'):
                raise ValueError('Source capture unavailable')
            seen[item] = argv[index + 1]
            index += 2
        elif item in flags:
            seen[item] = True
            index += 1
        elif not item.startswith('-') and index == len(argv) - 1 and not prompt:
            prompt = True
            index += 1
        else:
            raise ValueError('Source capture unavailable')
    if not all(k in seen for k in ('-p', '--trust', '--model')) or seen.get('--output-format') != 'stream-json' or seen.get('--mode', 'ask') != 'ask':
        raise ValueError('Source capture unavailable')
    return ('--model', seen['--model'])


def capture_source(provider, *, environment, executable, argv, cwd, config):
    from . import quota_admission as policy
    try:
        if provider != 'cursor-cli' or not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()):
            raise ValueError('Source capture unavailable')
        binary, folder = _absolute(executable), _absolute(cwd)
        if binary != NATIVE / 'cursor-agent' or not folder.is_dir() or environment.get('AGENT_CLI_CREDENTIAL_STORE') != 'file':
            raise ValueError('Source capture unavailable')
        for key, value in environment.items():
            if value and (key in ('NODE_OPTIONS', 'NODE_PATH', 'BASH_ENV', 'ENV') or key.startswith('DYLD_') or
                          key.startswith('AGENT_CLI_') and key != 'AGENT_CLI_CREDENTIAL_STORE' or
                          key.startswith('CURSOR_') and key not in ('CURSOR_CONFIG_DIR', 'CURSOR_DATA_DIR')):
                raise ValueError('Source capture unavailable')
        source_argv = _argv(argv, binary)
        home = _absolute(environment.get('HOME'))
        settings = _absolute(environment.get('CURSOR_CONFIG_DIR') or str(_absolute(environment.get('XDG_CONFIG_HOME') or str(home / '.config')) / 'cursor')) if environment.get('CURSOR_CONFIG_DIR') or environment.get('XDG_CONFIG_HOME') else home / '.cursor'
        data = _absolute(environment.get('CURSOR_DATA_DIR') or str(home / '.cursor'))
        files, contents = [], []
        for relative, expected in MANIFEST.items():
            descriptor, content = _file(NATIVE / relative)
            if descriptor[-1] != expected:
                raise ValueError('Source capture unavailable')
            files.append(descriptor)
        for path in (home / '.cursor/auth.json', settings / 'cli-config.json'):
            descriptor, content = _file(path)
            files.append(descriptor)
            contents.append(content.hex())
        for ancestor in (folder, *folder.parents):
            for name in ('.cursor/cli.json', '.envrc'):
                path = ancestor / name
                descriptor, content = _file(path, absent=True)
                if descriptor[1] is not None and (name != '.envrc' or environment.get('DIRENV_DISABLE') != '1'):
                    raise ValueError('Source capture unavailable')
                files.append(descriptor)
                contents.append(content.hex())
        material = json.dumps([PROFILE, environment, str(binary), str(folder), str(home), str(settings), str(data), source_argv, files, contents], sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        return SourceCapture(profile=PROFILE, lane='cursor', source_argv=source_argv, material=material, files=tuple(files))
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('Source capture unavailable') from None


def read_source(profile, *, environment, executable, source_argv, cwd, config):
    from . import quota_observations
    if profile != PROFILE or tuple(source_argv[:1]) != ('--model',) or len(source_argv) != 2:
        raise _failure()
    argv = (executable, '-p', '--output-format', 'stream-json', '--trust', '--model', source_argv[1])
    before = capture_source('cursor-cli', environment=environment, executable=executable, argv=argv, cwd=cwd, config=config)
    request = {'observed_at': time.time(), 'manifest': MANIFEST}
    value = quota_observations._run([str(NATIVE / 'node'), str(Path(__file__).with_name('cursor_quota.cjs'))],
            config=config, request=request, environment=environment, cwd=cwd, parser=lambda raw: json.loads(raw.decode()))
    after = capture_source('cursor-cli', environment=environment, executable=executable, argv=argv, cwd=cwd, config=config)
    if before != after or set(value) != {'payload', 'valid_until_epoch'} or not _finite(value['valid_until_epoch']):
        raise _failure()
    return SourceRead(record=cursor_record(value['payload'], observed_at=request['observed_at']), valid_until_epoch=value['valid_until_epoch'])
