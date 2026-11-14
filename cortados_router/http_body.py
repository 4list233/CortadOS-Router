"""Reading an HTTP request body under an absolute deadline (stdlib only).

A socket timeout bounds inactivity, not the total read: a client that sends one byte just inside each timeout holds the
handler thread for as long as it likes. Every server here that reads a body from a client it does not yet trust reads
it through read_within, so the whole body must arrive by one deadline. Used by the bot gateway, the local gate and the
provider's dashboard login and local-switch POST (Handler._control_body). Its client-side counterpart, a whole outgoing
call under one deadline, is local_gate.deadline_call.
"""
from __future__ import annotations

import time

_CHUNK = 65536


def read_within(conn, rfile, n: int, deadline_s: float, idle_s: float, clock=time.monotonic) -> bytes | None:
    """n bytes from `rfile` within `deadline_s` seconds, or None when the deadline passes or the client goes away
    (stalls past the timeout, closes, or the socket fails). Each wait on `conn` is bounded by the smaller of `idle_s`
    and the time left, so a trickle cannot extend the deadline. The socket's timeout is put back to `idle_s` after."""
    end = clock() + deadline_s
    parts, left = [], n
    try:
        while left > 0:
            remaining = end - clock()
            if remaining <= 0:
                return None
            conn.settimeout(min(idle_s, remaining))
            chunk = rfile.read1(min(left, _CHUNK))
            if not chunk:
                return None
            parts.append(chunk)
            left -= len(chunk)
    except OSError:
        return None                                 # the client stalled or left; the caller closes the connection
    finally:
        try:
            conn.settimeout(idle_s)
        except OSError:
            pass
    return b"".join(parts)
