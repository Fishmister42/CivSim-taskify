r"""The detection aggregator (T150).

research R12 names four independent signals, any of which trips detection
within the 60 s budget of SC-010:

| Signal              | Module                          | Event on trip           |
|---------------------|----------------------------------|--------------------------|
| Process liveness     | `resilience.liveness`            | `crash_detected`         |
| Tuner heartbeat       | `resilience.heartbeat_monitor`   | `hang_detected`\*        |
| Per-operation bounds  | `resilience.operation_bounds`    | `unresponsive_detected`  |
| Screen-identity probe | (R13, a concurrent wave)         | `unknown_screen`         |

\* Or, for a corroborated dropped connection, `unresponsive_detected` /
`crash_detected` -- see below.

This module is the classifier and aggregator: it turns each signal's raw
outcome into the one `RunEvent` data-model.md SS14 declares for it, and
combines whichever signals a caller has the inputs to check right now into
a single pass. **The removed `stall` event type must not reappear here or
anywhere else** -- a turn that stops accomplishing anything is the turn
cycle's own no-progress backstop (data-model.md SS14, research R12), not a
fault this module detects.

**A single connection refusal is not evidence of a crash.** Civ VI accepts
exactly one tuner client at a time (contracts/nexus-protocol.md) and can
transiently refuse a rapid reconnect while a perfectly healthy client is
still releasing the previous socket -- a live incident this exact shape
was found against. `check_heartbeat` therefore never turns a single
`HeartbeatOutcome.CONNECTION_LOST` reading straight into an event: it
re-probes once, bounded by `DEFAULT_RECONNECT_REPROBE_DELAY_S`, and only
then classifies -- `crash_detected` exclusively when process liveness (the
authoritative signal: psutil against the client's own PID) also agrees the
process is gone, `unresponsive_detected` when the connection is still
unreachable but the PID is alive or liveness was not wired in, and no
event at all when the re-probe recovers. A plain `HANG` (a timed-out but
still-connected round-trip) has no such ambiguity and is still classified
on a single reading, exactly as before.

**No signal, and no function in this module, measures elapsed turn time.**
Every check below is a single point-in-time evaluation (process liveness),
a single bounded round-trip -- or, for a dropped connection specifically,
two, plus one short fixed pause between them, all scoped to that one
corroboration decision and never to a turn (heartbeat) -- a single bounded
call (per-operation bounds), or a single probe result (screen identity) --
never a duration accumulated across decision steps or across a turn. A
turn may legitimately run for hours across hundreds of steps; nothing here
would notice, because nothing here is watching the clock that way
(research R12, FR-014). SC-010's 60 s budget is satisfied by keeping each
individual signal's own bound short (the heartbeat's `DEFAULT_HEARTBEAT_
TIMEOUT_S`, the per-kind bounds in `DEFAULT_OPERATION_BOUNDS_S`, the fixed
`DEFAULT_RECONNECT_REPROBE_DELAY_S` pause), not by this module imposing a
wall-clock deadline of its own.

The screen-identity probe itself is declared catalog data implemented in a
concurrent wave (R13, observe/catalogs) -- this module depends on it only
through the `ScreenIdentityProbe` protocol below, so it has no import-time
dependency on that package.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from civsim_harness.models.common import EventId, RunId, Timestamp
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.resilience.heartbeat_monitor import HeartbeatMonitor, HeartbeatOutcome
from civsim_harness.resilience.liveness import ProcessLivenessMonitor
from civsim_harness.resilience.operation_bounds import (
    OperationKind,
    OperationTimedOut,
    run_bounded,
)

#: Bound on the pause before one re-probe of a dropped/refused tuner
#: connection (`HeartbeatOutcome.CONNECTION_LOST`). Short and fixed -- long
#: enough to let the game finish releasing a previous socket
#: (contracts/nexus-protocol.md: "the game accepts one tuner connection at
#: a time") before a rapid reconnect is retried, never derived from or
#: compared against how long a turn or a decision step has been running
#: (FR-014). Together with two `DEFAULT_HEARTBEAT_TIMEOUT_S`-bounded
#: reads, this keeps the whole corroboration decision a small, fixed
#: fraction of SC-010's 60 s detection budget (see `check_heartbeat`).
DEFAULT_RECONNECT_REPROBE_DELAY_S: float = 2.0


class ScreenIdentityProbe(Protocol):
    """What the detector needs from the R13 screen-identity observation.

    Implemented elsewhere (observe/catalogs, a concurrent wave): "which
    declared screen is currently up, if any". Defined here as a Protocol,
    not imported from that package, so this module never depends on
    unfinished code -- a caller wires in a real implementation once one
    exists; tests wire in a fake.
    """

    async def __call__(self) -> bool:
        """Return `True` when the current screen is one of the harness's
        declared screens, `False` when it is not recognised."""
        ...


def make_event(
    *,
    run_id: RunId,
    event_type: RunEventType,
    occurred_at: Timestamp,
    turn_number: int | None = None,
    step_index: int | None = None,
    detail: dict[str, Any] | None = None,
    event_id: EventId | None = None,
) -> RunEvent:
    """Build one detection `RunEvent`. `occurred_at` is always supplied by the
    caller (never read from a clock in here) so nothing in this module can
    drift into measuring a duration by accident.
    """
    return RunEvent(
        event_id=event_id if event_id is not None else EventId(uuid.uuid4().hex),
        run_id=run_id,
        turn_number=turn_number,
        step_index=step_index,
        event_type=event_type,
        occurred_at=occurred_at,
        detail=dict(detail) if detail else {},
    )


# --------------------------------------------------------------------------
# Individual signal checks -- each is one point-in-time evaluation
# --------------------------------------------------------------------------


def check_process_liveness(
    monitor: ProcessLivenessMonitor,
    *,
    run_id: RunId,
    occurred_at: Timestamp,
    turn_number: int | None = None,
    step_index: int | None = None,
) -> RunEvent | None:
    """`crash_detected` when the client PID is no longer a live process."""
    if monitor.check():
        return None
    return make_event(
        run_id=run_id,
        event_type=RunEventType.CRASH_DETECTED,
        occurred_at=occurred_at,
        turn_number=turn_number,
        step_index=step_index,
        detail={"pid": monitor.pid},
    )


async def check_heartbeat(
    monitor: HeartbeatMonitor,
    *,
    run_id: RunId,
    occurred_at: Timestamp,
    liveness: ProcessLivenessMonitor | None = None,
    reprobe_delay_s: float = DEFAULT_RECONNECT_REPROBE_DELAY_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    turn_number: int | None = None,
    step_index: int | None = None,
) -> RunEvent | None:
    """`hang_detected` when one heartbeat round-trip times out while still connected.

    research R12 names both "hang, deadlock" and "dropped tuner connection"
    as this signal's catches, but they are not equally trustworthy on a
    single reading. A `HANG` (the round-trip timed out; the socket itself
    is fine) is unambiguous and is reported immediately, as before.

    A `CONNECTION_LOST` reading is not: Civ VI accepts exactly one tuner
    client at a time and can transiently refuse a rapid reconnect while a
    perfectly healthy client is still releasing the previous socket
    (contracts/nexus-protocol.md) -- a live incident this exact shape was
    found against, where treating one refusal as a crash would abandon a
    good turn and reload a save that was never in danger. So a single
    `CONNECTION_LOST` reading is corroborated, never trusted on its own:

    1. Wait `reprobe_delay_s` (a short, fixed pause -- never derived from
       or compared against elapsed turn time, FR-014), then check once
       more. Recovered -> no event at all: the refusal was transient.
    2. Still lost -> defer to *liveness*, the strongest signal available
       (psutil against the client's own PID). PID confirmed gone ->
       `crash_detected`. PID alive, or no liveness monitor was supplied to
       corroborate against -> `unresponsive_detected`: the connection is
       still down, but that alone is not evidence the game has crashed.

    The outcome of whichever reading was last taken is preserved in
    `detail` either way, for anyone reading the record back.
    """
    outcome = await monitor.check()
    if outcome is HeartbeatOutcome.OK:
        return None

    if outcome is HeartbeatOutcome.CONNECTION_LOST:
        await sleep(reprobe_delay_s)
        outcome = await monitor.check()
        if outcome is HeartbeatOutcome.OK:
            return None  # transient refusal -- recovered on its own
        if outcome is HeartbeatOutcome.CONNECTION_LOST:
            if liveness is not None and not liveness.check():
                return make_event(
                    run_id=run_id,
                    event_type=RunEventType.CRASH_DETECTED,
                    occurred_at=occurred_at,
                    turn_number=turn_number,
                    step_index=step_index,
                    detail={
                        "heartbeat_outcome": outcome.value,
                        "pid": liveness.pid,
                        "corroborated_by": "process_liveness",
                        "reprobed": True,
                    },
                )
            return make_event(
                run_id=run_id,
                event_type=RunEventType.UNRESPONSIVE_DETECTED,
                occurred_at=occurred_at,
                turn_number=turn_number,
                step_index=step_index,
                detail={
                    "heartbeat_outcome": outcome.value,
                    "timeout_s": monitor.timeout_s,
                    "reprobed": True,
                },
            )
        # the re-probe itself came back a plain HANG -- fall through and
        # classify that latest reading below, same as any other hang.

    return make_event(
        run_id=run_id,
        event_type=RunEventType.HANG_DETECTED,
        occurred_at=occurred_at,
        turn_number=turn_number,
        step_index=step_index,
        detail={"heartbeat_outcome": outcome.value, "timeout_s": monitor.timeout_s},
    )


async def check_operation_bound[T](
    operation: Callable[[], Awaitable[T]],
    *,
    kind: OperationKind,
    run_id: RunId,
    occurred_at: Timestamp,
    bound_s: float | None = None,
    turn_number: int | None = None,
    step_index: int | None = None,
) -> tuple[T | None, RunEvent | None]:
    """`unresponsive_detected` when one operation does not complete within its
    own bound (research R12: "alive and answers the heartbeat but will not
    service a specific operation"). Bounds exactly this one call -- never a
    turn (FR-014).

    Returns `(result, None)` on success, or `(None, event)` on timeout so a
    caller can pattern-match without a second call into
    `resilience.operation_bounds` to re-derive the same information.
    """
    result = await run_bounded(operation, kind=kind, bound_s=bound_s)
    if isinstance(result, OperationTimedOut):
        event = make_event(
            run_id=run_id,
            event_type=RunEventType.UNRESPONSIVE_DETECTED,
            occurred_at=occurred_at,
            turn_number=turn_number,
            step_index=step_index,
            detail={"operation_kind": result.kind.value, "bound_s": result.bound_s},
        )
        return None, event
    return result, None


async def check_screen_identity(
    probe: ScreenIdentityProbe,
    *,
    run_id: RunId,
    occurred_at: Timestamp,
    turn_number: int | None = None,
    step_index: int | None = None,
) -> RunEvent | None:
    """`unknown_screen` when the current screen does not resolve to a declared one (R13)."""
    known = await probe()
    if known:
        return None
    return make_event(
        run_id=run_id,
        event_type=RunEventType.UNKNOWN_SCREEN,
        occurred_at=occurred_at,
        turn_number=turn_number,
        step_index=step_index,
    )


# --------------------------------------------------------------------------
# Aggregation -- combine whichever signals a caller currently has
# --------------------------------------------------------------------------


class DetectionAggregator:
    """Combines whichever of the four signals a caller can currently check.

    Each dependency is optional: a caller wires in only what it has at its
    point in the run (e.g. no `NexusClient` yet during preflight, no
    screen-identity probe available before observe/ is wired up) --
    omitted signals are skipped, not treated as failures. `check_once`
    performs exactly one pass over the signals it has: one liveness check,
    one heartbeat check, one screen probe. It does not loop, sleep, or
    retry itself -- any polling cadence belongs to the caller (the run
    orchestrator), which decides how often to invoke this, not to this
    aggregator, which must never itself become a hidden turn timer.

    The one exception is scoped entirely inside that single heartbeat
    check, not at this level: when both `liveness` and `heartbeat` are
    wired in, `check_once` forwards `liveness` into `check_heartbeat` so a
    dropped/refused connection is corroborated (one short, fixed-bound
    re-probe, then process liveness as the tiebreaker) before it is ever
    classified as `crash_detected` -- see `check_heartbeat` and
    `DEFAULT_RECONNECT_REPROBE_DELAY_S`. That pause is still a small, fixed
    quantity scoped to this one detection pass, never a loop and never
    keyed to elapsed turn time (FR-014).
    """

    def __init__(
        self,
        *,
        liveness: ProcessLivenessMonitor | None = None,
        heartbeat: HeartbeatMonitor | None = None,
        screen_identity_probe: ScreenIdentityProbe | None = None,
        reprobe_delay_s: float = DEFAULT_RECONNECT_REPROBE_DELAY_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._liveness = liveness
        self._heartbeat = heartbeat
        self._screen_identity_probe = screen_identity_probe
        self._reprobe_delay_s = reprobe_delay_s
        self._sleep = sleep

    async def check_once(
        self,
        *,
        run_id: RunId,
        occurred_at: Timestamp,
        turn_number: int | None = None,
        step_index: int | None = None,
    ) -> list[RunEvent]:
        """Run every wired-in signal once; return every event tripped (0 or more).

        Order is liveness, then heartbeat, then screen identity -- cheapest
        and least ambiguous first -- but all wired-in signals are checked
        regardless of an earlier one tripping, since a caller may want the
        full picture (e.g. both a crash and an unknown screen from a game
        that died mid-modal) rather than only the first.
        """
        events: list[RunEvent] = []
        if self._liveness is not None:
            event = check_process_liveness(
                self._liveness,
                run_id=run_id,
                occurred_at=occurred_at,
                turn_number=turn_number,
                step_index=step_index,
            )
            if event is not None:
                events.append(event)
        if self._heartbeat is not None:
            event = await check_heartbeat(
                self._heartbeat,
                run_id=run_id,
                occurred_at=occurred_at,
                liveness=self._liveness,
                reprobe_delay_s=self._reprobe_delay_s,
                sleep=self._sleep,
                turn_number=turn_number,
                step_index=step_index,
            )
            if event is not None:
                events.append(event)
        if self._screen_identity_probe is not None:
            event = await check_screen_identity(
                self._screen_identity_probe,
                run_id=run_id,
                occurred_at=occurred_at,
                turn_number=turn_number,
                step_index=step_index,
            )
            if event is not None:
                events.append(event)
        return events
