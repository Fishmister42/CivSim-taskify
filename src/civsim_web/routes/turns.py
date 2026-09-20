"""Turn and step routes (T027, extended by T036 and T043).

``GET /runs/{run_id}/turns/{turn_number}``    -> ``TurnCycleView``
``GET /runs/{run_id}/turns/{n}/steps/{i}``    -> ``DecisionStepView``

**This file is edited by three tasks in three stories, in this order**, and is
laid out so that each is an addition rather than a rewrite. Read this before
editing it:

- **T027 (US1, done)** -- the two routes above, authoritative attempt, full step
  list. The store reads are composed in ``_load_turn``; the view is built by
  ``viewmodels/turn.build_turn_cycle_view``; the handler is the thin part.
- **T036 (US2, done)** -- ``?attempt={n}``. A query parameter on ``get_turn``,
  forwarded to ``build_turn_view``, which was already shaped to take it. No
  view-model change was needed: ``TurnCycleView`` already carried
  ``is_authoritative`` and ``superseded_by``, and the latter is now *computed*
  from the authoritative attempt rather than supplied by the caller. The store
  read lives in ``_load_attempt`` / ``store_client.reads.turn_attempt``.
- **T043 (US3, done)** -- step-window pagination. ``?step_offset=`` /
  ``?step_limit=`` on ``get_turn``, passed straight through; the builder already
  accepted them and already reported ``StepWindow.skipped_step_indices``, which
  is data-model.md SS5's "no page may skip an index without marking it". They
  are on ``get_turn``'s signature **only** -- ``_load_turn`` and ``_load_attempt``
  needed no change, and ``build_step_view`` deliberately calls
  ``build_turn_view`` with the *full* step list so a step opened directly is
  never missing because of someone else's page size.

Six failure shapes are distinguished deliberately, because contracts/
web-read-api.md, FR-009 and FR-016 all depend on a client being able to tell
them apart:

| Condition                                    | Response                          |
|----------------------------------------------|-----------------------------------|
| the run never existed                         | ``404 run_not_found``            |
| the turn is outside the recorded range        | ``404 turn_not_found`` + range   |
| the turn is a recorded **gap**                | ``404 turn_gap`` + the gap        |
| ``?attempt=k`` names an unrecorded attempt    | ``404 attempt_not_found``         |
| ``?attempt=k`` names an unreachable attempt   | ``404 attempt_not_addressable``   |
| ``?step_offset=`` is past the last step       | ``404 step_window_out_of_range``  |

A gap is not the same as a turn that never happened, and collapsing the two
would let a client mistake an incomplete record for a boring, complete turn --
exactly what FR-016 and data-model.md SS5 forbid. The attempt pair are likewise
different facts: one is about the run, the other about the store, and the
published port cannot address an arbitrary attempt at all (see
``store_client/port.py`` ``TurnAttemptReader``).

The last one is T043's own, and it is a ``404`` rather than an empty page for
the same reason: a page of no steps for a turn that has steps is indistinguishable
from a turn whose steps were never recorded, and SS5 spends its whole Validation
clause on those two not being confusable. ``?step_offset=0`` on a turn with no
recorded steps is *not* this error -- that turn genuinely has nothing to page
through, and its ``step_gaps`` say whether that is a hole or a fact.

**What never happens on any of those paths**: the current authoritative turn is
never substituted for a superseded attempt someone asked for by name. That is
the single outcome FR-009 rules out, and it is why ``?attempt=`` resolves
through its own read rather than falling back to ``get_turn_cycle``'s default.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from civsim_web.negotiate.respond import respond
from civsim_web.refs.reference import ViewReference
from civsim_web.routes.common import ErrorView, RunContext, WebError, load_run_context
from civsim_web.store_client import reads
from civsim_web.viewmodels.step import DecisionStepView
from civsim_web.viewmodels.turn import TurnCycleView, TurnIsGap, build_turn_cycle_view

__all__ = ["build_step_view", "build_turn_view", "router"]

router = APIRouter()

TURN_TEMPLATE = "history/turn_replay.html"
STEP_TEMPLATE = "turns/step.html"

#: Upper bound on one step page. A late-game turn of hundreds of steps is a
#: valid turn (002 data-model.md SS5, validation 5), so the cap exists to bound
#: one request rather than to bound a turn.
MAX_STEP_PAGE_SIZE = 500

#: The window a caller gets without asking (T060, data-model.md SS5).
#:
#: This was ``None`` -- the whole ordered list -- until Phase 7's scale test
#: measured a 200-step turn and found the default response carrying all 200.
#: SS5 is explicit that "``steps`` on the default response is a bounded window
#: (e.g., first N and a cursor), with the full ordered list available by
#: paging", and research R6 chose lazy step loading for exactly this case.
#:
#: The rationale recorded for the unbounded default was that a window "would
#: make every turn look shorter than it is". ``StepWindow`` is what answers
#: that: it carries ``total``, ``has_more`` and ``skipped_step_indices``, so a
#: bounded page states its own bounds and SS5's "no page may skip an index
#: without marking it" holds by construction. A turn never looks shorter than
#: it is; it looks exactly as long as it is, with fewer of its steps inlined.
#:
#: ``build_step_view`` still builds its enclosing turn with the *full* list
#: (``step_limit=None``), so a step opened directly is never missing because of
#: someone else's page size.
DEFAULT_STEP_PAGE_SIZE = 50


@router.get("/runs/{run_id}/turns/{turn_number}")
def get_turn(
    request: Request,
    run_id: str,
    turn_number: int,
    attempt: int | None = Query(default=None, ge=0),
    step_offset: int = Query(default=0, ge=0),
    step_limit: int = Query(default=DEFAULT_STEP_PAGE_SIZE, ge=1, le=MAX_STEP_PAGE_SIZE),
    focus: str | None = Query(default=None),
) -> Response:
    """One turn's full record (FR-014), or one named attempt of it (FR-009).

    With no ``?attempt=``, this is the current authoritative attempt -- what a
    bare turn reference has always meant and must keep meaning. With one, it is
    that exact attempt, abandoned or not, carrying ``is_authoritative=false``
    and a ``superseded_by`` pointer when it has been overtaken.

    ``?step_offset=`` / ``?step_limit=`` are T043's step window (research R6,
    data-model.md SS5). The default window is ``DEFAULT_STEP_PAGE_SIZE`` steps,
    not the whole list -- see that constant for why it changed in Phase 7.
    Whatever the window, ``step_window`` carries ``total`` and ``has_more``, and
    ``step_window.skipped_step_indices`` names every index this page left out,
    so a page boundary can never be mistaken for a record gap.

    ``?focus=`` is T044's panel focus, carried in the URL so stepping and
    jumping preserve it across a plain ``<a href>`` (FR-015) and so the
    directing session resolving the identical reference sees the identical page
    (Principle VI). An unregistered ``panel_id`` is refused rather than ignored:
    a focus parameter that quietly did nothing would leave the user looking at
    an unfocused page they believe is focused.
    """
    context = load_run_context(request, run_id, with_events=False)
    view = build_turn_view(
        context,
        turn_number,
        attempt=attempt,
        step_offset=step_offset,
        step_limit=step_limit,
        focus_panel_id=_resolve_focus(context, focus),
    )
    return respond(request, view, TURN_TEMPLATE)


@router.get("/runs/{run_id}/turns/{turn_number}/steps/{step_index}")
def get_step(
    request: Request,
    run_id: str,
    turn_number: int,
    step_index: int,
    attempt: int | None = Query(default=None, ge=0),
) -> Response:
    """One decision step: what was seen, what was decided, and what it cost.

    ``?attempt=`` carries here too: a step of an abandoned attempt is a real
    record someone can hold a reference to, and it must resolve to that attempt's
    step rather than to the authoritative attempt's step of the same index
    (FR-009 at step grain).
    """
    context = load_run_context(request, run_id, with_events=False)
    view = build_step_view(context, turn_number, step_index, attempt=attempt)
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
    focus_panel_id: str | None = None,
) -> TurnCycleView:
    """Build one turn's view model, or raise the ``WebError`` that explains why not.

    ``attempt is None`` is the authoritative attempt -- the meaning a bare turn
    reference has always had. Naming an attempt switches to the attempt-addressed
    read and computes ``superseded_by`` from whichever attempt is authoritative
    now, so a reference someone shared five minutes ago still explains itself
    (FR-009). An explicit ``superseded_by=`` still wins, for a caller that has
    already resolved it.
    """
    if attempt is None:
        record, gaps = _load_turn(context, turn_number)
        computed_superseded_by: int | None = None
    else:
        record, gaps, computed_superseded_by = _load_attempt(context, turn_number, attempt)

    try:
        view = build_turn_cycle_view(
            record,
            registry=context.registry,
            provenance=context.provenance,
            turn_gaps=gaps,
            step_gaps=tuple(context.store.step_gaps(context.run_id, turn_number) or ()),
            captures=reads.capture_records_for_turn(context.store, record),
            superseded_by=(
                superseded_by if superseded_by is not None else computed_superseded_by
            ),
            step_offset=step_offset,
            step_limit=step_limit,
            focus_panel_id=focus_panel_id,
            # A named attempt on a turn whose every attempt was abandoned is a
            # real reference to a real record; it renders marked as a gap rather
            # than answering as a dead link (FR-009 over FR-016's 404).
            allow_gap=attempt is not None,
        )
    except TurnIsGap as exc:
        raise _gap_error(context, turn_number, exc.gaps) from exc

    window = view.step_window
    if window.returned == 0 and window.total > 0 and window.offset > 0:
        raise _step_window_error(context, turn_number, window)
    return view


def build_step_view(
    context: RunContext, turn_number: int, step_index: int, *, attempt: int | None = None
) -> DecisionStepView:
    """One step of one turn, or the ``WebError`` explaining its absence.

    Built by selecting from the turn's own view rather than by a second
    construction path, so a step opened directly and the same step opened inside
    its turn are the identical object (invariant V8).
    """
    turn = build_turn_view(context, turn_number, attempt=attempt)
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


def _resolve_focus(context: RunContext, focus: str | None) -> str | None:
    """Validate ``?focus=`` against the Panel Registry (T044).

    An unregistered ``panel_id`` is not a valid reference at all
    (contracts/web-read-api.md's error table says so for the panel route), and
    it is not a valid focus either. Answering ``400`` rather than dropping it
    keeps this page consistent with ``GET /runs``'s unknown-filter rule: a
    parameter that is silently ignored produces a page that looks like it obeyed
    one.
    """
    if focus is None:
        return None
    if context.registry.get(focus) is None:
        raise WebError(
            400,
            ErrorView(
                kind="unknown_focus_panel",
                message=(
                    f"No Panel Registry entry named {focus!r}, so there is nothing "
                    f"to focus. An unregistered panel_id is a reference error, not "
                    f"a field-unavailable case."
                ),
                run_id=context.run_id,
                detail={"focus": focus},
            ),
        )
    return focus


# --------------------------------------------------------------------------
# Store reads and the three failure shapes
# --------------------------------------------------------------------------


def _load_turn(context: RunContext, turn_number: int) -> tuple[Any, tuple[int, ...]]:
    """``(record, turn_gaps)`` for a turn's authoritative attempt.

    Raises the ``WebError`` explaining a gap or an out-of-range turn. This is
    the bare-reference path and it is deliberately strict: with no attempt
    named, a turn with no authoritative record is a gap, full stop (FR-016).
    """
    gaps = reads.turn_gaps(context.store, context.run_id)
    if turn_number in gaps:
        raise _gap_error(context, turn_number, gaps)

    record = reads.turn_record(context.store, context.run_id, turn_number)
    if record is None:
        raise _out_of_range_error(context, turn_number, gaps)
    return (record, gaps)


def _load_attempt(
    context: RunContext, turn_number: int, attempt: int
) -> tuple[Any, tuple[int, ...], int | None]:
    """``(record, turn_gaps, superseded_by)`` for one named attempt (FR-009).

    Never substitutes. If the named attempt cannot be produced, the failure says
    which of the two reasons applies -- nobody recorded it, or this store cannot
    address it -- and neither answer is the authoritative turn.
    """
    gaps = reads.turn_gaps(context.store, context.run_id)
    lookup = reads.turn_attempt(context.store, context.run_id, turn_number, attempt)

    if lookup.record is None:
        raise _attempt_error(context, turn_number, attempt, lookup, gaps)

    authoritative = reads.turn_record(context.store, context.run_id, turn_number)
    superseded_by: int | None = None
    if authoritative is not None:
        authoritative_index = reads.attempt_index_of(authoritative)
        if authoritative_index != reads.attempt_index_of(lookup.record):
            superseded_by = authoritative_index
    return (lookup.record, gaps, superseded_by)


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


def _step_window_error(context: RunContext, turn_number: int, window: Any) -> WebError:
    """T043: a step page past the end says so, rather than looking empty.

    data-model.md SS5 spends its whole lazy-loading clause on one distinction --
    a page that skipped an index must never look like a genuine gap. The
    converse belongs here: a window starting past the last recorded step is a
    page that does not exist, and answering it with an empty step list would
    render as a turn whose steps were never recorded.
    """
    return WebError(
        404,
        ErrorView(
            kind="step_window_out_of_range",
            message=(
                f"Turn {turn_number} has {window.total} recorded step(s); "
                f"step_offset={window.offset} starts past the last of them. This "
                f"is a page that does not exist, not a turn with nothing recorded "
                f"-- the two are answered differently on purpose."
            ),
            run_id=context.run_id,
            turn_number=turn_number,
            detail={
                "step_offset": window.offset,
                "step_limit": window.limit,
                "step_count": window.total,
                "first_page": ViewReference(
                    run_id=context.run_id, turn_number=turn_number
                ).path,
            },
        ),
    )


def _attempt_error(
    context: RunContext,
    turn_number: int,
    attempt: int,
    lookup: Any,
    gaps: tuple[int, ...],
) -> WebError:
    """Why a named attempt could not be produced -- about the run, or the store.

    Both are ``404``: the reference names something this server cannot serve.
    What differs is ``kind``, because FR-009's whole point is that a reference
    must explain itself, and "nobody recorded attempt 7" and "the published port
    cannot address attempt 0" are different explanations a client may want to act
    on differently.
    """
    low, high = _turn_range(context)
    authoritative = reads.turn_record(context.store, context.run_id, turn_number)
    authoritative_reference = ViewReference(
        run_id=context.run_id, turn_number=turn_number
    ).path
    detail: dict[str, Any] = {
        "attempt": attempt,
        "turn_range": [low, high],
        "turn_gaps": list(gaps),
        "authoritative_reference": authoritative_reference,
        "authoritative_attempt": (
            reads.attempt_index_of(authoritative) if authoritative is not None else None
        ),
    }

    if lookup.exhaustive and lookup.addressable:
        detail["addressable_attempts"] = list(lookup.addressable)
        return WebError(
            404,
            ErrorView(
                kind="attempt_not_addressable",
                message=(
                    f"Attempt {attempt} of turn {turn_number} cannot be reached "
                    f"through the published MatchStore port. `get_turn_cycle` "
                    f"selects by a boolean, not by an attempt index, so this "
                    f"store can address only attempts "
                    f"{list(lookup.addressable)} of this turn. The authoritative "
                    f"attempt is at {authoritative_reference} -- it is named "
                    f"here, not substituted, because a reference to a specific "
                    f"attempt must never quietly resolve to a different one "
                    f"(FR-009)."
                ),
                run_id=context.run_id,
                turn_number=turn_number,
                detail=detail,
            ),
        )

    return WebError(
        404,
        ErrorView(
            kind="attempt_not_found",
            message=(
                f"Turn {turn_number} of run {context.run_id} has no attempt "
                f"{attempt} recorded. The authoritative attempt is at "
                f"{authoritative_reference}."
                if authoritative is not None
                else (
                    f"Turn {turn_number} of run {context.run_id} has no attempt "
                    f"{attempt} recorded, and no authoritative attempt either. "
                    f"Recorded turns: {low}-{high}."
                )
            ),
            run_id=context.run_id,
            turn_number=turn_number,
            detail=detail,
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
