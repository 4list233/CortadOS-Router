# PURPOSE: Own captured native profiles and historical attempt provenance.
# CONTRACT: RT-MCP-NATIVE-002; unknown versions refuse and admitted versions never drift.
# DEPENDS: Closed codex environment, operator startup deadline, typed WorkerError.
# CEILING: Observe version only; never execute tasks, refresh login, or migrate records.
import subprocess
import time

from .schemas import WorkerError


# These are captured protocol identities, not version ranges or tunable limits.
CAPTURED_VERSIONS = frozenset(("0.162.1", "0.163.0"))
# Only this captured profile could produce schema-1 records before provenance existed.
HISTORICAL_VERSION = "0.162.1"


def captured_version(value):
    """Require one exact captured identity at every attempt provenance boundary."""
    if not isinstance(value, str) or value not in CAPTURED_VERSIONS:
        raise WorkerError("native_version_mismatch", "Native CLI profile is not captured")
    return value


def record_version(record):
    """Missing historical fields have one default; malformed present fields refuse."""
    return captured_version(record.get("cli_version", HISTORICAL_VERSION))


def check_version(config, deadline, *, expected=None):
    """Probe within the caller's startup budget and optionally bind one attempt."""
    from .codex import native_environment
    if expected is not None:
        captured_version(expected)
    try:
        result = subprocess.run([config["codex_bin"], "--version"], shell=False,
                                cwd=config["state_dir"], env=native_environment(config),
                                capture_output=True, timeout=max(0, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerError("native_version_mismatch", "Native CLI version could not be checked") from exc
    # Native emits a single version line. Permit its line terminator, never extra
    # whitespace, additional lines, suffixes, or a merely similar version prefix.
    output = result.stdout.removesuffix(b"\n").removesuffix(b"\r")
    version = next((value for value in CAPTURED_VERSIONS
                    if output == ("codex-cli " + value).encode()), None)
    if result.returncode != 0 or version is None or (expected is not None and version != expected):
        raise WorkerError("native_version_mismatch", "Native CLI does not match the captured attempt profile")
    return version
