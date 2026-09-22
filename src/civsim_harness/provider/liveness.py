"""Liveness ticks around an in-flight provider call (T290).

**The defect this closes.** ``run/decision_loop.py`` reaches the model with a single
blocking line -- ``response = ctx.provider.complete(request)`` -- and until this module
existed the whole ``provider`` package emitted *nothing*: no logger, no telemetry import,
no structured event of any kind. MEASURED on the live driver logs, 2026-09-22: a decision
step waited **146 s** on a model with not one line published in between. Against a watchdog
that treats three minutes without an affirmative signal as stuck, a perfectly healthy call
-- ``openrouter.DEFAULT_REQUEST_TIMEOUT_S`` is 120 s per attempt, and
``chain.ProviderChain`` may legitimately spend several of those in a row -- is
indistinguishable from a dead process.

The governing rule, and the reason this is a new module rather than a widened bound:

    A phase that is silent by construction is a defect in the phase, never evidence
    about the run -- and widening a ceiling until the silence fits is how a threshold
    gets chosen without measuring what it bounds.

So nothing here touches a timeout. The request bound is exactly what it was; what changes
is that the wait is now *readable* while it happens.

**Why a thread and not a start/end pair.** A single "started" line logged three minutes ago
is not a heartbeat -- it is the same silence with a timestamp on the front, and a watchdog
reading "last line 179 s ago" cannot tell it from a hang. ``httpx.Client.post`` is a
blocking call with no progress callback, so the only way to publish elapsed time *during*
it is a second thread. :func:`provider_call_liveness` starts a daemon thread that emits a
tick every :data:`PROVIDER_TICK_INTERVAL_S` seconds until the call returns, so the gap
between consecutive records is bounded by the tick interval rather than by the call.

**Principle I.** Everything emitted here is harness telemetry -- provider name, model name,
elapsed seconds, tick number, the request bound. It goes to the ``civsim_harness`` logger,
which ``telemetry/logging.py`` forces through the redacting filter and formatter, and it is
never returned to a caller, never attached to a ``DecisionResponse``, and never reaches an
``Observation``. No prompt text, no observation payload, no image bytes, and no response
content is read by this module at all: the tick payload is built *before* the call starts,
from values already in hand, and the ticker thread only adds a counter and a clock reading.

**Failure is never the caller's problem.** A tick that cannot be emitted is swallowed. A
telemetry thread must not be able to fail a model call -- the emission exists to describe
the call, so letting it break the call would invert the whole point.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from logging import INFO
from typing import Any, Final

from civsim_harness.telemetry.logging import get_harness_logger, log_event

#: Seconds between consecutive in-flight ticks. Chosen against the thing it actually
#: bounds: the watchdog's three-minute silence budget. At 15 s a stalled emitter has to
#: miss twelve consecutive ticks before the run looks stuck, so the signal is unambiguous
#: long before the kill threshold, while a 120 s request costs at most eight extra log
#: lines. This is a *publication* interval, not a timeout -- raising it cannot hide a
#: hang, it only makes the evidence coarser.
PROVIDER_TICK_INTERVAL_S: Final[float] = 15.0

#: The three record kinds this module publishes. Named constants so a log consumer (and
#: ``tests/contract/test_long_phase_liveness.py``) can match on a fixed string rather than
#: on prose that may be reworded.
PROVIDER_CALL_STARTED: Final[str] = "provider.call.started"
PROVIDER_CALL_WAITING: Final[str] = "provider.call.waiting"
PROVIDER_CALL_FINISHED: Final[str] = "provider.call.finished"

#: Published by ``chain.ProviderChain.complete_step`` before each backoff sleep. The chain
#: already records a ``provider_retry`` RunEvent, but that reaches the *store*; a watchdog
#: polling the driver log cannot see it, and the chain is a composite phase that can
#: legitimately run several 120 s attempts back to back.
PROVIDER_CHAIN_RETRY: Final[str] = "provider.chain.retry"

#: The interval used to poll the "call finished" flag. Deliberately much smaller than
#: :data:`PROVIDER_TICK_INTERVAL_S` so the ticker thread joins promptly when the call
#: returns rather than keeping the process alive for up to a full tick.
_JOIN_POLL_S: Final[float] = 0.25


def emit_provider_liveness(event: str, payload: Mapping[str, Any]) -> None:
    """Publish one harness-telemetry record, swallowing anything it raises.

    ``log_event`` on the ``civsim_harness`` logger, so the record lands on the redacting
    handler ``operator/cli.py`` configures onto stderr -- which is what the driver log a
    watchdog polls actually captures.
    """
    try:
        log_event(get_harness_logger(), INFO, event, extra=dict(payload))
    except Exception:  # noqa: BLE001 -- telemetry must never fail the call it describes
        pass


@contextmanager
def provider_call_liveness(
    *,
    provider: str,
    model: str,
    step_index: int | None = None,
    bound_s: float | None = None,
    tick_interval_s: float = PROVIDER_TICK_INTERVAL_S,
    emit: Callable[[str, Mapping[str, Any]], None] = emit_provider_liveness,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    """Publish a heartbeat for the duration of one in-flight provider call.

    Emits :data:`PROVIDER_CALL_STARTED` on entry, :data:`PROVIDER_CALL_WAITING` every
    *tick_interval_s* while the body runs, and :data:`PROVIDER_CALL_FINISHED` on exit --
    on the exception path too, so a call that dies mid-flight still closes its own record
    rather than leaving a dangling "started".

    *provider* and *model* say **what is being waited for**; ``elapsed_s`` says how long;
    ``bound_s`` says how long it is allowed to take, so a reader can tell "two thirds of
    the way through a legitimate 120 s request" from "past its own bound and therefore
    genuinely wrong".

    *emit*, *monotonic* and *tick_interval_s* are injectable so a test can drive the whole
    window without waiting and without a logging handler.
    """
    started = monotonic()
    base: dict[str, Any] = {"provider": provider, "model": model}
    if step_index is not None:
        base["step_index"] = step_index
    if bound_s is not None:
        base["bound_s"] = bound_s

    emit(PROVIDER_CALL_STARTED, {**base, "elapsed_s": 0.0})

    done = threading.Event()

    # Never coarser than the tick interval itself: a caller asking for fast ticks (a test
    # driving the whole window in milliseconds) must get them, and the poll exists only so
    # the thread notices the call returned without waiting out a full interval.
    join_poll_s = min(_JOIN_POLL_S, tick_interval_s)

    def _tick() -> None:
        tick = 0
        while not done.wait(join_poll_s):
            if monotonic() - started >= (tick + 1) * tick_interval_s:
                tick += 1
                emit(
                    PROVIDER_CALL_WAITING,
                    {**base, "elapsed_s": round(monotonic() - started, 3), "tick": tick},
                )

    ticker = threading.Thread(target=_tick, name="provider-call-liveness", daemon=True)
    ticker.start()
    try:
        yield
    finally:
        done.set()
        ticker.join(timeout=tick_interval_s)
        emit(
            PROVIDER_CALL_FINISHED,
            {**base, "elapsed_s": round(monotonic() - started, 3)},
        )


__all__ = [
    "PROVIDER_CALL_FINISHED",
    "PROVIDER_CALL_STARTED",
    "PROVIDER_CALL_WAITING",
    "PROVIDER_CHAIN_RETRY",
    "PROVIDER_TICK_INTERVAL_S",
    "emit_provider_liveness",
    "provider_call_liveness",
]
