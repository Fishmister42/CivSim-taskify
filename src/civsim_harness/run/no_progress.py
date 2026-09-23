"""No-progress accounting for the within-turn decision loop (T111, T113).

data-model.md SS5 "No-progress accounting" (FR-014, SC-022):

    counter = 0
    per step:
        execute + verify
        if decision rejected OR verification shows no game-state change:  counter += 1
        else:                                                             counter = 0
        if counter == no_progress_step_limit:  end turn as ended_on_no_progress

:class:`NoProgressTracker` is exactly that counter, nothing more -- it holds no reference to a
turn, a store, or a decision. This is deliberate: the counter is a pure function of the sequence of
:class:`~civsim_harness.models.turn.StepProgress` values it has seen, and keeping it that way is
what makes it trivially unit-testable (``tests/unit/test_turn_state_machine.py``,
``tests/integration/test_turn_endings.py``) without a turn, a run, or a store in the picture at
all. ``run/decision_loop.py`` (T110/T112) is the only caller: it feeds this tracker one
:class:`~civsim_harness.models.turn.StepProgress` per completed step and reads back
:attr:`NoProgressTracker.tripped` to decide whether the loop's *second* exit condition has fired.

**T113 -- the backstop synthesises no decision.** There is no function in this module that
constructs a :class:`~civsim_harness.models.decision.Decision` or a
:class:`~civsim_harness.models.turn.DecisionStep` -- structurally, nothing here *could* fabricate
one even by accident. A turn ended on no progress is recorded in exactly two places: the
:class:`~civsim_harness.models.turn.TurnCycle` itself (``outcome=ended_on_no_progress``,
``final_no_progress_streak``, set by the caller from :attr:`NoProgressTracker.streak`) and the
plain timeline event :func:`build_no_progress_event` builds below. Fabricating an ``end_turn``
decision to represent the backstop would be a defaulted move recorded as if the agent chose it,
corrupting SC-012's "zero decisions without a model call" and destroying the
``ended_by_agent``/``ended_on_no_progress`` distinction SC-022 requires -- this module has no path
that could do that even if a future edit tried to add one carelessly, since it never imports
``Decision`` or ``DecisionStep`` at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from civsim_harness.models.common import EventId, RunId, Timestamp
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.turn import StepProgress


@dataclass
class NoProgressTracker:
    """Counts consecutive no-progress decision steps within one turn attempt (data-model.md SS5).

    ``limit`` is ``no_progress_step_limit`` from the run's configuration (FR-001, FR-014) -- run
    configuration, not a constant this module hard-codes. :meth:`record` increments the streak on
    a step whose ``progress`` is ``rejected`` or ``no_change``, and resets it to zero on any step
    verified ``changed_state`` -- the reset is what makes this a stuck-agent detector rather than a
    step cap: a turn of hundreds of productive steps never approaches ``limit``, while an agent
    spinning on illegal or no-op orders trips it in exactly ``limit`` steps regardless of how long
    the turn has already run.
    """

    limit: int
    streak: int = 0
    #: The declaration the previous step issued, and how many consecutive steps have issued it.
    #: Separate from ``streak`` because they count different futilities and only one of them can
    #: be reset by the agent.
    repeated_declaration: str | None = None
    repeat_streak: int = 0

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("no_progress_step_limit must be >= 1 (FR-014)")

    def record(self, progress: StepProgress, declaration_id: str) -> int:
        """Update the streaks for one just-completed step; return the no-progress streak.

        *declaration_id* is required, with no default. A bound an existing caller can keep by
        forgetting a new argument is the shape three of this project's worst defects had.
        """
        if progress is StepProgress.CHANGED_STATE:
            self.streak = 0
        else:  # NO_CHANGE or REJECTED
            self.streak += 1

        if declaration_id == self.repeated_declaration:
            self.repeat_streak += 1
        else:
            self.repeated_declaration = declaration_id
            self.repeat_streak = 1
        return self.streak

    @property
    def tripped(self) -> bool:
        """Whether the turn must end ``ended_on_no_progress`` -- on either futility.

        **The second test exists because the first one cannot see a loop that succeeds.**
        ``streak`` resets on ``CHANGED_STATE``, so an agent that keeps issuing an action which
        genuinely changes something resets the bound on every step and the guard can never trip.
        Measured live 2026-09-22: a run issued ``research.set_tech`` **158 consecutive times**, every
        one of them ``applied``, for twenty-one minutes on a single turn, and this counter stayed at
        zero throughout -- each step really did change the research, so each step really was
        progress by the only definition available here.

        A repeat streak is not resettable by doing the same thing again, which is exactly the
        property ``streak`` lacks. The threshold is ``limit`` itself rather than a new constant:
        issuing one action ``limit`` times in a row is as futile as ``limit`` steps that change
        nothing, and a second number here would be a second thing to mis-tune.
        """
        return self.streak >= self.limit or self.repeat_streak >= self.limit


def build_no_progress_event(
    *,
    run_id: RunId,
    turn_number: int,
    occurred_at: Timestamp,
    final_no_progress_streak: int,
    step_index: int | None = None,
    event_id: EventId | None = None,
) -> RunEvent:
    """Build the ``turn_ended_on_no_progress`` timeline event (FR-014, SC-022).

    This is the audit trail data-model.md SS14 requires alongside ``TurnCycle.outcome`` itself:
    "SC-022 requires a backstop-ended turn to be distinguishable from one the agent chose to end.
    The ``TurnCycle.outcome`` records it; the event puts it on the timeline." The caller
    (``run/decision_loop.py``/``run/turn_cycle.py``) is responsible for persisting this event
    through the store alongside the rest of the turn's record; this function only builds it.
    """
    return RunEvent(
        event_id=event_id if event_id is not None else EventId(uuid.uuid4().hex),
        run_id=run_id,
        turn_number=turn_number,
        step_index=step_index,
        event_type=RunEventType.TURN_ENDED_ON_NO_PROGRESS,
        occurred_at=occurred_at,
        detail={"final_no_progress_streak": final_no_progress_streak},
    )
