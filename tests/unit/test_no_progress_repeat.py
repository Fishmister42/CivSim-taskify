"""A loop that succeeds is still a loop, and the no-progress counter cannot see it.

Measured live 2026-09-22: a run issued ``research.set_tech`` **158 consecutive times**, every one
of them ``applied``, for twenty-one minutes on a single turn. Each step genuinely changed the
research, so each step was ``CHANGED_STATE``, so the no-progress streak reset on every step and the
turn-ending guard stayed at zero the whole time. The bound existed and the agent reset it by
repeating itself.
"""

from __future__ import annotations

from civsim_harness.models.turn import StepProgress
from civsim_harness.run.no_progress import NoProgressTracker


def test_an_action_repeated_forever_trips_the_guard_even_though_every_step_succeeds() -> None:
    """The regression this exists for, in the shape production produced it."""
    tracker = NoProgressTracker(limit=5)

    for _ in range(5):
        tracker.record(StepProgress.CHANGED_STATE, "research.set_tech")

    # The positive control, and the whole reason this test is not redundant with the old one:
    # the ORIGINAL counter never moved. If someone reverts the repeat guard, this assertion still
    # passes and the one below fails -- which points at the right line instead of at this fixture.
    assert tracker.streak == 0, "changed-state steps must still reset the original counter"
    assert tracker.tripped, "five identical successful steps is a loop and must end the turn"


def test_varied_successful_work_never_trips_it() -> None:
    """The negative control. A turn doing real, varied work must be left alone.

    Without this, 'trip on repetition' could be satisfied by a guard that trips on everything,
    which would end every turn after five steps and look like a fix.
    """
    tracker = NoProgressTracker(limit=5)

    for declaration in (
        "research.set_tech",
        "units.move_to",
        "cities.set_production",
        "units.select",
        "research.set_civic",
        "units.move_to",
        "camera.zoom",
    ):
        tracker.record(StepProgress.CHANGED_STATE, declaration)

    assert not tracker.tripped
    assert tracker.streak == 0


def test_alternating_between_two_actions_does_not_trip_it() -> None:
    """Deliberately NOT caught, and the limit is worth stating out loud.

    This guard counts CONSECUTIVE repeats only. An agent alternating between two actions forever
    would defeat it, and that is a real remaining hole rather than an oversight -- catching it
    needs a different measure than 'the last declaration'. Pinned here so the next person finds
    the boundary stated rather than having to rediscover it.
    """
    tracker = NoProgressTracker(limit=5)

    for _ in range(10):
        tracker.record(StepProgress.CHANGED_STATE, "research.set_tech")
        tracker.record(StepProgress.CHANGED_STATE, "research.set_civic")

    assert not tracker.tripped


def test_the_original_no_change_streak_still_trips_on_its_own() -> None:
    """The pre-existing behaviour must survive the addition, on its own terms."""
    tracker = NoProgressTracker(limit=3)

    for declaration in ("units.move_to", "camera.zoom", "cities.select"):
        tracker.record(StepProgress.REJECTED, declaration)

    assert tracker.streak == 3
    assert tracker.tripped


def test_a_successful_step_clears_the_repeat_run() -> None:
    """Four repeats then something else must not carry the count across."""
    tracker = NoProgressTracker(limit=5)

    for _ in range(4):
        tracker.record(StepProgress.CHANGED_STATE, "research.set_tech")
    tracker.record(StepProgress.CHANGED_STATE, "units.move_to")
    for _ in range(4):
        tracker.record(StepProgress.CHANGED_STATE, "research.set_tech")

    assert not tracker.tripped, "the run was broken, so neither half reaches the limit"
