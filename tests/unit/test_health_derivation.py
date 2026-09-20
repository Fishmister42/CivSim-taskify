"""Health derivation (T017, data-model.md SS2 / invariant V3).

The invariant under test, verbatim from quickstart.md Scenario 1:

    'stalled' only ever set from a `hang_detected`/`unresponsive_detected`
    event, never from an independently computed timestamp gap.

This is not a style preference. 002 already decides when a run has hung; a
second detector here, timing turn arrivals, would eventually disagree with it,
and two parties being told different things about whether the same run is stuck
is the asymmetry Principle VI exists to prevent (research R7).

So the negative cases below matter more than the positive one: a run that has
recorded nothing for a simulated week, with no hang event, must still not read
as stalled.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from civsim_web.health.derive import LIFECYCLE_TO_HEALTH, derive_health
from civsim_web.viewmodels.base import HealthState


@pytest.fixture
def support():
    from web_support import fixtures

    return fixtures


# --------------------------------------------------------------------------
# The lifecycle mapping (data-model.md SS2, verbatim)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lifecycle_state", "expected"),
    [
        ("preparing", HealthState.RUNNING),
        ("playing", HealthState.RUNNING),
        ("waiting_on_model", HealthState.WAITING_ON_MODEL),
        ("waiting_on_game", HealthState.WAITING_ON_GAME),
        ("interrupted", HealthState.CRASHED),
        ("failed", HealthState.CRASHED),
        ("resuming", HealthState.RESUMED),
        ("finished", HealthState.FINISHED),
    ],
)
def test_lifecycle_state_maps_as_the_data_model_specifies(support, lifecycle_state, expected):
    run = support.make_run(lifecycle_state=lifecycle_state)
    assert derive_health(run, []).state is expected


def test_paused_is_neither_running_nor_stalled(support):
    """002's lifecycle has `paused`; data-model.md SS2's rule does not map it.

    Recorded as a finding rather than folded into a neighbour: `running` would
    claim a run is advancing when it has stopped, and `stalled` would violate
    the hang-event-only rule below. It gets its own honest state (UP-005).
    """
    run = support.make_run(lifecycle_state="paused")
    health = derive_health(run, [])
    assert health.state is HealthState.PAUSED
    assert health.lifecycle_state == "paused"


def test_an_unrecognised_lifecycle_state_fails_closed_to_unknown(support):
    """A future 002 schema value must not be silently read as healthy."""
    run = support.make_run(lifecycle_state="hibernating_for_the_winter")
    health = derive_health(run, [])
    assert health.state is HealthState.UNKNOWN
    assert health.lifecycle_state == "hibernating_for_the_winter"


def test_every_published_lifecycle_state_is_mapped():
    """The nine states in 002's published state machine all have a mapping."""
    published = {
        "preparing",
        "playing",
        "waiting_on_model",
        "waiting_on_game",
        "paused",
        "interrupted",
        "resuming",
        "finished",
        "failed",
    }
    assert published <= set(LIFECYCLE_TO_HEALTH)


# --------------------------------------------------------------------------
# The invariant: stalled comes from an event, or not at all
# --------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", ["hang_detected", "unresponsive_detected"])
def test_stalled_is_set_from_a_hang_event(support, event_type):
    run = support.make_run(lifecycle_state="playing")
    event = support.make_event(event_type, minutes=30)

    health = derive_health(run, [event])

    assert health.state is HealthState.STALLED
    assert health.reason_event_id == event.event_id
    assert health.since == event.occurred_at


def test_stalled_is_never_inferred_from_a_long_silence(support):
    """The whole point. A week of nothing, no hang event, still not stalled.

    If this test ever fails, someone has added exactly the independent
    staleness heuristic research R7 rules out.
    """
    run = support.make_run(
        lifecycle_state="playing",
        started_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    # A timeline full of ordinary activity, none of it a hang, and all of it
    # ancient by wall-clock standards.
    events = [
        support.make_event("save_taken", minutes=1),
        support.make_event("lifecycle_transition", minutes=2, detail={"to": "playing"}),
        support.make_event("provider_retry", minutes=3),
    ]

    health = derive_health(run, events)

    assert health.state is HealthState.RUNNING
    assert health.reason_event_id is None


def test_a_resume_after_a_hang_clears_the_stall(support):
    run = support.make_run(lifecycle_state="playing")
    events = [
        support.make_event("hang_detected", minutes=30),
        support.make_event("resumed", minutes=35),
    ]

    assert derive_health(run, events).state is HealthState.RUNNING


def test_a_playing_transition_after_a_hang_clears_the_stall(support):
    """"no later resumed/playing transition after it" -- the `playing` half."""
    run = support.make_run(lifecycle_state="playing")
    events = [
        support.make_event("hang_detected", minutes=30),
        support.make_event("lifecycle_transition", minutes=36, detail={"to": "playing"}),
    ]

    assert derive_health(run, events).state is HealthState.RUNNING


def test_a_hang_then_another_hang_stays_stalled(support):
    run = support.make_run(lifecycle_state="playing")
    events = [
        support.make_event("hang_detected", minutes=30),
        support.make_event("resumed", minutes=35),
        support.make_event("unresponsive_detected", event_id="evt-second", minutes=40),
    ]

    health = derive_health(run, events)
    assert health.state is HealthState.STALLED
    assert health.reason_event_id == "evt-second"


def test_a_finished_run_is_not_retroactively_stalled(support):
    """A hang recorded mid-run is history once the run finished, not the state."""
    run = support.make_run(lifecycle_state="finished")
    events = [support.make_event("hang_detected", minutes=30)]

    assert derive_health(run, events).state is HealthState.FINISHED


def test_an_unreadable_transition_detail_does_not_clear_a_stall(support):
    """`RunEvent.detail` is untyped json; a shape we cannot read fails closed.

    Clearing a stall on a detail we could not actually parse would hide a
    stuck run, which is the one direction this must never fail in.
    """
    run = support.make_run(lifecycle_state="playing")
    events = [
        support.make_event("hang_detected", minutes=30),
        support.make_event("lifecycle_transition", minutes=35, detail={"mystery": "shape"}),
    ]

    assert derive_health(run, events).state is HealthState.STALLED


def test_crashed_names_the_crash_event_when_one_exists(support):
    run = support.make_run(lifecycle_state="interrupted")
    crash = support.make_event("crash_detected", minutes=12)

    health = derive_health(run, [crash])

    assert health.state is HealthState.CRASHED
    assert health.reason_event_id == crash.event_id


def test_health_is_always_present_even_with_no_events(support):
    """A missing health value is indistinguishable from a loading state (SS1)."""
    run = support.make_run(lifecycle_state="finished")
    health = derive_health(run)
    assert health.state is HealthState.FINISHED
    assert health.lifecycle_state == "finished"
