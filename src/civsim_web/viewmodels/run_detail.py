"""``RunDetailView`` -- the glance (T025, data-model.md SS3).

UP-003 in one model: *"The landing view answers 'what is happening right now and
is it healthy' without interaction; all detail is reachable by drilling down
from it, never required to reach that answer."* So ``latest_decision`` is a
convenience pointer to the current turn's last decision, present precisely so
FR-001's "single landing view, no navigation required" does not require drilling
into the turn to find what the agent just did.

Three things this model is careful about:

- **``current_turn = None`` is a state, not a gap in the page.** A run opened
  before its first turn is recorded renders an explicit empty state, never a
  blank panel or a zero-filled turn (data-model.md SS3 Validation, spec Edge
  Cases). ``empty_state_reason`` carries that explanation into the JSON body
  too, so the directing session reads the same sentence the user does -- an
  empty state visible to only one of them would be an asymmetry (Principle VI).
- **``last_confirmed_current_at`` is when *this response* was assembled**, which
  is the FR-002 currency indicator and deliberately distinct from
  ``HealthStatus`` (research R7). "The view was current at 12:04:31" and "the
  run is healthy" answer different questions, and collapsing them would let a
  stale page look healthy.
- **``intervention_info`` is always present, never conditional** (FR-027) and
  carries no control (invariant V9, enforced by the type in
  ``viewmodels/base.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from civsim_web.registry.loader import PanelRegistry
from civsim_web.viewmodels.base import (
    InterventionInfo,
    PanelRegistryVersion,
    RunSummaryView,
    ViewModel,
)
from civsim_web.viewmodels.decision import DecisionView
from civsim_web.viewmodels.event import RunEventView
from civsim_web.viewmodels.gate import GatedReader
from civsim_web.viewmodels.turn import TurnCycleView

__all__ = [
    "NO_RUNS_RECORDED",
    "NO_TURNS_RECORDED",
    "LandingView",
    "RunDetailView",
    "build_intervention_info",
    "build_run_detail_view",
    "latest_decision_of",
    "utcnow",
]

#: data-model.md SS3 Validation, said in the words the page shows.
NO_TURNS_RECORDED = (
    "No turns have been recorded for this run yet. The run exists and is being "
    "tracked; nothing has been written to its turn record so far."
)

NO_RUNS_RECORDED = (
    "No runs are active. Nothing is being tracked right now, which is different "
    "from a run being stalled -- a stalled run would still be listed here."
)


def utcnow() -> datetime:
    """The currency stamp's clock, in one place so tests can reason about it.

    Nothing in this feature *decides* anything from a clock (invariant V3,
    research R7). This timestamp is reported, never compared.
    """
    return datetime.now(UTC)


class RunDetailView(ViewModel):
    """One run, at a glance (FR-001, FR-002, FR-006, UP-003)."""

    summary: RunSummaryView
    current_turn: TurnCycleView | None = None
    latest_decision: DecisionView | None = None
    last_confirmed_current_at: datetime
    intervention_info: InterventionInfo
    recent_events: tuple[RunEventView, ...] = ()
    empty_state_reason: str | None = None
    panel_registry: PanelRegistryVersion


class LandingView(ViewModel):
    """``GET /`` when it cannot resolve to a single run.

    The contract makes ``GET /`` a redirect to whichever run is currently
    active. Two cases have nowhere to redirect *to*: several runs are active
    (spec Edge Cases -- "the user can tell which run they are looking at at all
    times"), and none is. Both render here rather than guessing, and neither is
    an error.
    """

    active_runs: tuple[RunSummaryView, ...] = ()
    empty_state_reason: str | None = None
    last_confirmed_current_at: datetime
    panel_registry: PanelRegistryVersion


def latest_decision_of(turn: TurnCycleView | None) -> DecisionView | None:
    """The last recorded decision of a turn, or ``None``.

    Reads the *last* step rather than a separately-stored "latest decision"
    field, so there is no second value that could disagree with the turn the
    same response is showing.
    """
    if turn is None:
        return None
    for step in reversed(turn.steps):
        if step.decision is not None:
            return step.decision
    return None


def build_run_detail_view(
    *,
    summary: RunSummaryView,
    current_turn: TurnCycleView | None,
    intervention_info: InterventionInfo,
    panel_registry: PanelRegistryVersion,
    recent_events: tuple[RunEventView, ...] = (),
    last_confirmed_current_at: datetime | None = None,
    latest_decision: DecisionView | None = None,
) -> RunDetailView:
    """Assemble the glance. A run with zero authoritative turns says so."""
    return RunDetailView(
        summary=summary,
        current_turn=current_turn,
        latest_decision=(
            latest_decision if latest_decision is not None else latest_decision_of(current_turn)
        ),
        last_confirmed_current_at=last_confirmed_current_at or utcnow(),
        intervention_info=intervention_info,
        recent_events=recent_events,
        empty_state_reason=None if current_turn is not None else NO_TURNS_RECORDED,
        panel_registry=panel_registry,
    )


def build_intervention_info(
    run: Any,
    *,
    registry: PanelRegistry,
    last_known_good: Any | None = None,
) -> InterventionInfo:
    """FR-027's three facts, read from the store this feature already depends on.

    Deliberately *not* fetched from 002's operator surface: this feature
    presents, the operator surface commands, and the two never call one another
    (plan.md Constraints, 002 plan C3). Displaying the run id, lifecycle status
    and last-known-good save is what keeps intervention *information* and
    intervention *capability* on opposite sides of that line.

    Read through the registry gate like every other constructor, so the save
    lineage this shows is registered (``run.intervention``) rather than reached
    for directly -- FR-027 does not exempt a panel from UP-001.
    """
    run_gate = GatedReader(registry, "Run", run)
    save_gate = GatedReader(registry, "SavePoint", last_known_good)
    return InterventionInfo(
        run_id=str(run_gate.get("run_id", default="") or ""),
        lifecycle_status=run_gate.text("lifecycle_state", default="") or "",
        last_known_good_turn=(
            save_gate.get("turn_number") if last_known_good is not None else None
        ),
        last_known_good_save_id=(
            save_gate.text("save_point_id") if last_known_good is not None else None
        ),
        last_known_good_save_name=(
            save_gate.text("save_name") if last_known_good is not None else None
        ),
        unavailable=tuple(run_gate.missing) + tuple(save_gate.missing),
    )
