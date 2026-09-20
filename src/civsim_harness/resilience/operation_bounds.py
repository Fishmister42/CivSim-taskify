"""Per-operation bound enforcement (T149).

research R12's third signal: every Nexus command, capture, and post-action
read-back carries its own timeout, catching a client that is alive and
still answers the heartbeat but will not service one specific operation --
the failure shape process liveness and the heartbeat both miss.

**Scoped to exactly one operation, never to a turn.** `run_bounded` takes a
single awaitable-producing call, times *that call alone*, and returns.
Nothing here keeps a running total across calls, and there is no notion of
"how long has this turn been going" anywhere in this module -- a turn may
legitimately issue thousands of individually-bounded operations over
several hours without ever approaching a fault (research R12, FR-014).

`OperationTimedOut` is a plain value, not an exception: a single stuck
operation is not by itself grounds to abort anything from this module's
point of view. It is `resilience/detector.py`'s job to decide that a
timed-out operation is the `unresponsive_detected` signal and to turn it
into a recorded event; this module only measures.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from civsim_harness.errors import NexusError
from civsim_harness.nexus.client import REASON_TIMEOUT


class OperationKind(StrEnum):
    """The three operation shapes research R12 names as needing their own bound."""

    NEXUS_COMMAND = "nexus_command"
    CAPTURE = "capture"
    POST_ACTION_READBACK = "post_action_readback"


#: Default per-kind bound, in seconds. Each one bounds a single operation of
#: that kind -- never a turn, never a sum across operations. Callers may
#: override per call via `run_bounded(..., bound_s=...)` (e.g. a capability
#: catalog entry that knows a particular command is normally slower).
#:
#: `NEXUS_COMMAND` matches `nexus.client.DEFAULT_COMMAND_TIMEOUT_S` so a
#: caller that does not care to think about it gets the same default the
#: client itself already uses; `CAPTURE` and `POST_ACTION_READBACK` get
#: their own defaults since they are not Nexus round-trips at all (capture
#: is host-layer, research R6) or may legitimately take longer than a
#: cheap command (a read-back after an action that changes a lot of state).
DEFAULT_OPERATION_BOUNDS_S: dict[OperationKind, float] = {
    OperationKind.NEXUS_COMMAND: 30.0,
    OperationKind.CAPTURE: 15.0,
    OperationKind.POST_ACTION_READBACK: 30.0,
}


@dataclass(frozen=True)
class OperationTimedOut:
    """*kind* did not complete within *bound_s* on this one attempt.

    Not raised -- returned, so a caller can tell "this operation is what
    timed out" apart from any other exception the operation itself might
    raise, without this module needing its own exception subclass (the
    harness's exception hierarchy already names what recovery needs;
    see `civsim_harness.errors`).
    """

    kind: OperationKind
    bound_s: float


async def run_bounded[T](
    operation: Callable[[], Awaitable[T]],
    *,
    kind: OperationKind,
    bound_s: float | None = None,
) -> T | OperationTimedOut:
    """Run *operation* (a zero-argument coroutine factory) under its own bound.

    Returns the operation's result on success, or an `OperationTimedOut`
    marker if it did not complete within *bound_s* (defaulting to *kind*'s
    entry in `DEFAULT_OPERATION_BOUNDS_S`). The bound applies to this one
    call only.

    A `NexusError` the operation raises for `REASON_TIMEOUT` (e.g. it calls
    `NexusClient.execute_command` directly with its own `timeout_s` and
    that command timed out) is treated the same as an `asyncio.wait_for`
    timeout here, since both mean "this operation did not finish in time".
    Any other `NexusError` (a dropped connection, an invalid result) is not
    this signal's shape and propagates unchanged -- that is process
    liveness's or the reconnect path's concern, not an unresponsive
    operation.
    """
    bound = bound_s if bound_s is not None else DEFAULT_OPERATION_BOUNDS_S[kind]
    try:
        return await asyncio.wait_for(operation(), timeout=bound)
    except TimeoutError:
        return OperationTimedOut(kind=kind, bound_s=bound)
    except NexusError as exc:
        if exc.detail.get("reason") == REASON_TIMEOUT:
            return OperationTimedOut(kind=kind, bound_s=bound)
        raise
