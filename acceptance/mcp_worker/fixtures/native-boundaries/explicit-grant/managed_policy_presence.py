"""Observe native macOS managed policy presence without reading its values."""
import ctypes
import json
from pathlib import Path

root = Path(__file__).resolve().parent
cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
cf.CFStringCreateWithCString.restype = ctypes.c_void_p
cf.CFPreferencesAppValueIsForced.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
cf.CFPreferencesAppValueIsForced.restype = ctypes.c_bool
cf.CFRelease.argtypes = [ctypes.c_void_p]


def forced(key):
    domain = cf.CFStringCreateWithCString(None, b'com.openai.codex', 0x08000100)
    key_obj = cf.CFStringCreateWithCString(None, key.encode(), 0x08000100)
    try:
        return bool(cf.CFPreferencesAppValueIsForced(key_obj, domain))
    finally:
        cf.CFRelease(key_obj)
        cf.CFRelease(domain)


result = {
    'domain': 'com.openai.codex',
    'method': 'CFPreferencesAppValueIsForced only; no preference values read',
    'forced_keys': {k: forced(k) for k in ['config_toml_base64', 'requirements_toml_base64']},
    'system_files': {p: Path(p).exists() for p in [
        '/etc/codex/config.toml', '/etc/codex/requirements.toml',
        '/etc/codex/managed_config.toml',
    ]},
    'cloud_policy': 'not established by these local checks',
}
(root / 'managed-policy-presence.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
