"""Where the S1 scorer listens, and where its clients reach it. One resolver, config-driven, with no device named in code.

A CortadOS session can route through an S1 that lives on any machine on the tailnet: the Mac Studio today, a second
Studio, a laptop, whatever is serving. None of that belongs in code, so this module reads it from the environment and
from an endpoints file that maps plain names to URLs. Adding or moving a device is one line in that file; this module
never hardcodes an address.

Client resolution order for the scorer URL (first that yields a value wins):
  1. CORTADOS_S1_URL         -- an explicit full URL. Always wins. The long-standing single-machine override; untouched.
  2. CORTADOS_S1_ENDPOINT    -- a name looked up in the endpoints file (errors if the file or the name is missing, so a
                                typo surfaces instead of silently hitting the wrong machine).
  3. the endpoints file's "default" name, when it sets one.
  4. http://127.0.0.1:8431   -- loopback. A machine that serves its own S1 needs no configuration at all.

The endpoints file is $CORTADOS_S1_ENDPOINTS, else <tables>/s1-endpoints.json. Its shape (see s1-endpoints.example.json):
  {"default": "studio", "endpoints": {"studio": "http://100.x.y.z:8431", "local": "http://127.0.0.1:8431"}}
The real file is machine-local and git-ignored, because a tailnet address is not something this public repo should ship.

Server bind (s1/serve.py):
  CORTADOS_S1_BIND  -- the interface to listen on. Default 127.0.0.1, so the port is never exposed to the network unless
                       an operator deliberately sets this (e.g. the Studio's tailnet IP, or 0.0.0.0). No silent exposure.
  CORTADOS_S1_PORT  -- the port, default 8431. Shared with the loopback client default above.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cortados_router.compat import getenv

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8431
DEFAULT_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
_SCHEMES = ("http://", "https://")


def endpoints_path(*, environ=None) -> Path:
    """The endpoints file: $CORTADOS_S1_ENDPOINTS, else <tables>/s1-endpoints.json."""
    override = getenv("CORTADOS_S1_ENDPOINTS", environ=environ)
    if override:
        return Path(override)
    from cortados_router.paths import tables_dir
    return tables_dir() / "s1-endpoints.json"


def _normalize(url: str, *, source: str) -> str:
    """A scorer URL with a scheme and no trailing slash, or a clear error naming where the bad value came from."""
    url = str(url).strip()
    if not url.startswith(_SCHEMES):
        raise ValueError(f"S1 endpoint from {source} is not an http(s) URL: {url!r}")
    return url.rstrip("/")


def load_endpoints(*, environ=None) -> dict[str, Any]:
    """Parse the endpoints file. {} when it does not exist (loopback is then the default); a clear error when it exists
    but is malformed -- a broken config must not silently fall back to the wrong machine."""
    path = endpoints_path(environ=environ)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"S1 endpoints file {path} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("endpoints", {}), dict):
        raise ValueError(f"S1 endpoints file {path} must be an object with an 'endpoints' object")
    return data


def resolve_url(*, environ=None) -> str:
    """The scorer URL this session should call, by the order documented at the top of this module."""
    explicit = getenv("CORTADOS_S1_URL", environ=environ)
    if explicit:
        return _normalize(explicit, source="CORTADOS_S1_URL")

    data = load_endpoints(environ=environ)
    table = data.get("endpoints", {})
    chosen = getenv("CORTADOS_S1_ENDPOINT", environ=environ)
    source = "CORTADOS_S1_ENDPOINT"
    if not chosen:
        chosen = data.get("default")
        source = f"the 'default' in {endpoints_path(environ=environ)}"
    if not chosen:
        return DEFAULT_URL                                   # no device selected: serve/reach S1 on loopback

    if chosen not in table:
        known = ", ".join(sorted(table)) or "(none)"
        raise ValueError(f"S1 endpoint {chosen!r} (from {source}) is not in {endpoints_path(environ=environ)}; known: {known}")
    return _normalize(table[chosen], source=f"endpoint {chosen!r}")


def bind_address(*, environ=None) -> tuple[str, int]:
    """(host, port) for s1/serve.py to listen on. Loopback unless CORTADOS_S1_BIND deliberately widens it."""
    host = getenv("CORTADOS_S1_BIND", environ=environ) or DEFAULT_HOST
    port = int(getenv("CORTADOS_S1_PORT", environ=environ) or DEFAULT_PORT)
    return host, port
