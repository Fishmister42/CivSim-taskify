"""``TurnCycleView`` and ``TurnCompleteness`` (T020, data-model.md SS5).

One rule from data-model.md SS5 is enforced structurally here, verbatim:

    A ``TurnCycleView`` for a turn number present in ``turn_gaps()`` is never
    constructed as if it were a normal turn

so ``build_turn_cycle_view`` raises ``TurnIsGap`` rather than returning a view
when asked to build one for a gapped turn. The route turns that into the
explicit "turn not recorded / gap" response FR-016 requires. The check lives in
the constructor and not only in the route because a route is a place someone can
forget; a constructor that refuses is a place they cannot.

**Extension points**, named so the two later stories that edit this file and its
route do not have to reverse-engineer them:

- **T036 (US2, done)** added ``?attempt={n}``. It needed ``is_authoritative``
  and ``superseded_by``, both already on the model and both already populated
  from the record -- so T036 was a *route* change (read a different attempt)
  plus passing ``superseded_by=`` here, and **no model change**. The one
  addition to this file was ``allow_gap=``, which is what lets a reference to a
  turn whose every attempt was abandoned answer with the attempt rather than
  with the bare turn's gap 404; see ``build_turn_cycle_view``.
- **T043 (US3)** adds step-window pagination. ``build_turn_cycle_view`` already
  takes ``steps`` as an explicit sequence the caller selected, and
  ``StepWindow`` below is the shape the page metadata goes in; the ordering
  guarantee data-model.md SS5 demands ("no page may skip an index without
  marking it") is expressed as ``StepWindow.skipped_step_indices``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from civsim_web.refs.reference import ViewReference
from civsim_web.registry.loader import PanelRegistry
from civsim_web.viewmodels.base import UnavailableField, ViewModel
from civsim_web.viewmodels.gate import GatedReader
from civsim_web.viewmodels.provenance import Provenance
from civsim_web.viewmodels.step import DecisionStepView, build_decision_step_view

__all__ = [
    "StepWindow",
    "TurnCompleteness",
    "TurnCycleView",
    "TurnIsGap",
    "build_turn_cycle_view",
    "select_step_window",
]


class TurnIsGap(LookupError):
    """Asked to render a turn number 002 has recorded as a gap.

    Distinct from "this turn never existed": a gap is a turn number *inside* a
    run's recorded range with no authoritative attempt, which FR-016 requires be
    marked rather than silently skipped. The route renders the distinction.
    """

    def __init__(self, run_id: str, turn_number: int, gaps: Sequence[int]) -> None:
        super().__init__(
            f"turn {turn_number} of run {run_id!r} is a recorded gap "
            f"(turn_gaps: {list(gaps)})"
        )
        self.run_id = run_id
        self.turn_number = turn_number
        self.gaps = tuple(gaps)


class TurnCompleteness(ViewModel):
    """Whether this turn's record is whole (data-model.md SS5, invariant V4)."""

    is_complete: bool = True
    is_gap: bool = False
    missing_step_indices: tuple[int, ...] = ()
    """Verbatim from ``step_gaps`` -- never re-derived here (invariant V5)."""


class StepWindow(ViewModel):
    """Which steps this response carries, when it does not carry all of them.

    Present unconditionally so a reader never has to infer whether they are
    looking at a window or the whole turn. US1 always returns the full ordered
    list; T043 (US3) narrows it and sets ``has_more``.
    """

    offset: int = 0
    limit: int | None = None
    returned: int = 0
    total: int = 0
    has_more: bool = False
    skipped_step_indices: tuple[int, ...] = ()
    """Indices this page deliberately omitted. data-model.md SS5: "no page may
    skip an index without marking it, which would look identical to a genuine
    gap and must not"."""


class TurnCycleView(ViewModel):
    """One turn attempt, unpacked into its ordered decision steps."""

    run_id: str
    turn_number: int
    reference: str
    attempt_index: int = 0
    is_authoritative: bool = True
    outcome: str | None = None
    """``ended_by_agent`` | ``ended_on_no_progress`` | ``abandoned`` --
    verbatim, never relabelled."""

    steps: tuple[DecisionStepView, ...] = ()
    step_window: StepWindow = StepWindow()
    step_count: int | None = None
    yields: dict[str, Any] = {}
    started_at: datetime | None = None
    ended_at: datetime | None = None
    completeness: TurnCompleteness = TurnCompleteness()
    superseded_by: int | None = None
    """The attempt index that superseded this one; set by T036 (US2) when a
    reference names a specific, since-superseded attempt (FR-009)."""

    focus_panel_id: str | None = None
    """Which panel the replay view is focused on (T044, FR-015).

    **Why this is on the response and not a browser-side detail.** FR-015 wants
    stepping and jumping between turns to preserve the current panel focus, and
    the honest way to carry that across a plain ``<a href>`` navigation is in
    the URL (``?focus=``) -- there is no control on this page and no client-side
    state to keep. Once focus is in the URL, Principle VI settles where it
    belongs: the directing session resolving the identical reference must see
    the same page the user is looking at, focus included. A focus the browser
    rendered and the JSON did not mention would be exactly the asymmetry
    Principle VI forbids, in miniature.

    ``None`` is "no panel focused", which is what a bare turn reference means
    and must keep meaning."""

    provenance: Provenance
    unavailable: tuple[UnavailableField, ...] = ()


def build_turn_cycle_view(
    record: Any,
    *,
    registry: PanelRegistry,
    provenance: Provenance,
    turn_gaps: Sequence[int] = (),
    step_gaps: Sequence[int] = (),
    captures: dict[str, Any] | None = None,
    superseded_by: int | None = None,
    step_offset: int = 0,
    step_limit: int | None = None,
    allow_gap: bool = False,
    focus_panel_id: str | None = None,
) -> TurnCycleView:
    """Project one of 002's ``TurnCycleRecord``s.

    ``captures`` maps ``decision_step_id`` to the already-read ``ScreenCapture``
    record, because store reads belong at the route layer. A step whose capture
    id is absent from the mapping renders as unavailable rather than as missing
    markup (FR-034).

    ``allow_gap`` is US2's addition (T036) and is the only way this constructor
    will build a view for a gapped turn. The default refusal stands: a turn
    number in ``turn_gaps()`` has no authoritative attempt, so the *bare* turn
    reference must answer with the explicit gap response FR-016 requires. But a
    reference that names a specific attempt (``?attempt=k``) is asking for an
    attempt, not for the authoritative record -- and a turn whose every attempt
    was abandoned is exactly the "branch abandoned or rolled back" case FR-009
    says must explain itself rather than look like a dead link. Such a view is
    built with ``completeness.is_gap = true`` and ``is_complete = false``, so
    invariant V4 ("never rendered as if it were a normal, complete turn") holds
    on the response rather than by the constructor refusing to exist.
    """
    cycle = getattr(record, "turn_cycle", record)
    gate = GatedReader(registry, "TurnCycle", cycle)

    run_id = str(gate.get("run_id", default="") or "")
    turn_number = int(gate.get("turn_number", default=0) or 0)

    is_gap = turn_number in tuple(turn_gaps)
    if is_gap and not allow_gap:
        raise TurnIsGap(run_id, turn_number, turn_gaps)

    all_steps = tuple(getattr(record, "steps", ()) or ())
    selected, window = select_step_window(all_steps, step_offset, step_limit)

    lookup = captures or {}
    steps = tuple(
        build_decision_step_view(
            bundle,
            registry=registry,
            run_id=run_id,
            turn_number=turn_number,
            capture=lookup.get(_capture_id_for(bundle)),
            capture_expected=bool(getattr(getattr(bundle, "observation", None), "captures", ())),
        )
        for bundle in selected
    )

    missing_steps = tuple(int(n) for n in step_gaps)
    return TurnCycleView(
        run_id=run_id,
        turn_number=turn_number,
        reference=ViewReference(run_id=run_id, turn_number=turn_number).path,
        attempt_index=int(gate.get("attempt_index", default=0) or 0),
        is_authoritative=bool(gate.get("is_authoritative", default=False)),
        outcome=gate.text("outcome"),
        steps=steps,
        step_window=window,
        step_count=gate.get("step_count"),
        yields=gate.mapping("yields"),
        started_at=gate.get("started_at"),
        ended_at=gate.get("ended_at"),
        completeness=TurnCompleteness(
            is_complete=not missing_steps and not is_gap,
            is_gap=is_gap,
            missing_step_indices=missing_steps,
        ),
        superseded_by=superseded_by,
        focus_panel_id=focus_panel_id,
        provenance=provenance,
        unavailable=gate.missing,
    )


def _capture_id_for(bundle: Any) -> str:
    """The first capture id this step's observation declared, if any.

    One capture per turn is the documented baseline (spec Assumptions); a step
    declaring several is handled by showing the first, with the rest reachable
    at their own ``/captures/{id}/image`` route.
    """
    observation = getattr(bundle, "observation", None)
    declared = tuple(getattr(observation, "captures", ()) or ()) if observation else ()
    return str(declared[0]) if declared else ""


def select_step_window(
    steps: Sequence[Any], offset: int, limit: int | None
) -> tuple[tuple[Any, ...], StepWindow]:
    """Select the requested step window and describe it honestly.

    Ordering is by ``step_index``, always -- the guarantee data-model.md SS5
    says is "preserved regardless of pagination".

    **Public because the turn route needs the same answer before this module
    runs** (T067). FR-036 requires that viewing a turn not load captures beyond
    the ones being viewed, and the route reads capture records *before* calling
    ``build_turn_cycle_view``. Having the route call this same pure function --
    rather than reimplementing "first N by step index" -- is what makes the
    steps whose captures were read and the steps that render provably the same
    set. A second, hand-rolled slice in the route is exactly how the two would
    drift into disagreeing.
    """
    ordered = tuple(sorted(steps, key=lambda b: getattr(getattr(b, "step", b), "step_index", 0)))
    total = len(ordered)
    start = max(0, offset)
    selected = ordered[start:] if limit is None else ordered[start : start + max(0, limit)]
    chosen = {getattr(getattr(b, "step", b), "step_index", 0) for b in selected}
    skipped = tuple(
        sorted(
            getattr(getattr(b, "step", b), "step_index", 0)
            for b in ordered
            if getattr(getattr(b, "step", b), "step_index", 0) not in chosen
        )
    )
    return (
        selected,
        StepWindow(
            offset=start,
            limit=limit,
            returned=len(selected),
            total=total,
            has_more=start + len(selected) < total,
            skipped_step_indices=skipped,
        ),
    )
