"""Execution verification (T108).

Evaluates a dispatched action's declared ``verification_predicate`` against re-read game state
and **derives** ``applied | rejected | partially_applied`` from the result -- never asserted by
the executor (FR-011, invariant I4). The same verification result determines the step's
``progress`` in ``{changed_state, no_change, rejected}`` (data-model.md §6), so a weak predicate
both mis-records the action and disables the no-progress backstop (research R14) -- which is
exactly why this module treats "the predicate could not be evaluated" identically to "the
predicate evaluated false": never a free pass to ``applied``.

**"Read state back through GameCore_Tuner" without this module touching Nexus itself.** Research
R14's loop is *observe -> decide -> execute -> verify -> account*; "reading state back" is
precisely what :mod:`civsim_harness.observe.assemble` already does (most of the game's own
read-only observations run in the ``GameCore_Tuner`` context -- see
``catalogs/observations/*.yaml``'s own ``context`` fields). Rather than this module independently
re-implementing "call Nexus, get JSON, validate it", it accepts a **freshly re-assembled**
post-execution ``Observation`` -- produced by the run loop calling
``observe.assemble.assemble_observation`` again after execution -- and evaluates purely against
that and the pre-execution ``Observation``. This keeps a single source of truth for "how the
harness reads game state" and keeps this module a pure function, fully testable without a running
game or a Nexus connection, in the same spirit as ``run.lifecycle.transition``.

**Why only two of the three ``ExecutionOutcome`` values are ever produced here.** Every action
declares exactly one ``verification_predicate`` -- a single boolean expression (contracts/
capability-catalog.md; ``catalogs/README.md`` §4's grammar has no arithmetic and returns one
truth value). A single boolean cannot, by construction, distinguish three outcomes. The one worked
example the contracts document gives (``turn.end_turn``) resolves the ambiguity directly: a false
verification result is described as "a click the game swallowed" -- i.e. the action had **no**
effect at all, not a partial one -- and data-model.md's own step-level wording confirms it:
"``rejected`` means it was refused before **or at** execution". So this module maps:

- predicate ``True``  -> ``ExecutionOutcome.APPLIED``   / ``StepProgress.CHANGED_STATE``
- predicate ``False`` (or unevaluable) -> ``ExecutionOutcome.REJECTED`` (``rejection_reason =
  VERIFICATION_FAILED``) / ``StepProgress.REJECTED``

``ExecutionOutcome.PARTIALLY_APPLIED`` and ``StepProgress.NO_CHANGE`` remain valid, constructible
model states -- reachable once a future catalog action declares a verification predicate expressive
enough to report a partial effect -- but no declaration in this catalog does that today, and this
module does not invent a false three-way split out of a single bool to manufacture one. This is a
deliberate, documented scope call for this wave, not an oversight; it is restated in this wave's
implementation report.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from civsim_harness.act.liveness import emit_confirm_liveness, emit_harness_liveness
from civsim_harness.act.predicates import (
    PredicateEvaluationError,
    build_predicate_bindings,
    evaluate_expression,
    evaluate_predicate,
    membership_collection,
    split_conjuncts,
)
from civsim_harness.models.catalog import ParityDeclaration
from civsim_harness.models.common import Timestamp
from civsim_harness.models.decision import ActionExecution, ExecutionOutcome, RejectionReason
from civsim_harness.models.turn import Observation, StepProgress

#: Which pre-execution snapshot fields this catalog's verification predicates actually reference,
#: as ``observed_<name>`` -> ``(namespace, field)`` in the pre-execution bindings environment
#: (``catalogs/README.md`` §4 ``observed_*``: "only the snapshots an actual predicate ... needs
#: need exist"). Extending this table alongside a new catalog predicate that needs another
#: ``observed_*`` name is the correct way to grow it; an unlisted ``observed_*`` name still fails
#: loudly at evaluation time (see ``act.predicates.evaluate_predicate``) rather than silently
#: binding to ``None``.
_OBSERVED_FIELD_SOURCES: Mapping[str, tuple[str, str]] = {
    "observed_turn_number": ("game", "turn_number"),
    "observed_diplomatic_favor": ("player", "diplomatic_favor"),
    # `units.build_improvement`: the build charge counter on the unit panel, read before the order
    # goes out. The bindings below are built with no ``target``, so the ``unit`` namespace resolves
    # to the entry ``units.state`` reports as selected -- the same unit a build order acts on.
    "observed_charges_remaining": ("unit", "charges_remaining"),
}


def observed_snapshot(pre_observation: Observation) -> dict[str, Any]:
    """Flatten *pre_observation* into the ``observed_*`` keys this catalog's verification
    predicates reference (see :data:`_OBSERVED_FIELD_SOURCES`).

    Built from the same :func:`~civsim_harness.act.predicates.build_predicate_bindings` the
    dispatch/verify bindings themselves use, so a rename applied there (e.g. ``screen`` ->
    ``current_screen``) is reflected here automatically rather than needing to be duplicated.
    """
    pre_bindings = build_predicate_bindings(observation=pre_observation)
    snapshot: dict[str, Any] = {}
    for observed_name, (namespace, field) in _OBSERVED_FIELD_SOURCES.items():
        namespace_value = pre_bindings.get(namespace)
        if isinstance(namespace_value, Mapping) and field in namespace_value:
            snapshot[observed_name] = namespace_value[field]
    return snapshot


@dataclass(frozen=True)
class ExecutionVerification:
    """The derived outcome of one action's post-execution verification (T108)."""

    execution: ActionExecution
    progress: StepProgress


@dataclass(frozen=True)
class ConfirmWindow:
    """How hard the harness looked before it called an effect unconfirmed.

    A client read can lag its own game state: MEASURED 2026-09-21 that a `units.move_to` accepted
    by `RequestOperation` still showed the warrior on its old plot at +0 s and on the destination
    at +1 s (four of seven moves in gameplay block 2 were recorded ``verification_failed`` for that
    reason alone), and that an end turn is confirmed only after the AI players have taken theirs.
    So verification re-reads, bounded. Recording the window on the result is what keeps the
    distinction honest in the record: an ``applied`` says which read confirmed it, and a
    ``verification_failed`` says how long and how often the harness looked before saying so --
    without that, "the client was a beat behind" and "the game refused the order" are the same row.
    """

    timeout_s: float
    poll_s: float
    attempts: int
    elapsed_s: float

    def as_detail(self) -> dict[str, Any]:
        return {
            "confirm_timeout_s": self.timeout_s,
            "confirm_poll_s": self.poll_s,
            "confirm_attempts": self.attempts,
            "confirm_elapsed_s": round(self.elapsed_s, 3),
        }


def verify_execution(
    *,
    declaration: ParityDeclaration,
    pre_observation: Observation,
    post_observation: Observation,
    target: Any = None,
    verified_at: Timestamp,
    confirm_window: ConfirmWindow | None = None,
) -> ExecutionVerification:
    """Evaluate *declaration*'s ``verification_predicate`` and derive its outcome and progress.

    *pre_observation* is the observation assembled immediately before execution (its own
    ``observed_*`` snapshot is derived from it via :func:`observed_snapshot`); *post_observation*
    is the fresh observation assembled immediately after (see module docstring for why this module
    does not re-read state itself). Both must be genuinely fresh, step-scoped observations -- this
    function does not, and cannot, check that its caller obeyed FR-015; it only ever evaluates
    whatever it is handed.

    *confirm_window*, when given, is recorded on the result: on an ``applied`` it says which
    re-read confirmed the effect, and on a ``verification_failed`` it states the bound the harness
    looked within and quotes what that last read actually said (see :class:`ConfirmWindow`).
    :func:`confirm_execution` is the bounded caller that supplies it.
    """
    assert declaration.verification_predicate is not None  # guaranteed for kind == action

    snapshot = observed_snapshot(pre_observation)
    bindings = build_predicate_bindings(
        observation=post_observation, target=target, observed_snapshot=snapshot
    )

    try:
        confirmed = evaluate_predicate(declaration.verification_predicate, bindings)
    except PredicateEvaluationError as exc:
        return _rejected(
            declaration=declaration,
            verified_at=verified_at,
            reason=f"verification_predicate could not be evaluated: {exc.message}",
            extra_detail=exc.detail,
            confirm_window=confirm_window,
            bindings=bindings,
        )

    if confirmed:
        detail: dict[str, Any] = {
            "declaration_id": str(declaration.declaration_id),
            "predicate": declaration.verification_predicate,
            "result": True,
        }
        if confirm_window is not None:
            detail.update(confirm_window.as_detail())
        execution = ActionExecution(
            outcome=ExecutionOutcome.APPLIED,
            verification=detail,
            verified_at=verified_at,
        )
        return ExecutionVerification(execution=execution, progress=StepProgress.CHANGED_STATE)

    return _rejected(
        declaration=declaration,
        verified_at=verified_at,
        reason="verification_predicate evaluated false: the action's expected effect was not "
        "confirmed (e.g. the click was swallowed)",
        confirm_window=confirm_window,
        bindings=bindings,
    )


def last_read(predicate: str, bindings: Mapping[str, Any]) -> dict[str, Any]:
    """What the final re-read actually said, expression by expression.

    "Not confirmed" on its own is unreadable in the ledger: ``target in city.production_queue``
    being false is one thing when the queue holds a Monument and quite another when it is ``[]``.
    Each top-level ``and`` operand of *predicate* is evaluated for its *value* rather than its
    truth (:func:`~civsim_harness.act.predicates.evaluate_expression`), and an operand of the form
    ``target in <collection>`` is quoted as the collection itself -- the thing the harness looked
    in -- since ``target`` is already recorded on the decision. An operand that cannot be evaluated
    is recorded as saying so, never omitted.
    """
    readings: dict[str, Any] = {}
    for conjunct in split_conjuncts(predicate):
        expression = membership_collection(conjunct) or conjunct
        try:
            readings[expression] = evaluate_expression(expression, bindings)
        except PredicateEvaluationError as exc:
            readings[expression] = f"<could not be evaluated: {exc.message}>"
    return readings


def _rejected(
    *,
    declaration: ParityDeclaration,
    verified_at: Timestamp,
    reason: str,
    extra_detail: Mapping[str, Any] | None = None,
    confirm_window: ConfirmWindow | None = None,
    bindings: Mapping[str, Any] | None = None,
) -> ExecutionVerification:
    detail: dict[str, Any] = {
        "declaration_id": str(declaration.declaration_id),
        "predicate": declaration.verification_predicate,
        "result": False,
    }
    if extra_detail:
        # Merged before "reason" is set below, so a PredicateEvaluationError's own detail (which
        # carries its own inner "reason" key, e.g. the raw comparison failure) can never clobber
        # this function's own, already-informative wrapped message.
        detail.update(extra_detail)
    if confirm_window is not None:
        detail.update(confirm_window.as_detail())
        if bindings is not None and declaration.verification_predicate is not None:
            detail["last_read"] = last_read(declaration.verification_predicate, bindings)
    detail["reason"] = reason
    execution = ActionExecution(
        outcome=ExecutionOutcome.REJECTED,
        rejection_reason=RejectionReason.VERIFICATION_FAILED,
        verification=detail,
        verified_at=verified_at,
    )
    return ExecutionVerification(execution=execution, progress=StepProgress.REJECTED)


async def confirm_execution[ReadT](
    *,
    declaration: ParityDeclaration,
    pre_observation: Observation,
    first_read: ReadT,
    observation_of: Callable[[ReadT], Observation],
    reobserve: Callable[[], Awaitable[ReadT]],
    clock: Callable[[], Timestamp],
    timeout_s: float,
    poll_s: float,
    target: Any = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    emit: Callable[[str, Mapping[str, Any]], None] = emit_harness_liveness,
) -> tuple[ExecutionVerification, ReadT]:
    """Verify one dispatched action, re-reading until it confirms or *timeout_s* runs out.

    **One bounded re-read, for every action.** The first evaluation is against *first_read* -- the
    observation the run loop already assembled after dispatch, so a client that keeps up costs
    nothing extra. Only if that says "not confirmed" does this re-read, every *poll_s* until
    *timeout_s*, and only the LAST of those readings is recorded. The bound fails closed: an effect
    the game never confirms inside it is still ``verification_failed``, never assumed applied --
    but it is now recorded with the window it was looked for in and with
    :func:`last_read`'s quotation of what the final read said, so "the client was a beat behind"
    and "the game refused the order" stop being the same row in the ledger.

    *first_read* and whatever *reobserve* returns are opaque to this function: the run loop's reads
    carry recovery events alongside the observation, and it needs the bundle that belongs to the
    reading actually recorded, not just its ``Observation``. *observation_of* projects one out.
    *sleep* and *monotonic* are injectable so a test can drive the whole window without waiting.

    Returns the verification and the read it was derived from -- always the last one taken, so the
    caller records the observation it actually verified against.
    """
    started = monotonic()
    deadline = started + timeout_s
    read = first_read
    attempts = 0

    while True:
        attempts += 1
        window = ConfirmWindow(
            timeout_s=timeout_s,
            poll_s=poll_s,
            attempts=attempts,
            elapsed_s=monotonic() - started,
        )
        # T290: this is the single longest legitimate operation in the harness --
        # `run/decision_loop.py::END_TURN_CONFIRM_TIMEOUT_S` and
        # `run/turn_cycle.py::BACKSTOP_CONFIRM_TIMEOUT_S` are both 200 s, because an end turn
        # is confirmed only once every AI player has taken theirs. Until now it polled every
        # two seconds and said nothing, so under a watchdog that reads three minutes of
        # silence as stuck, the first healthy thing killed would be this loop, during exactly
        # the slow turn its raised bound exists to accommodate. The bound is untouched; the
        # wait is now readable. See `act/liveness.py` for what is and is not in the record.
        emit_confirm_liveness(declaration=declaration, window=window, emit=emit)
        verification = verify_execution(
            declaration=declaration,
            pre_observation=pre_observation,
            post_observation=observation_of(read),
            target=target,
            verified_at=clock(),
            confirm_window=window,
        )
        if verification.execution.outcome is ExecutionOutcome.APPLIED:
            return verification, read
        if monotonic() >= deadline:
            return verification, read
        await sleep(poll_s)
        read = await reobserve()
