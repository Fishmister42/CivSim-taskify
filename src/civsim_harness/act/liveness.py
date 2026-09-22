"""Per-attempt liveness for the confirm-execution poll loop (T302).

**The defect this closes.** ``act/verify.py::confirm_execution`` re-reads the tuner every
``poll_s`` (2 s in production) for up to ``timeout_s`` -- 200 s at both production call
sites, ``run/decision_loop.py::END_TURN_CONFIRM_TIMEOUT_S`` and
``run/turn_cycle.py::BACKSTOP_CONFIRM_TIMEOUT_S`` -- because an end turn is confirmed only
once every AI player has taken theirs. It is the single longest legitimate operation in the
system, and it emitted nothing a reader outside the process could see: the ``ConfirmWindow``
it builds every attempt was recorded only on the *final* verification result, which is
published when the wait is already over. Under a watchdog that treats three minutes without
an affirmative signal as stuck, this loop is the first healthy thing killed, during exactly
the slow turn its raised bound exists to accommodate.

    A phase that is silent by construction is a defect in the phase, never evidence about
    the run -- and widening a ceiling until the silence fits is how a threshold gets chosen
    without measuring what it bounds.

Accordingly nothing here changes ``timeout_s``, ``poll_s`` or the fail-closed behaviour.
The loop's bound is what it was; the difference is that each attempt now publishes that it
is still looking.

**Principle I -- what is deliberately NOT in the record.** This is harness telemetry, not
observation, and it must not become a second channel by which game state reaches anything
that shapes a decision. The payload carries only:

* ``declaration_id`` -- which catalog action is being confirmed;
* ``predicate`` -- the *static catalog expression* being waited on, i.e. the shape of the
  question, never its answer;
* ``attempt``, ``elapsed_s``, ``timeout_s``, ``poll_s`` -- the window, from values
  ``confirm_execution`` has already computed for its own record.

It carries **no** ``Observation``, no predicate *bindings*, and no
:func:`~civsim_harness.act.predicates.last_read` quotation -- those are readings of the
live game, and ``confirm_execution`` keeps them for the durable verification detail, which
is the ledger, not a heartbeat. Nothing here is returned to the caller or merged into
``ExecutionVerification``, so no code path exists by which a tick could reach the agent.

**Cheapness.** This runs every two seconds for the length of a turn, so the emitter builds
one small flat dict of values the caller already holds, takes no lock of its own beyond
``logging``'s, and swallows anything it raises: a telemetry failure must never turn a
confirmed action into an unconfirmed one.

The emitter is deliberately *not* shared with ``provider/liveness.py``. The two modules
publish to the same logger by the same call, but their Principle I boundaries are
different -- that one must exclude prompt and response content, this one must exclude game
state -- and a single shared helper would put both exclusions one refactor away from each
other with nothing red to show for it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from logging import INFO
from typing import TYPE_CHECKING, Any, Final

from civsim_harness.telemetry.logging import get_harness_logger, log_event

if TYPE_CHECKING:  # pragma: no cover -- import cycle guard only
    from civsim_harness.act.verify import ConfirmWindow
    from civsim_harness.models.catalog import ParityDeclaration

#: The record kind published once per confirm attempt. A fixed string so a log consumer
#: (and ``tests/contract/test_long_phase_liveness.py``) can match on it rather than on
#: prose that may be reworded.
CONFIRM_EXECUTION_WAITING: Final[str] = "act.confirm_execution.waiting"


def emit_harness_liveness(event: str, payload: Mapping[str, Any]) -> None:
    """Publish one harness-telemetry record, swallowing anything it raises.

    ``log_event`` on the ``civsim_harness`` logger, so the record passes the redacting
    filter and formatter ``telemetry/logging.py`` forces onto every sanctioned handler and
    lands on the stderr handler ``operator/cli.py`` configures -- which is what the driver
    log a watchdog polls actually captures.
    """
    try:
        log_event(get_harness_logger(), INFO, event, extra=dict(payload))
    except Exception:  # noqa: BLE001 -- telemetry must never fail the action it describes
        pass


def emit_confirm_liveness(
    *,
    declaration: ParityDeclaration,
    window: ConfirmWindow,
    emit: Callable[[str, Mapping[str, Any]], None] = emit_harness_liveness,
) -> None:
    """Publish one :data:`CONFIRM_EXECUTION_WAITING` record for the attempt *window* describes.

    Emitted *before* the attempt's evaluation rather than after it, so the record exists
    even if the evaluation itself is what hangs -- a heartbeat published only on the way
    out of the thing it is meant to prove alive proves nothing.
    """
    emit(
        CONFIRM_EXECUTION_WAITING,
        {
            "declaration_id": str(declaration.declaration_id),
            "predicate": declaration.verification_predicate,
            "attempt": window.attempts,
            "elapsed_s": round(window.elapsed_s, 3),
            "timeout_s": window.timeout_s,
            "poll_s": window.poll_s,
        },
    )


__all__ = [
    "CONFIRM_EXECUTION_WAITING",
    "emit_confirm_liveness",
    "emit_harness_liveness",
]
