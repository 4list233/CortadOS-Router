"""Host boundary for the router: environment lookup and optional host modules.

Owner of two rules:
  getenv           the environment adapter for the Wangcai → Cortados rename. Same semantics as CortadOS
                   `context/env_compat.py`: reads are lazy (profile scopes and tests may change the environment
                   after import), an explicit new name always wins (including an empty value), legacy values are
                   never copied into os.environ, and a legacy name warns once per process (names only, no values).
  optional_module  imports a host module that the router can run without (Hermes, the CortadOS Context engine).
                   A missing module is never a silent skip: it is logged once and recorded in MISSING (module -> fallbacks
                   taken), which `missing_modules()` exposes for diagnostics and tests.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading

LEGACY_PREFIX = "WANGCAI_"
CURRENT_PREFIX = "CORTADOS_"
_warned: set[str] = set()
_lock = threading.Lock()
logger = logging.getLogger(__name__)


def new_name(name: str) -> str:
    if name == "WANGCAI_HERMES_HOME":
        return "CORTADOS_HOME"
    return CURRENT_PREFIX + name[len(LEGACY_PREFIX):] if name.startswith(LEGACY_PREFIX) else name


def _legacy(env, name):
    with _lock:
        if name not in _warned:
            _warned.add(name)
            logger.warning("%s is deprecated; use %s (legacy fallback, once per process)", name, new_name(name))
    return env[name]


def getenv(name: str, default=None, *, environ=None):
    env = os.environ if environ is None else environ
    name = new_name(name)
    if name in ("CORTADOS_HOME", "CORTADOS_HERMES_HOME"):
        for key in ("CORTADOS_HOME", "CORTADOS_HERMES_HOME"):
            if key in env:
                return env[key]
        for key in ("WANGCAI_HOME", "WANGCAI_HERMES_HOME"):
            if key in env:
                return _legacy(env, key)
    elif name in env:
        return env[name]
    elif name.startswith(CURRENT_PREFIX):
        old = LEGACY_PREFIX + name[len(CURRENT_PREFIX):]
        if old in env:
            return _legacy(env, old)
    return default


# ------------------------------------------------------------------ optional host modules
MISSING: dict[str, list[str]] = {}      # module name -> what the router does without it, one entry per fallback taken


def note_missing(module: str, consequence: str) -> None:
    """Record, once per (module, fallback) and process, that a host module is unavailable and what the router does instead."""
    with _lock:
        seen = MISSING.setdefault(module, [])
        if consequence in seen:
            return
        seen.append(consequence)
    logger.warning("cortados-router: host module %s unavailable; %s", module, consequence)


def optional_module(module: str, consequence: str):
    """Import `module`, or record its absence (see note_missing) and return None.

    Only a missing module is tolerated. An import that fails for any other reason (a bug inside the host
    module, or one of ITS dependencies missing) propagates, so a broken host is never mistaken for an absent one.
    """
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        missing = exc.name or module
        if module != missing and not module.startswith(missing + "."):
            raise
        note_missing(module, consequence)
        return None


def missing_modules() -> dict[str, list[str]]:
    with _lock:
        return {k: list(v) for k, v in MISSING.items()}
