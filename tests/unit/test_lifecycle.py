"""Unit tests for the run lifecycle state machine (T045).

Covers legal and illegal transitions against the full data-model.md SS4
graph, an event recorded for every transition, ``finished`` requiring
exactly one ``stop_resolution``, and -- the subtle one -- ``archived_at``
being orthogonal to ``lifecycle_state``: an archived run keeps its records
and stays readable, and archival is never modelled as a lifecycle state.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from civsim_harness.errors import HarnessError
from civsim_harness.models.common import CapturePath, CatalogVersionRef, ConfigId, RunId
from civsim_harness.models.records import RunEventType
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
    StopResolution,
)
from civsim_harness.run.lifecycle import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    is_legal_transition,
    transition,
)

_T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 9, 20, 12, 5, 0, tzinfo=UTC)

_ALL_STATES = list(LifecycleState)


def _run(lifecycle_state: LifecycleState, **overrides: object) -> Run:
    fields: dict[str, object] = {
        "run_id": RunId("run-1"),
        "config_id": ConfigId("cfg-1"),
        "lifecycle_state": lifecycle_state,
        "record_completeness_status": RecordCompletenessStatus.UNKNOWN,
        "comparability_status": ComparabilityStatus.COMPARABLE,
        "observation_catalog_version": CatalogVersionRef(version="1.0.0", content_hash="obs-hash"),
        "action_catalog_version": CatalogVersionRef(version="1.0.0", content_hash="act-hash"),
        "game_build": "win/1.0.12.9",
        "host_support_tier": HostSupportTier.VALIDATED,
        "capture_path": CapturePath.NONE,
    }
    fields.update(overrides)
    return Run.model_validate(fields)


# --------------------------------------------------------------------------
# The graph itself
# --------------------------------------------------------------------------


def test_every_lifecycle_state_is_a_key_in_the_transition_graph() -> None:
    assert set(LEGAL_TRANSITIONS) == set(_ALL_STATES)


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[state] == frozenset()


def test_transition_graph_matches_data_model_ss4_exactly() -> None:
    """Locks in the exact edge set read off the SS4 diagram and its prose.

    In particular: ``resuming -> failed`` is legal (FR-048's "a run reaching
    failed from resuming must identify its last-known-good save" names
    resuming, not interrupted, as the state that feeds failed), and
    ``interrupted -> failed`` is therefore NOT an edge, even though the
    diagram's ASCII art is locally ambiguous about that branch.
    """
    expected = {
        LifecycleState.PREPARING: {LifecycleState.PLAYING, LifecycleState.FAILED},
        LifecycleState.PLAYING: {
            LifecycleState.WAITING_ON_MODEL,
            LifecycleState.WAITING_ON_GAME,
            LifecycleState.PAUSED,
            LifecycleState.INTERRUPTED,
            LifecycleState.FINISHED,
        },
        LifecycleState.WAITING_ON_MODEL: {LifecycleState.PLAYING},
        LifecycleState.WAITING_ON_GAME: {LifecycleState.PLAYING},
        LifecycleState.PAUSED: {LifecycleState.PLAYING},
        LifecycleState.INTERRUPTED: {LifecycleState.RESUMING},
        LifecycleState.RESUMING: {LifecycleState.PLAYING, LifecycleState.FAILED},
        LifecycleState.FINISHED: set(),
        LifecycleState.FAILED: set(),
    }
    assert {k: set(v) for k, v in LEGAL_TRANSITIONS.items()} == expected


# --------------------------------------------------------------------------
# Legal transitions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (LifecycleState.PREPARING, LifecycleState.PLAYING),
        (LifecycleState.PLAYING, LifecycleState.WAITING_ON_MODEL),
        (LifecycleState.WAITING_ON_MODEL, LifecycleState.PLAYING),
        (LifecycleState.PLAYING, LifecycleState.WAITING_ON_GAME),
        (LifecycleState.WAITING_ON_GAME, LifecycleState.PLAYING),
        (LifecycleState.PLAYING, LifecycleState.PAUSED),
        (LifecycleState.PAUSED, LifecycleState.PLAYING),
        (LifecycleState.PLAYING, LifecycleState.INTERRUPTED),
        (LifecycleState.INTERRUPTED, LifecycleState.RESUMING),
        (LifecycleState.RESUMING, LifecycleState.PLAYING),
    ],
)
def test_legal_transition_updates_state_and_returns_one_event(
    from_state: LifecycleState, to_state: LifecycleState
) -> None:
    assert is_legal_transition(from_state, to_state)
    run = _run(from_state)
    updated_run, event = transition(run, to_state, occurred_at=_T0)

    assert updated_run.lifecycle_state == to_state
    assert run.lifecycle_state == from_state, "the input Run must not be mutated"

    assert event.event_type == RunEventType.LIFECYCLE_TRANSITION
    assert event.run_id == run.run_id
    assert event.occurred_at == _T0
    assert event.detail["from"] == from_state.value
    assert event.detail["to"] == to_state.value


def test_preparing_to_playing_stamps_started_at_once() -> None:
    run = _run(LifecycleState.PREPARING)
    updated_run, _ = transition(run, LifecycleState.PLAYING, occurred_at=_T0)
    assert updated_run.started_at == _T0

    # A later playing -> paused -> playing round trip must not overwrite it.
    paused_run, _ = transition(updated_run, LifecycleState.PAUSED, occurred_at=_T1)
    resumed_run, _ = transition(paused_run, LifecycleState.PLAYING, occurred_at=_T1)
    assert resumed_run.started_at == _T0


# --------------------------------------------------------------------------
# finished requires exactly one stop_resolution
# --------------------------------------------------------------------------


def test_playing_to_finished_without_stop_resolution_is_rejected() -> None:
    run = _run(LifecycleState.PLAYING)
    with pytest.raises(ValidationError):
        transition(run, LifecycleState.FINISHED, occurred_at=_T1)


def test_playing_to_finished_with_stop_resolution_succeeds() -> None:
    run = _run(LifecycleState.PLAYING)
    updated_run, event = transition(
        run,
        LifecycleState.FINISHED,
        occurred_at=_T1,
        stop_resolution=StopResolution.TURN_REACHED,
    )
    assert updated_run.lifecycle_state == LifecycleState.FINISHED
    assert updated_run.stop_resolution == StopResolution.TURN_REACHED
    assert updated_run.ended_at == _T1
    assert event.event_type == RunEventType.LIFECYCLE_TRANSITION


def test_resuming_to_failed_does_not_require_a_stop_resolution() -> None:
    """failed is broader than finished: resuming -> failed needs no stop_resolution."""
    run = _run(LifecycleState.RESUMING)
    updated_run, _ = transition(run, LifecycleState.FAILED, occurred_at=_T1)
    assert updated_run.lifecycle_state == LifecycleState.FAILED
    assert updated_run.stop_resolution is None
    assert updated_run.ended_at == _T1


def test_resuming_to_failed_may_record_unrecoverable_failure() -> None:
    run = _run(LifecycleState.RESUMING)
    updated_run, _ = transition(
        run,
        LifecycleState.FAILED,
        occurred_at=_T1,
        stop_resolution=StopResolution.UNRECOVERABLE_FAILURE,
    )
    assert updated_run.stop_resolution == StopResolution.UNRECOVERABLE_FAILURE


def test_preparing_to_failed_preflight_mismatch() -> None:
    run = _run(LifecycleState.PREPARING)
    updated_run, event = transition(run, LifecycleState.FAILED, occurred_at=_T0)
    assert updated_run.lifecycle_state == LifecycleState.FAILED
    assert event.detail == {"from": "preparing", "to": "failed"}


def test_stop_resolution_rejected_on_a_non_terminal_transition() -> None:
    run = _run(LifecycleState.PLAYING)
    with pytest.raises(HarnessError):
        transition(
            run,
            LifecycleState.PAUSED,
            occurred_at=_T0,
            stop_resolution=StopResolution.OPERATOR_STOP,
        )


# --------------------------------------------------------------------------
# Illegal transitions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        # Skipping intermediate states entirely.
        (LifecycleState.PREPARING, LifecycleState.FINISHED),
        (LifecycleState.PREPARING, LifecycleState.WAITING_ON_MODEL),
        (LifecycleState.PLAYING, LifecycleState.RESUMING),
        (LifecycleState.PLAYING, LifecycleState.FAILED),
        # interrupted must route through resuming -- direct failed is not an edge
        # even though the ASCII diagram draws that branch ambiguously.
        (LifecycleState.INTERRUPTED, LifecycleState.FAILED),
        (LifecycleState.INTERRUPTED, LifecycleState.PLAYING),
        # Sideways moves between the wait states.
        (LifecycleState.WAITING_ON_MODEL, LifecycleState.WAITING_ON_GAME),
        (LifecycleState.WAITING_ON_GAME, LifecycleState.WAITING_ON_MODEL),
        (LifecycleState.PAUSED, LifecycleState.INTERRUPTED),
        # Leaving a terminal state.
        (LifecycleState.FINISHED, LifecycleState.PLAYING),
        (LifecycleState.FAILED, LifecycleState.PREPARING),
        (LifecycleState.FAILED, LifecycleState.RESUMING),
        # Self-loops -- not drawn in the diagram, not a real transition.
        (LifecycleState.PLAYING, LifecycleState.PLAYING),
        (LifecycleState.PREPARING, LifecycleState.PREPARING),
        (LifecycleState.FINISHED, LifecycleState.FINISHED),
    ],
)
def test_illegal_transition_raises_and_leaves_the_run_unchanged(
    from_state: LifecycleState, to_state: LifecycleState
) -> None:
    assert not is_legal_transition(from_state, to_state)
    kwargs: dict[str, object] = {}
    if from_state == LifecycleState.FINISHED:
        kwargs["stop_resolution"] = StopResolution.TURN_REACHED
        kwargs["ended_at"] = _T0
    run = _run(from_state, **kwargs)

    with pytest.raises(HarnessError):
        transition(run, to_state, occurred_at=_T1)

    assert run.lifecycle_state == from_state


# --------------------------------------------------------------------------
# archived_at is orthogonal to lifecycle_state
# --------------------------------------------------------------------------


def test_archived_run_keeps_its_records_and_stays_readable() -> None:
    """Archival is not a lifecycle state: an archived, finished run reads back
    with every field intact, and lifecycle.transition() never touches
    archived_at (that is an explicit operator action elsewhere -- the store's
    archive_run, a different task -- not this state machine).
    """
    finished_run = _run(
        LifecycleState.FINISHED,
        stop_resolution=StopResolution.VICTORY,
        ended_at=_T1,
    )
    assert finished_run.archived_at is None

    archived_run = Run.model_validate({**finished_run.model_dump(), "archived_at": _T1})

    assert archived_run.lifecycle_state == LifecycleState.FINISHED
    assert archived_run.stop_resolution == StopResolution.VICTORY
    assert archived_run.archived_at == _T1
    # Every other field survives archival untouched.
    assert archived_run.run_id == finished_run.run_id
    assert archived_run.game_build == finished_run.game_build


def test_archived_at_does_not_change_which_transitions_are_legal() -> None:
    """A terminal run rejects every outgoing transition regardless of
    archived_at -- archival neither opens nor closes any edge in the graph,
    because it is not part of the graph at all.
    """
    not_archived = _run(
        LifecycleState.FINISHED, stop_resolution=StopResolution.DEFEAT, ended_at=_T0
    )
    archived = Run.model_validate({**not_archived.model_dump(), "archived_at": _T0})

    for candidate in _ALL_STATES:
        if candidate == LifecycleState.FINISHED:
            continue
        with pytest.raises(HarnessError):
            transition(not_archived, candidate, occurred_at=_T1)
        with pytest.raises(HarnessError):
            transition(archived, candidate, occurred_at=_T1)


def test_archived_at_before_terminal_is_rejected_by_run_itself() -> None:
    """Structural half of orthogonality: Run itself (not this module) refuses
    archived_at on a non-terminal run -- archival presupposes a terminal run
    but is still not itself a lifecycle_state.
    """
    playing_run = _run(LifecycleState.PLAYING)
    with pytest.raises(ValidationError):
        Run.model_validate({**playing_run.model_dump(), "archived_at": _T0})
