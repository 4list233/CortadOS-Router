#!/usr/bin/env python3
# CortadOS Office (#77): build Claw3D Studio's gateway settings file for deploy-air.sh.
#
#   sudo cat <bridge-token> | python3 -I studio-settings.py --url ws://localhost:18789 [--existing FILE] > settings.json
#
# Why a file: Claw3D 51337f7's gateway proxy takes its upstream ONLY from
# loadUpstreamGatewaySettings (server/studio-settings.js lines 85-112): <stateDir>/claw3d/settings.json
# -> {gateway: {url, token, adapterType}}, then openclaw.json, then ws://localhost:18789 with no token.
# It never reads CLAW3D_GATEWAY_URL / CLAW3D_GATEWAY_TOKEN, so without this file every connect is
# refused with studio.gateway_token_missing (server/gateway-proxy.js lines 230-243).
#
# The bridge token (the one the bridge checks, CORTADOS_OFFICE_BRIDGE_TOKEN_FILE) comes on stdin only:
# never argv, never printed; errors name the problem, not the value. --existing is a copy of the
# installed file: its other keys (Studio's own UI state: avatars, floors, desks...) are kept and only
# `gateway` is replaced, so an upgrade rewrites the connection without wiping the owner's office.
# An existing file that is not a JSON object is refused (exit 2) rather than overwritten blind.
# Stdlib only. Exit 0 with the JSON on stdout; 1 bad token or URL; 2 bad existing file.
import argparse
import json
import re
import sys
from urllib.parse import urlsplit

TOKEN_RE = re.compile(r"[A-Za-z0-9._~-]{16,4096}")   # openssl rand -hex 32 in practice


def build(token, url, existing_text=""):
    """The settings object; ValueError (token/URL) or TypeError (existing file) with a value-free reason."""
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("the bridge token on stdin is empty or malformed")
    parts = urlsplit(url)
    if parts.scheme != "ws" or not parts.hostname or not parts.port:
        raise ValueError("the gateway URL must be ws://<host>:<port>")
    settings = {}
    if existing_text.strip():
        try:
            settings = json.loads(existing_text)
        except ValueError:
            raise TypeError("the existing settings.json is not valid JSON") from None
        if not isinstance(settings, dict):
            raise TypeError("the existing settings.json is not a JSON object")
    settings.setdefault("version", 1)
    # The whole gateway entry is ours: no stale profiles or lastKnownGood pointing elsewhere.
    settings["gateway"] = {"url": url, "token": token, "adapterType": "openclaw"}
    return settings


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--existing", default="")
    a = p.parse_args(argv)
    token = sys.stdin.read().strip()
    existing = ""
    if a.existing:
        with open(a.existing, encoding="utf-8") as f:
            existing = f.read()
    try:
        settings = build(token, a.url, existing)
    except ValueError as e:
        print(f"studio-settings: {e}", file=sys.stderr)
        return 1
    except TypeError as e:
        print(f"studio-settings: {e}: move it aside deliberately first", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(settings, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
