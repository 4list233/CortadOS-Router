# PURPOSE: Validate the explicit synthetic core policy before runtime effects.
# CONTRACT: RT-COWORK-MCP-008 v4, R002–004 and section 4 construction.
# DEPENDS: remote_schemas closed records; existing validated base configuration.
# CEILING: No root creation, database access, native preflight or policy discovery.
import hashlib
import os
from pathlib import Path
import stat

from .remote_schemas import CoreConfig, PolicySnapshot, RemoteError, json_bytes, plain, strict_json


def load_core_config(raw, base):
    try:
        value = strict_json(raw)
        key = value.get('ticket_key')
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise RemoteError('invalid_request')
        value['ticket_key'] = bytes.fromhex(key)
        config = CoreConfig(**value)
        limits = config.limits
        # Byte/count/time dimensions are policy operands, not deployment defaults.
        # The 512-byte floor fits a complete refusal envelope; the upper bound
        # is the representable unsigned four-byte frame-header length.
        if not (limits.file_bytes <= limits.task_bytes <= limits.installation_bytes
                and 512 <= limits.control_bytes <= 4294967295
                and limits.chunk_bytes <= limits.control_bytes
                and limits.artifacts_per_task <= 100
                and max(limits.uploads_per_task, limits.downloads_per_task) <= limits.streams_per_installation
                and max(limits.transfer_idle_ms, limits.ipc_idle_ms) <= limits.transfer_total_ms):
            raise RemoteError('invalid_request')
        principal = config.principal
        if (principal.issuer != 'urn:cortados:synthetic'
                or not principal.subject.startswith('synthetic:')
                or principal.installation_id != config.installation_id
                or config.transfer_origin != 'https://cowork.invalid'):
            raise RemoteError('invalid_request')
        roots = list(plain(config.roots).values())
        devices = set()
        for spelling in roots:
            path = Path(spelling)
            if not path.is_absolute() or str(path.resolve()) != spelling:
                raise RemoteError('invalid_request')
            for ancestor in (path, *path.parents):
                if ancestor.is_symlink():
                    raise RemoteError('invalid_request')
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
                raise RemoteError('invalid_request')
            devices.add(info.st_dev)
        if (len(set(roots)) != 4 or len(devices) != 1
                or len({str(Path(root).parent) for root in roots}) != 1
                or config.roots.state_dir != base['state_dir']):
            raise RemoteError('invalid_request')
        return config
    except (RemoteError, OSError, ValueError, TypeError, AttributeError) as exc:
        raise RemoteError('policy_unavailable', 'Local core configuration is unavailable') from exc


def policy_snapshot(config):
    value = plain(config)
    value.pop('mode')
    key = value.pop('ticket_key')
    value.update(schema='cortados.cowork-snapshot.v1', ticket_key_sha256=hashlib.sha256(key).hexdigest())
    value['config_digest'] = hashlib.sha256(json_bytes(value)).hexdigest()
    return PolicySnapshot(**value)
