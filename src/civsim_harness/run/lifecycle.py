"""The run lifecycle state machine (T044).

data-model.md SS4. The legal transition graph, read literally off that
section's diagram:

    preparing        -> playing | failed
    playing          -> waiting_on_model | waiting_on_game | paused | interrupted | finished
    waiting_on_model -> playing
    waiting_on_game  -> playing
    paused           -> playing
    interrupted      -> resuming
    resuming         -> playing | failed

``finished`` and ``failed`` are terminal: no edge leaves either. Every
transition that actually executes produces a ``lifecycle_transition``
``RunEvent`` (FR-003) -- an illegal transition raises *before* any event is
produced or any ``Run`` field changes, so a rejected attempt leaves no trace
on either.

The one edge the SS4 ASCII diagram draws ambiguously is the branch to
``failed`` below ``interrupted -> resuming -> playing``. The diagram's prose
resolves it unambiguously: "A run reaching ``failed`` from ``resuming`` must
identify its last-known-good save (FR-048)" names ``resuming`` as the state
that feeds ``failed`` on that branch, not ``interrupted`` directly, so that is
the edge implemented here.

**Archival is deliberately absent from this module.** data-model.md SS4 is
explicit that ``archived_at`` is "a terminal-state action, not a lifecycle
state" -- orthogonal to ``lifecycle_state``, set only by an explicit operator
command (``store.archive_run``, a later wave), never by a lifecycle
transition. An archived run keeps its records and stays readable regardless of
how it got to its terminal state; nothing here reads or writes
``archived_at`` beyond what ``Run`` itself already enforces structurally
(archived_at only once terminal -- see ``models.run.Run``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from civsim_harness.errors import HarnessError
from civsim_harness.models.common import EventId, Timestamp
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.run import LifecycleState, Run, StopResolution

TERMINAL_STATES: frozenset[LifecycleState] = frozenset(
    {LifecycleState.FINISHED, LifecycleState.FAILED}
)

#: The legal transition graph. Every ``LifecycleState`` is a key; a state with
#: no outgoing edge (``finished``, ``failed``) maps to an empty set rather than
#: being absent, so ``LEGAL_TRANSITIONS[state]`` never raises ``KeyError`` for
#: a real ``LifecycleState``.
LEGAL_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.PREPARING: frozenset({LifecycleState.PLAYING, LifecycleState.FAILED}),
    LifecycleState.PLAYING: frozenset(
        {
            LifecycleState.WAITING_ON_MODEL,
            LifecycleState.WAITING_ON_GAME,
            LifecycleState.PAUSED,
            LifecycleState.INTERRUPTED,
            LifecycleState.FINISHED,
        }
    ),
    LifecycleState.WAITING_ON_MODEL: frozenset({LifecycleState.PLAYING}),
    LifecycleState.WAITING_ON_GAME: frozenset({LifecycleState.PLAYING}),
    LifecycleState.PAUSED: frozenset({LifecycleState.PLAYING}),
    LifecycleState.INTERRUPTED: frozenset({LifecycleState.RESUMING}),
    LifecycleState.RESUMING: frozenset({LifecycleState.PLAYING, LifecycleState.FAILED}),
    LifecycleState.FINISHED: frozenset(),
    LifecycleState.FAILED: frozenset(),
}


def is_legal_transition(from_state: LifecycleState, to_state: LifecycleState) -> bool:
    """Whether ``from_state -> to_state`` is an edge in the SS4 graph.

    A state "transitioning" to itself is never legal -- self-loops are not
    drawn in the diagram, and data-model.md's "every transition is recorded"
    (FR-003) presumes an actual state change.
    """
    return to_state in LEGAL_TRANSITIONS[from_state]


def transition(
    run: Run,
    to_state: LifecycleState,
    *,
    occurred_at: Timestamp,
    turn_number: int | None = None,
    step_index: int | None = None,
    stop_resolution: StopResolution | None = None,
    detail: Mapping[str, Any] | None = None,
    event_id: EventId | None = None,
) -> tuple[Run, RunEvent]:
    """Advance *run* from its current ``lifecycle_state`` to *to_state*.

    Returns ``(updated_run, event)`` on success: a new ``Run`` (the input is
    never mutated) with ``lifecycle_state`` set to *to_state*, plus the
    ``lifecycle_transition`` ``RunEvent`` that records the move (FR-003).
    Neither is persisted here -- that is the caller's (the run orchestrator,
    a later wave) responsibility, per the write-before-advance guard.

    Entering a terminal state (``finished`` or ``failed``) stamps
    ``ended_at`` (if not already set) and, when *stop_resolution* is given,
    records it. Entering ``playing`` from ``preparing`` stamps ``started_at``
    (if not already set).

    Raises ``HarnessError`` when:

    - ``from_state -> to_state`` is not an edge in ``LEGAL_TRANSITIONS`` --
      this includes attempting to leave a terminal state, and attempting a
      no-op transition to the run's current state.
    - *stop_resolution* is supplied for a transition that does not enter a
      terminal state.

    A *legal* transition into ``finished`` that omits *stop_resolution* is
    **not** rejected here -- that invariant ("finished requires exactly one
    stop_resolution", FR-005) already belongs to ``Run`` itself
    (``models.run.Run._terminal_state_invariants``), which this function
    re-runs by reconstructing the updated ``Run`` through validation rather
    than mutating in place. That reconstruction is what turns a missing
    stop_resolution into a raised ``pydantic.ValidationError`` instead of a
    silently inconsistent ``Run`` -- duplicating that check here would only
    let this module's copy drift from the model's.
    """
    from_state = run.lifecycle_state
    if not is_legal_transition(from_state, to_state):
        raise HarnessError(
            f"illegal lifecycle transition: {from_state.value} -> {to_state.value}",
            detail={"run_id": run.run_id, "from": from_state.value, "to": to_state.value},
        )

    entering_terminal = to_state in TERMINAL_STATES
    if stop_resolution is not None and not entering_terminal:
        raise HarnessError(
            "stop_resolution may only be recorded on a transition into a terminal state",
            detail={"run_id": run.run_id, "to": to_state.value},
        )

    updates: dict[str, Any] = {"lifecycle_state": to_state}
    if entering_terminal:
        updates["ended_at"] = run.ended_at or occurred_at
        if stop_resolution is not None:
            updates["stop_resolution"] = stop_resolution
    if from_state == LifecycleState.PREPARING and to_state == LifecycleState.PLAYING:
        updates["started_at"] = run.started_at or occurred_at

    # Reconstruct through validation (not `run.model_copy`, which does not
    # re-run validators) so `Run`'s own terminal-state invariants -- finished
    # requires exactly one stop_resolution, archived_at only once terminal --
    # are enforced on the updated state, not just the original.
    updated_run = Run.model_validate({**run.model_dump(), **updates})

    event_detail: dict[str, Any] = {"from": from_state.value, "to": to_state.value}
    if detail:
        event_detail.update(detail)

    event = RunEvent(
        event_id=event_id if event_id is not None else EventId(uuid.uuid4().hex),
        run_id=run.run_id,
        turn_number=turn_number,
        step_index=step_index,
        event_type=RunEventType.LIFECYCLE_TRANSITION,
        occurred_at=occurred_at,
        detail=event_detail,
    )
    return updated_run, event
