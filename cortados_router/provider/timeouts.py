"""Provider-owned timeout resolution; standalone shims receive these resolved values explicitly."""
from cortados_router.compat import getenv


def positive(name, default):
    value = float(getenv(name) or default)
    if not 0 < value < float("inf"):
        raise ValueError(f"{name} must be positive and finite")
    return value


IDLE_S = positive("CORTADOS_PROVIDER_IDLE_S", 1800)
TOOL_WAIT_S = positive("CORTADOS_PROVIDER_TOOL_WAIT_S", 10800)
TURN_TIMEOUT_S = positive("CORTADOS_PROVIDER_TURN_TIMEOUT_S", 1800)
CALL_SLACK_S = positive("CORTADOS_PROVIDER_CALL_SLACK_S", 120)
ABORT_TIMEOUT_S = 120.0
# The CLI starts its timer before the shim: cover process startup/scheduling as well as abort completion.
CLI_SLACK_S = 120.0


def effective_tool_wait(idle_s, tool_wait_s):
    return max(idle_s, tool_wait_s)


CALL_TIMEOUT_S = TURN_TIMEOUT_S + effective_tool_wait(IDLE_S, TOOL_WAIT_S) + CALL_SLACK_S
CLI_TOOL_TIMEOUT_S = CALL_TIMEOUT_S + ABORT_TIMEOUT_S + CLI_SLACK_S
