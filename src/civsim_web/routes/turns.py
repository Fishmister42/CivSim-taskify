"""Turn and step routes (T027, extended by T036 and T043).

``GET /runs/{run_id}/turns/{turn_number}``    -> ``TurnCycleView``
``GET /runs/{run_id}/turns/{n}/steps/{i}``    -> ``DecisionStepView``

**This file is edited by three tasks in three stories, in this order**, and is
laid out so that each is an addition rather than a rewrite. Read this before
editing it:

- **T027 (US1, done)** -- the two routes above, authoritative attempt, full step
  list. The store reads are composed in ``_load_turn``; the view is built by
  ``viewmodels/turn.build_turn_cycle_view``; the handler is the thin part.
- **T036 (US2)** -- ``?attempt={n}``. Add the query parameter to ``get_turn``,
  pass it to ``_load_turn`` (which already takes ``attempt``), and pass
  ``superseded_by=`` through to the builder. ``TurnCycleView`` already carries
  ``is_authoritative`` and ``superseded_by``; no view-model change is needed.
  **Do not** let a superseded attempt fall through to the 404 path: FR-009 wants
  it to explain itself, not look like a dead link.
- **T043 (US3)** -- step-window pagination. Add ``?step_offset=`` /
  ``?step_limit=`` to ``get_turn`` and pass them straight through; the builder
  already accepts them and already reports ``StepWindow.skipped_step_indices``,
  which is data-model.md SS5's "no page may skip an index without marking it".

Three failure shapes are distinguished deliberately, because contracts/
web-read-api.md and FR-016 both depend on a client being able to tell them
apart:

| Condition                                   | Response                        |
|---------------------------------------------|---------------------------------|
| the run never existed                        | ``404 run_not_found``           |
| the turn is outside the run's recorded range | ``404 turn_not_found``, naming the range |
| the turn is a recorded **gap**               | ``404 turn_gap``, naming the gap |

A gap is not the same as a turn that never happened, and collapsing the two
would let a client mistake an incomplete record for a boring, complete turn --
exactly what FR-016 and data-model.md SS5 forbid.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from civsim_web.negotiate.respond import respond
from civsim_web.routes.common import ErrorView, RunContext, WebError, load_run_context
from civsim_web.store_client import reads
from civsim_web.viewmodels.step import DecisionStepView
from civsim_web.viewmodels.turn import TurnCycleView, TurnIsGap, build_turn_cycle_view

__all__ = ["build_step_view", "build_turn_view", "router"]

router = APIRouter()

TURN_TEMPLATE = "turns/turn.html"
STEP_TEMPLATE = "turns/step.html"


@router.get("/runs/{run_id}/turns/{turn_number}")
def get_turn(request: Request, run_id: str, turn_number: int) -> Response:
    """One turn's full record (FR-014).

    T036 adds ``attempt: int | None = None`` here; T043 adds the step window.
    Both forward to ``build_turn_view``, which already accepts them.
    """
    context = load_run_context(request, run_id, with_events=False)
    view = build_turn_view(context, turn_number)
    return respond(request, view, TURN_TEMPLATE)


@router.get("/runs/{run_id}/turns/{turn_number}/steps/{step_index}")
def get_step(request: Request, run_id: str, turn_number: int, step_index: int) -> Response:
    """One decision step: what was seen, what was decided, and what it cost."""
    context = load_run_context(request, run_id, with_events=False)
    view = build_step_view(context, turn_number, step_index)
    return respond(request, view, STEP_TEMPLATE)


# --------------------------------------------------------------------------
# Builders -- the part US2 and US3 extend
# --------------------------------------------------------------------------


def build_turn_view(
    context: RunContext,
    turn_number: int,
    *,
    attempt: int | None = None,
    superseded_by: int | None = None,
    step_offset: int = 0,
    step_limit: int | None = None,
) -> TurnCycleView:
    """Build one turn's view model, or raise the ``WebError`` that explains why not.

    ``attempt`` is accepted now and honoured by ``_load_turn`` so T036's change
    is a query-parameter addition rather than a signature change here.
    """
    record, gaps = _load_turn(context, turn_number, attempt=attempt)
    try:
        return build_turn_cycle_view(
            record,
            registry=context.registry,
            provenance=context.provenance,
            turn_gaps=gaps,
            step_gaps=tuple(context.store.step_gaps(context.run_id, turn_number) or ()),
            captures=reads.capture_records_for_turn(context.store, record),
            superseded_by=superseded_by,
            step_offset=step_offset,
            step_limit=step_limit,
        )
    except TurnIsGap as exc:
        raise _gap_error(context, turn_number, exc.gaps) from exc


def build_step_view(context: RunContext, turn_number: int, step_index: int) -> DecisionStepView:
    """One step of one turn, or the ``WebError`` explaining its absence.

    Built by selecting from the turn's own view rather than by a second
    construction path, so a step opened directly and the same step opened inside
    its turn are the identical object (invariant V8).
    """
    turn = build_turn_view(context, turn_number)
    for step in turn.steps:
        if step.step_index == step_index:
            return step
    raise WebError(
        404,
        ErrorView(
            kind="step_not_found",
            message=(
                f"Turn {turn_number} has no step {step_index}. Recorded steps: "
                f"{[s.step_index for s in turn.steps] or 'none'}."
                + (
                    f" Steps marked missing for this turn: "
                    f"{list(turn.completeness.missing_step_indices)}."
                    if turn.completeness.missing_step_indices
                    else ""
                )
            ),
            run_id=context.run_id,
            turn_number=turn_number,
            detail={
                "step_index": step_index,
                "recorded_step_indices": [s.step_index for s in turn.steps],
                "missing_step_indices": list(turn.completeness.missing_step_indices),
            },
        ),
    )


# --------------------------------------------------------------------------
# Store reads and the three failure shapes
# --------------------------------------------------------------------------


def _load_turn(
    context: RunContext, turn_number: int, *, attempt: int | None = None
) -> tuple[Any, tuple[int, ...]]:
    """``(record, turn_gaps)`` for one turn, or the ``WebError`` explaining why not.

    ``attempt is not None`` reads with ``authoritative_only=False``, which is how
    an abandoned attempt stays retrievable for FR-009 (T036 supplies the
    parameter; the read path is already here so that change stays small).
    """
    gaps = reads.turn_gaps(context.store, context.run_id)
    if turn_number in gaps:
        raise _gap_error(context, turn_number, gaps)

    record = reads.turn_record(
        context.store,
        context.run_id,
        turn_number,
        authoritative_only=attempt is None,
    )
    if record is None:
        raise _out_of_range_error(context, turn_number, gaps)
    return (record, gaps)


def _turn_range(context: RunContext) -> tuple[int, int]:
    highest = reads.highest_recorded_turn(context.store, context.run_id)
    return (1 if highest else 0, highest)


def _gap_error(context: RunContext, turn_number: int, gaps: tuple[int, ...]) -> WebError:
    """FR-016: a gap is marked, never silently skipped or shown as complete."""
    low, high = _turn_range(context)
    return WebError(
        404,
        ErrorView(
            kind="turn_gap",
            message=(
                f"Turn {turn_number} is inside this run's recorded range "
                f"({low}-{high}) but has no authoritative attempt: the harness "
                f"recorded it as a gap. The run's turn-by-turn record is "
                f"incomplete and it is unfit for trend comparison."
            ),
            run_id=context.run_id,
            turn_number=turn_number,
            detail={"turn_gaps": list(gaps), "turn_range": [low, high]},
        ),
    )


def _out_of_range_error(
    context: RunContext, turn_number: int, gaps: tuple[int, ...]
) -> WebError:
    low, high = _turn_range(context)
    return WebError(
        404,
        ErrorView(
            kind="turn_not_found",
            message=(
                f"Run {context.run_id} has no turn {turn_number}. Recorded turns: "
                f"{low}-{high}." if high else
                f"Run {context.run_id} has no turns recorded yet."
            ),
            run_id=context.run_id,
            turn_number=turn_number,
            detail={"turn_range": [low, high], "turn_gaps": list(gaps)},
        ),
    )
