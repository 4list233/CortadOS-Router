# PURPOSE: Bind private runtime state before importing any Router owner.
# CONTRACT: RT-MCP-001 public python -m integrations.mcp --config entrypoint.
# DEPENDS: Strict config, detached broker lifecycle, official MCP stdio server.
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from .config import config_bytes, load_config
from .schemas import WorkerError, emit_event


def main():
    parser = argparse.ArgumentParser(description='Codex-first routed MCP worker')
    parser.add_argument('--config', required=True)
    parser.add_argument('--broker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = {}
    try:
        raw_config = config_bytes(args.config)
        config = load_config(args.config, raw=raw_config)
        digest = hashlib.sha256(raw_config).hexdigest()
        Path(config['state_dir']).mkdir(mode=0o700, parents=True, exist_ok=True)
        os.environ['CORTADOS_HOME'] = str(Path(config['state_dir']) / 'router')
        # Permissions on evidence and local channels are private by construction.
        os.umask(0o077)
        if args.broker:
            from .broker import serve
            asyncio.run(serve(config, digest))
        else:
            from .broker import ensure_broker
            from .server import serve
            ensure_broker(config, args.config, digest)
            asyncio.run(serve(config, digest))
        return 0
    except WorkerError as exc:
        if args.broker and config:
            # The starting frontend owns startup refusal emission; this private
            # handback is diagnostic data, never a second structured event.
            path = Path(config['state_dir']) / 'startup-error.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'code': exc.code, 'message': exc.message}))
            temporary.replace(path)
        elif not getattr(exc, 'emitted', False):
            emit_event(config, 'refusal', reason=exc.code)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
