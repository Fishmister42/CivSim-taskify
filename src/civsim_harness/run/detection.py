"""Wiring research R12's detection signals into the run loop (T233).

`resilience/detector.py` (T150), `resilience/heartbeat_monitor.py` (T148),
`resilience/liveness.py` (T147) and `resilience/operation_bounds.py` (T149) were each built and
each individually tested, and **nothing in `src/` ever constructed any of them** -- outside their
own modules they appeared only in `resilience/__init__.py`'s re-export list. So none of R12's
signals ran during a real run, and a client that died between turns was noticed only when the next
Nexus call happened to fail, if it did. **SC-010 ("a game client crash is detected and recorded
within 60 seconds") had no mechanism behind it at all.** This module is that mechanism's wiring:
`run/composition.py` builds one :class:`DetectionWatch` per run, and `run/turn_cycle.py` runs it
at the two points a fault can actually be caught.

**The two points, and why each is what it is.**

*Between turns* -- before this turn's quicksave has been taken -- only
:meth:`DetectionWatch.check_liveness_now` runs. It is a single, synchronous, point-in-time
``psutil`` call against the client's own PID: it cannot block, it issues no Nexus traffic outside
the turn's own work, and it is the authoritative answer to the one question that matters at a turn
boundary ("is the client still there at all"). A client that *hung* rather than died between turns
is caught a moment later by the quicksave command's own per-operation bound, which is already
enforced by `nexus/client.py` (T032). Nothing here is recoverable: the turn has no start quicksave
yet, so there is no attempt to abandon and nothing for `RecoveryEngine` to resume from -- the fault
is recorded and :class:`ClientFaultDetected` is raised, loudly and by name, rather than letting the
quicksave fail with a generic transport error that says nothing about *why*.

*During a turn* -- concurrently with the decision-step loop -- the whole
:class:`~civsim_harness.resilience.detector.DetectionAggregator` pass runs on a fixed cadence
(:data:`DEFAULT_DETECTION_INTERVAL_S`), which is what makes SC-010's 60 s budget a property of the
harness rather than of how long a decision step happens to take. This *is* recoverable: the turn's
start quicksave exists, so a detected fault routes into the same
:class:`~civsim_harness.resilience.recovery.RecoveryEngine` path a mid-turn observation-assembly
failure already takes (FR-045, FR-046) -- abandon the attempt, resume from this turn's own start
save, re-observe and re-capture before acting.

**Nothing here measures elapsed turn time, and the cadence is not a turn timer.**
:data:`DEFAULT_DETECTION_INTERVAL_S` bounds how long the harness may go *without asking* whether
the client is alive; it is never compared against how long the turn, the step, or the operation
under way has been running, and a pass that trips nothing simply schedules the next pass (research
R12, FR-014). A turn may legitimately run for hours across hundreds of steps and trip nothing.

**Why the aggregate pass is itself bounded.** `NexusClient` serializes socket access behind an
``asyncio.Lock`` that is acquired *outside* each command's own ``wait_for`` -- so a heartbeat probe
issued while a long command is in flight waits on the lock for as long as that command takes, with
its own ``timeout_s`` not yet counting. Left unbounded, one such wait would stall the watchdog
indefinitely and silently switch SC-010 back off. The pass therefore runs under
:func:`~civsim_harness.resilience.operation_bounds.run_bounded`, and a pass that exceeds its bound
is **not** classified as anything: a held lock is a busy client, not a faulty one, and inventing a
fault from slowness is exactly the turn-timer-by-the-back-door R12 rejects. Liveness is checked
separately and first, before that bounded pass, precisely so the authoritative crash signal can
never be lost to a pass that wedged on the lock.

**The screen-identity probe is deliberately not wired in here.** R12 lists it as the fourth signal
and `DetectionAggregator` accepts it, but `run/decision_loop.py` already polls the declared
``game.screen_state`` observation between decision steps and raises
:class:`~civsim_harness.run.decision_loop.UnknownScreenEncountered` (FR-049, research R13) --
which is R13's own "polled between decision steps" cadence, on the same declaration, carrying the
same ``unknown_screen`` event. Wiring a second producer of that event here would put two
independent paths on one timeline entry with no way to tell which wrote it. One definition,
wherever it lives.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Any

from civsim_harness.errors import HarnessError
from civsim_harness.models.common import RunId, Timestamp
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.resilience.detector import DetectionAggregator, check_process_liveness
from civsim_harness.resilience.liveness import ProcessLivenessMonitor
from civsim_harness.resilience.operation_bounds import (
    OperationKind,
    OperationTimedOut,
    run_bounded,
)
from civsim_harness.store.port import MatchStore
from civsim_harness.telemetry.logging import get_harness_logger, log_event

#: How often the in-turn watchdog asks the detection signals whether the client is still healthy.
#: Four passes fit inside SC-010's 60 s budget with room for one pass's own bound, which is the
#: whole point of choosing it well under the budget rather than at it. **Not a turn timer**: it
#: bounds how long the harness may go without *asking*, never how long a turn, a step, or an
#: operation may take (research R12, FR-014).
DEFAULT_DETECTION_INTERVAL_S: float = 10.0

#: Bound on one whole aggregate detection pass (see this module's docstring on why the pass needs
#: one at all). Generous enough to cover a wait on `NexusClient`'s command lock plus the
#: heartbeat's own re-probe sequence, and short enough that a wedged pass cannot swallow the
#: detection budget. A pass that exceeds it is reported as nothing at all -- never as a fault.
DEFAULT_DETECTION_PASS_BOUND_S: float = 45.0

#: The detected faults that route into `RecoveryEngine`, most authoritative first. ``crash`` ranks
#: above ``hang`` ranks above ``unresponsive`` for the same reason `detector.check_heartbeat`
#: corroborates a dropped connection against process liveness before classifying it: the more
#: specific finding is the one worth putting on the timeline as this recovery's trigger.
#: ``unknown_screen`` is deliberately absent -- FR-049 stalls the run visibly rather than
#: recovering, and `run/decision_loop.py` already owns that path.
RECOVERABLE_DETECTIONS: tuple[RunEventType, ...] = (
    RunEventType.CRASH_DETECTED,
    RunEventType.HANG_DETECTED,
    RunEventType.UNRESPONSIVE_DETECTED,
)


class ClientFaultDetected(HarnessError):
    """One of R12's detection signals tripped (FR-044, SC-010).

    Carries every :class:`~civsim_harness.models.records.RunEvent` the tripping pass produced --
    already durably written by :class:`DetectionWatch`, so a caller never has to persist them
    again -- and *primary*, the one event type this fault is classified as for the purpose of
    routing (see :data:`RECOVERABLE_DETECTIONS`).

    Raised only where the fault is **not** recoverable at the point it was found: between turns,
    where no start quicksave for the turn exists yet and there is therefore no attempt to abandon
    and nothing for `RecoveryEngine` to resume from. A fault found *during* a turn is routed into
    recovery by `run/turn_cycle.py` instead, and this exception never leaves that module.
    """

    def __init__(
        self,
        message: str,
        *,
        events: Sequence[RunEvent],
        primary: RunEventType,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            detail={
                "primary_detection": primary.value,
                "detections": [event.event_type.value for event in events],
                **(detail or {}),
            },
        )
        self.events = tuple(events)
        self.primary = primary


def primary_fault(events: Sequence[RunEvent]) -> RunEventType | None:
    """The one recoverable detection in *events* worth routing on, or ``None`` if there is none.

    ``None`` for an empty pass (nothing tripped) and for a pass that tripped only
    ``unknown_screen``, which is FR-049's visible stall rather than a fault to recover from.
    """
    tripped = {event.event_type for event in events}
    for event_type in RECOVERABLE_DETECTIONS:
        if event_type in tripped:
            return event_type
    return None


class DetectionWatch:
    """One run's binding of the R12 detection signals to that run's own record.

    Holds the `DetectionAggregator` (which owns *how* each signal is classified), the run id every
    event is attributed to, and the store every tripped event is durably written to before it is
    ever reported to a caller -- SC-010 asks for a crash "detected **and recorded** within 60
    seconds", and a detection this object returned but never persisted would satisfy only half of
    that.

    Deliberately not a poller: :meth:`check_now` and :meth:`check_liveness_now` each perform
    exactly one pass and return. The cadence belongs to :func:`run_under_detection`, whose caller
    (`run/turn_cycle.py`) is the thing that knows what a turn is.
    """

    def __init__(
        self,
        *,
        aggregator: DetectionAggregator,
        store: MatchStore,
        run_id: RunId,
        clock: Callable[[], Timestamp],
        liveness: ProcessLivenessMonitor | None = None,
        pass_bound_s: float = DEFAULT_DETECTION_PASS_BOUND_S,
    ) -> None:
        self._aggregator = aggregator
        self._store = store
        self._run_id = run_id
        self._clock = clock
        self._liveness = liveness
        self._pass_bound_s = pass_bound_s

    @property
    def run_id(self) -> RunId:
        return self._run_id

    async def check_liveness_now(
        self, *, turn_number: int | None = None, step_index: int | None = None
    ) -> tuple[RunEvent, ...]:
        """The authoritative crash signal alone: one ``psutil`` call against the client's PID.

        Synchronous underneath (`resilience/liveness.py` is a point-in-time check with no clock
        and no I/O beyond ``psutil``), so it cannot block on `NexusClient`'s command lock and
        cannot be lost to a pass that did. Returns ``()`` when no liveness monitor was wired in --
        a host that could not locate the client process has no PID to key on, and a fabricated one
        would be worse than none.
        """
        if self._liveness is None:
            return ()
        event = check_process_liveness(
            self._liveness,
            run_id=self._run_id,
            occurred_at=self._clock(),
            turn_number=turn_number,
            step_index=step_index,
        )
        if event is None:
            return ()
        self._store.write_run_event(event)
        return (event,)

    async def check_now(
        self, *, turn_number: int | None = None, step_index: int | None = None
    ) -> tuple[RunEvent, ...]:
        """One whole detection pass: liveness first and alone, then the bounded aggregate pass.

        Returns every event that tripped, each already written to the store. An empty tuple means
        either that nothing tripped or that the aggregate pass could not complete within its bound
        -- see this module's docstring: a pass that ran out of time is reported as nothing, never
        as a fault, because a `NexusClient` lock held by a long legitimate command is evidence of a
        busy client and of nothing else.
        """
        liveness_events = await self.check_liveness_now(
            turn_number=turn_number, step_index=step_index
        )
        if liveness_events:
            # The client process is gone. Probing a heartbeat through a socket to a dead process
            # can only produce a second, less specific way of saying so.
            return liveness_events

        outcome = await run_bounded(
            lambda: self._aggregator.check_once(
                run_id=self._run_id,
                occurred_at=self._clock(),
                turn_number=turn_number,
                step_index=step_index,
            ),
            kind=OperationKind.NEXUS_COMMAND,
            bound_s=self._pass_bound_s,
        )
        if isinstance(outcome, OperationTimedOut):
            log_event(
                get_harness_logger(),
                logging.WARNING,
                "run/detection: a detection pass did not complete within its own bound and was "
                "discarded; a busy client is not a faulty one (research R12, FR-014)",
                extra={
                    "run_id": str(self._run_id),
                    "turn_number": turn_number,
                    "bound_s": outcome.bound_s,
                },
            )
            return ()

        for event in outcome:
            self._store.write_run_event(event)
        return tuple(outcome)


async def run_under_detection[T](
    operation: Callable[[], Awaitable[T]],
    *,
    watch: DetectionWatch,
    turn_number: int,
    interval_s: float = DEFAULT_DETECTION_INTERVAL_S,
) -> T:
    """Run *operation* while asking *watch* every *interval_s* whether the client is still healthy.

    Returns whatever *operation* returned, and propagates whatever it raised, unchanged -- a turn
    that fails on its own terms (`MidTurnObservationFailure`, `ProviderChainExhausted`,
    `UnknownScreenEncountered`) must reach its existing handler with its own meaning intact, never
    reclassified as a detection.

    Raises :class:`ClientFaultDetected` when a pass trips a recoverable fault, after cancelling
    *operation*. The cancellation is what SC-010's "losing at most the turn in progress" allows and
    what FR-046 requires: whatever that attempt had assembled was read from a client that has since
    been found faulty, so none of it may be reused, and the caller replays the turn from its start
    quicksave instead. The detection events themselves are already durable by then --
    :class:`DetectionWatch` writes each one before reporting it -- so the record shows *why* the
    attempt ended even though the attempt itself contributed no steps.

    A pass that raises (a detector fault of its own, rather than a detected fault) is logged and
    skipped: a watchdog that cannot run must not be the reason a healthy turn is abandoned.
    """
    task: asyncio.Task[T] = asyncio.ensure_future(operation())
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=interval_s)
            if done:
                return task.result()

            try:
                events = await watch.check_now(turn_number=turn_number)
            except Exception as exc:  # noqa: BLE001 - see docstring: never abandon a healthy turn
                log_event(
                    get_harness_logger(),
                    logging.WARNING,
                    "run/detection: a detection pass failed; the turn continues and the next "
                    "pass will try again",
                    extra={
                        "run_id": str(watch.run_id),
                        "turn_number": turn_number,
                        "error_type": type(exc).__name__,
                    },
                )
                continue

            fault = primary_fault(events)
            if fault is None:
                continue

            task.cancel()
            with suppress(BaseException):
                await task
            raise ClientFaultDetected(
                "the game client was detected faulty while this turn was being played; the "
                "attempt is abandoned and replayed from this turn's own start quicksave "
                "(FR-044, FR-045, SC-010)",
                events=events,
                primary=fault,
                detail={"run_id": str(watch.run_id), "turn_number": turn_number},
            )
    finally:
        if not task.done():
            task.cancel()
            with suppress(BaseException):
                await task


__all__ = [
    "DEFAULT_DETECTION_INTERVAL_S",
    "DEFAULT_DETECTION_PASS_BOUND_S",
    "RECOVERABLE_DETECTIONS",
    "ClientFaultDetected",
    "DetectionWatch",
    "primary_fault",
    "run_under_detection",
]
