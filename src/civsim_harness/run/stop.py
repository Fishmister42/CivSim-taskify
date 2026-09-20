"""Stop resolution evaluation (T115).

Resolves the run's **configured** :data:`~civsim_harness.models.config.StopCondition` -- exactly
one of ``turn_reached(n)`` / ``game_outcome`` / ``operator_stop``, the 3-way set an operator picks
before a run starts (data-model.md SS2) -- against the facts observed right now to exactly one
**recorded** :class:`~civsim_harness.models.run.StopResolution`: ``turn_reached`` | ``victory`` |
``defeat`` | ``operator_stop`` | ``unrecoverable_failure`` (data-model.md SS4, FR-005, invariant
I10).

**These two enums must never be conflated.** The recorded set is strictly broader than the
configured set -- ``unrecoverable_failure`` terminates a run without being a configurable
``StopCondition.type`` at all, and a configured ``game_outcome`` condition resolves to one of two
distinct recorded outcomes (``victory``/``defeat``). data-model.md's own module docstring for
``models/run.py`` calls out that this conflation was a defect fixed earlier in this project --
this module is the one place that translation happens, and it is written against the two types
staying distinct on purpose: :func:`evaluate_stop` takes a
:data:`~civsim_harness.models.config.StopCondition` in and returns a
:class:`~civsim_harness.models.run.StopResolution` out, never the other way around.

This module is a pure decision function, like ``run.lifecycle.transition`` and
``run.preparation.build_pin_preflight`` before it: it makes no observation, store, or lifecycle
call of its own. The caller (``run/runner.py``, T116) supplies the facts as a
:class:`StopEvaluation` and is responsible for recording the returned resolution on the ``Run``
(via ``run.lifecycle.transition``) and a plain timeline event for every entry in
:attr:`StopDecision.coincident`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from civsim_harness.models.config import StopCondition, TurnReachedStopCondition
from civsim_harness.models.run import StopResolution


class GameOutcome(StrEnum):
    """Whether the game itself has resolved to a win or a loss for the local player, independent
    of what the run was configured to stop at. FR-005 requires a run to support playing to a game
    outcome, not only a turn limit -- so this is checked regardless of ``StopCondition.type``."""

    VICTORY = "victory"
    DEFEAT = "defeat"


@dataclass(frozen=True)
class StopEvaluation:
    """The observed facts a stop-resolution decision is made from, all supplied by the caller --
    this module never derives them itself (no observation, no store read)."""

    current_turn: int
    game_outcome: GameOutcome | None = None
    operator_stop_requested: bool = False
    unrecoverable_failure: bool = False


@dataclass(frozen=True)
class StopDecision:
    """Exactly one recorded resolution (or none, if the run should continue), plus every other
    condition that was *also* true right now (spec edge case, invariant I10)."""

    resolution: StopResolution | None
    coincident: tuple[StopResolution, ...]

    def __post_init__(self) -> None:
        if self.resolution is None and self.coincident:
            raise ValueError(
                "coincident resolutions require a primary resolution to be coincident with"
            )
        if self.resolution is not None and self.resolution in self.coincident:
            raise ValueError("the recorded resolution must not also appear in coincident")


def evaluate_stop(stop_condition: StopCondition, evaluation: StopEvaluation) -> StopDecision:
    """Resolve *stop_condition* against *evaluation* to at most one recorded
    :class:`~civsim_harness.models.run.StopResolution` (invariant I10).

    **Precedence when more than one condition holds at once** (spec edge case: "the run reaches
    its stop condition on the same turn as a victory, defeat, or crash -- exactly one stop
    condition is recorded, with the others present as events"): ``unrecoverable_failure`` outranks
    everything (nothing else matters once the harness itself cannot continue), then a resolved
    ``game_outcome`` (the game's own ending is the most concrete of what remains), then an explicit
    ``operator_stop`` (a deliberate human act), then the configured ``turn_reached`` threshold (the
    softest of the four -- a scheduled boundary, not an event). This ordering is a documented
    design choice, not dictated verbatim anywhere in the spec text; it is stated here precisely so
    a later reader can revisit it deliberately rather than by accident.

    ``game_outcome`` and ``operator_stop`` are evaluated **regardless of the configured**
    ``StopCondition.type``: a run configured to stop at ``turn_reached(50)`` must still recognise an
    actual victory or defeat that happens on turn 30, and an operator's explicit stop command is
    always honoured whenever it is issued (FR-004), not only on a run configured with
    ``operator_stop`` as its primary condition. ``turn_reached`` is the one condition gated on the
    configuration actually naming it -- a ``game_outcome``-configured run does not stop merely
    because some unrelated turn number was reached.

    Returns ``StopDecision(resolution=None, coincident=())`` when nothing has triggered a stop yet.
    """
    triggered: list[StopResolution] = []

    if evaluation.unrecoverable_failure:
        triggered.append(StopResolution.UNRECOVERABLE_FAILURE)
    if evaluation.game_outcome is GameOutcome.VICTORY:
        triggered.append(StopResolution.VICTORY)
    elif evaluation.game_outcome is GameOutcome.DEFEAT:
        triggered.append(StopResolution.DEFEAT)
    if evaluation.operator_stop_requested:
        triggered.append(StopResolution.OPERATOR_STOP)
    if (
        isinstance(stop_condition, TurnReachedStopCondition)
        and evaluation.current_turn >= stop_condition.turn
    ):
        triggered.append(StopResolution.TURN_REACHED)

    if not triggered:
        return StopDecision(resolution=None, coincident=())

    resolution, *coincident = triggered
    return StopDecision(resolution=resolution, coincident=tuple(coincident))
