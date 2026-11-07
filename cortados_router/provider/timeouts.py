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


# The ONE place both budgets come from. CALL_TIMEOUT_S is the shim's per-call deadline (handed to the shim verbatim as
# CORTADOS_PROVIDER_CALL_TIMEOUT_S); CLI_TOOL_TIMEOUT_S is the CLI's MCP_TOOL_TIMEOUT. The CLI must never reach its own
# timeout first: that timeout makes the CLI write a synthetic tool error and continue a paid turn unattended. The
# margin between them covers (1) the CLI starting its timer before the shim receives the call (process startup and
# scheduling), and (2) a result write that passed the shim's pre-write check just before the deadline: that is a real
# result and must finish flushing well before the CLI could time out. A pipe write of one JSON line takes
# milliseconds; 30 s is the documented floor, the configured margin is ABORT_TIMEOUT_S + CLI_SLACK_S = 240 s.
CLI_MARGIN_FLOOR_S = 30.0
CLI_MARGIN_S = ABORT_TIMEOUT_S + CLI_SLACK_S
assert CLI_MARGIN_S >= CLI_MARGIN_FLOOR_S, "the CLI timeout margin fell below its documented floor"
CALL_TIMEOUT_S = TURN_TIMEOUT_S + effective_tool_wait(IDLE_S, TOOL_WAIT_S) + CALL_SLACK_S
CLI_TOOL_TIMEOUT_S = CALL_TIMEOUT_S + CLI_MARGIN_S
