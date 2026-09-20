"""Run-health derivation (T010, data-model.md SS2).

This module contains the only real logic in the shared view models, and its
most important property is what it **does not** do: it never looks at a clock.

Research R7 and invariant V3 rule out an independent stall detector here. 002
already decides when a run has hung -- it records ``hang_detected`` and
``unresponsive_detected`` events -- and a second detector in this process,
timing turn arrivals and forming its own opinion, would eventually disagree
with 002's. Two parties looking at the same run and being told different things
about whether it is stuck is precisely the asymmetry Principle VI exists to
prevent, so the rule is stated as an invariant rather than a preference:

    ``stalled`` may only be set when a ``RunEvent`` of type ``hang_detected``
    or ``unresponsive_detected`` exists for this run with no later
    ``resumed``/``playing`` transition after it.

Timestamps *are* read, but only to answer "when did this state begin" for
display. No branch in this module compares a timestamp to now, or to another
timestamp, to decide what the state *is*.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from civsim_web.store_client.port import RunEventLike, RunLike
from civsim_web.viewmodels.base import HealthState, HealthStatus

__all__ = [
    "HANG_EVENT_TYPES",
    "LIFECYCLE_TO_HEALTH",
    "RESUME_EVENT_TYPES",
    "derive_health",
]

#: data-model.md SS2's mapping, verbatim, plus ``paused`` -- the one state in
#: 002's published lifecycle machine that rule does not name. See
#: ``HealthState`` for why it is not folded into ``running`` or ``stalled``.
LIFECYCLE_TO_HEALTH: dict[str, HealthState] = {
    "preparing": HealthState.RUNNING,
    "playing": HealthState.RUNNING,
    "waiting_on_model": HealthState.WAITING_ON_MODEL,
    "waiting_on_game": HealthState.WAITING_ON_GAME,
    "interrupted": HealthState.CRASHED,
    "failed": HealthState.CRASHED,
    "resuming": HealthState.RESUMED,
    "finished": HealthState.FINISHED,
    "paused": HealthState.PAUSED,
}

#: The only two events that may produce ``stalled``.
HANG_EVENT_TYPES: frozenset[str] = frozenset({"hang_detected", "unresponsive_detected"})

#: Events that end a stall. ``resumed`` is explicit; a ``lifecycle_transition``
#: back to ``playing`` is the other half of "no later resumed/playing
#: transition after it".
RESUME_EVENT_TYPES: frozenset[str] = frozenset({"resumed"})

#: Event that justifies ``crashed``, when one was recorded.
_CRASH_EVENT_TYPES: frozenset[str] = frozenset({"crash_detected"})

#: Keys a ``lifecycle_transition`` event's ``detail`` might use for its target
#: state. Checked tolerantly because ``detail`` is typed ``json`` in 002's
#: data model -- its internal key names are not part of the published contract,
#: so this feature reads what it can and treats an unreadable detail as *not*
#: a resume (fail closed: a stall stays visible rather than being cleared by a
#: detail shape we misread).
_TRANSITION_TARGET_KEYS: tuple[str, ...] = ("to", "to_state", "new_state", "state")


def _event_type(event: RunEventLike) -> str:
    raw: Any = event.event_type
    return str(getattr(raw, "value", raw))


def _is_playing_transition(event: RunEventLike) -> bool:
    if _event_type(event) != "lifecycle_transition":
        return False
    detail = event.detail
    if not isinstance(detail, dict):
        return False
    for key in _TRANSITION_TARGET_KEYS:
        value = detail.get(key)
        if value is None:
            continue
        if str(getattr(value, "value", value)) == "playing":
            return True
    return False


def _latest(events: Iterable[RunEventLike], types: frozenset[str]) -> RunEventLike | None:
    """The last event of one of ``types`` in an already-chronological sequence.

    ``list_run_events`` is contractually chronological by ``occurred_at``
    (002's port), so "latest" is a position in the sequence, not a timestamp
    comparison this feature performs.
    """
    found: RunEventLike | None = None
    for event in events:
        if _event_type(event) in types:
            found = event
    return found


def _index_of_last(events: Sequence[RunEventLike], predicate: Any) -> int:
    for index in range(len(events) - 1, -1, -1):
        if predicate(events[index]):
            return index
    return -1


def derive_health(run: RunLike, events: Sequence[RunEventLike] = ()) -> HealthStatus:
    """Derive one run's ``HealthStatus`` from its lifecycle state and events.

    ``events`` must be the run's own timeline in chronological order -- what
    ``MatchStore.list_run_events`` returns. An empty sequence is valid and
    yields the plain lifecycle-derived value, which is the documented fallback:
    "If no such event exists, the plain lifecycle-derived value stands."
    """
    raw_state: Any = run.lifecycle_state
    lifecycle = str(getattr(raw_state, "value", raw_state))
    base = LIFECYCLE_TO_HEALTH.get(lifecycle, HealthState.UNKNOWN)

    state = base
    reason_event: RunEventLike | None = None

    # -- refinement 1: stalled, and only from a hang event -------------------
    #
    # A finished run is never refined to stalled: a hang recorded earlier in a
    # run that went on to finish is history, not the current state.
    if base is not HealthState.FINISHED:
        hang_index = _index_of_last(events, lambda e: _event_type(e) in HANG_EVENT_TYPES)
        if hang_index >= 0:
            later = events[hang_index + 1 :]
            resumed = any(
                _event_type(e) in RESUME_EVENT_TYPES or _is_playing_transition(e)
                for e in later
            )
            if not resumed:
                state = HealthState.STALLED
                reason_event = events[hang_index]

    # -- refinement 2: name the event behind a crash, when one exists --------
    if state is HealthState.CRASHED:
        reason_event = _latest(events, _CRASH_EVENT_TYPES) or reason_event

    # -- refinement 3: name the event behind a resume ------------------------
    if state is HealthState.RESUMED:
        reason_event = _latest(events, RESUME_EVENT_TYPES) or reason_event

    since = _since(run, events, reason_event)

    return HealthStatus(
        state=state,
        reason_event_id=getattr(reason_event, "event_id", None) if reason_event else None,
        since=since,
        lifecycle_state=lifecycle,
    )


def _since(
    run: RunLike, events: Sequence[RunEventLike], reason_event: RunEventLike | None
) -> Any:
    """When the current state began -- for display only.

    Preference order: the event that justifies the state, then the most recent
    recorded lifecycle transition, then the run's own ``started_at``. Nothing
    here decides *what* the state is; it only labels when it began, so the
    "never by an independent timestamp comparison" rule is untouched.
    """
    if reason_event is not None:
        return reason_event.occurred_at
    transition = _latest(events, frozenset({"lifecycle_transition"}))
    if transition is not None:
        return transition.occurred_at
    return run.started_at
