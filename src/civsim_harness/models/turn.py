"""TurnCycle, DecisionStep, Observation, ObservationEntry, ScreenCapture (T021-T023).

data-model.md SS5-8. ``DecisionStep`` is the load-bearing addition of the
clarification session: the turn is a loop, not a phase (FR-008), so the
observation, the images, and the model call all hang off the step rather
than the turn.

A ``TurnCycle`` here models the *finished* attempt -- the shape
``write_turn_cycle`` receives as one atomic unit (contracts/match-store-port.md
D3), not an in-progress builder. By the time one of these exists, its
``outcome`` is always known.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from civsim_harness.models.common import (
    CaptureId,
    CapturePath,
    CatalogVersionRef,
    DecisionId,
    DecisionStepId,
    DeclarationId,
    HarnessModel,
    LuaContext,
    ModelCallId,
    ObservationId,
    RunId,
    SavePointId,
    Timestamp,
    TurnCycleId,
)

# --------------------------------------------------------------------------
# 5. TurnCycle
# --------------------------------------------------------------------------


class TurnOutcome(StrEnum):
    """How a turn attempt ended. ``abandoned`` is not an ending; see class docs.

    ``end_turn_unconfirmed`` is the agent's own end turn **dispatched but never confirmed**
    (research R14, revised 2026-09-21 after gameplay block 7): the harness's turn ended, the
    game's did not, and the record must not claim the agent ended it. It is a distinct value
    rather than a flag on ``ended_by_agent`` precisely so no reader can mistake the two -- block
    7 (``run-480aa573``) recorded five consecutive ``ended_by_agent`` cycles at game turn 35,
    each one an end turn that was dispatched and then ``verification_failed`` after the 45 s
    bound. The harness's own turn still advances on this outcome, so the loop cannot spin; what
    changes is what the record says happened.
    """

    ENDED_BY_AGENT = "ended_by_agent"
    ENDED_ON_NO_PROGRESS = "ended_on_no_progress"
    END_TURN_UNCONFIRMED = "end_turn_unconfirmed"
    ABANDONED = "abandoned"


class TurnCycle(HarnessModel):
    """One attempt at one turn of one run (data-model.md SS5).

    No ``observation_id`` field exists here by design: observations are
    per decision step (see ``DecisionStep``/``Observation`` below), not
    per turn. ``save_point_id`` is non-nullable -- a failed quicksave means
    the turn attempt never comes into existence at all (FR-007, SC-004).
    ``step_count`` is bounded only below (``ge=1``); there is deliberately
    no upper bound, since a turn may run for as many steps and as long as
    the agent needs (FR-008, FR-014, invariant I16).
    """

    turn_cycle_id: TurnCycleId
    run_id: RunId
    turn_number: int = Field(ge=1)
    # 0 for the first attempt; increments on replay.
    attempt_index: int = Field(ge=0)
    # Exactly one authoritative attempt per (run_id, turn_number) (FR-047).
    is_authoritative: bool
    save_point_id: SavePointId
    # Number of decision steps in this attempt. No upper bound: any cap here
    # would violate FR-014 directly (invariant I16).
    step_count: int = Field(ge=1)
    outcome: TurnOutcome
    final_no_progress_streak: int = Field(ge=0)
    # Derived: true if any step in this attempt ran without its images (FR-050).
    visually_degraded: bool
    game_turn_advanced: bool | None = None
    """Did the *game's* own turn counter advance as a result of this attempt (research R14,
    revised 2026-09-21)?

    ``True`` only when the agent's end turn was dispatched **and** confirmed by the declared
    readback within its bound; ``False`` when it was dispatched and never confirmed
    (``outcome = end_turn_unconfirmed``). ``None`` means *not recorded*, and is the honest answer
    in three cases, never a stand-in for ``True``: a record written before this field existed
    (block 7's own five cycles at game turn 35), an ``abandoned`` attempt that never reached an
    ending at all, and ``ended_on_no_progress`` -- whose harness-issued end turn is dispatched by
    ``run/turn_cycle.py`` strictly *after* this record is already durable (invariant I3), so the
    answer is genuinely unknown at write time and its failure surfaces as
    ``BackstopEndTurnNotConfirmed`` instead.

    Optional with a ``None`` default so a 002-era record still validates unchanged: the store
    file keeps the whole ``TurnCycle`` as JSON, so no column and no migration are involved, and
    the trending-eligibility rule (``store/completeness.py``) covers the historical ``None`` case
    by comparing consecutive cycles' recorded game turn numbers instead.
    """
    yields: dict[str, Any] = Field(default_factory=dict)
    started_at: Timestamp
    ended_at: Timestamp
    # Must precede the end-turn action (FR-013).
    persisted_at: Timestamp


# --------------------------------------------------------------------------
# 6. DecisionStep
# --------------------------------------------------------------------------


class StepProgress(StrEnum):
    """Derived from verification, never asserted -- feeds the no-progress counter."""

    CHANGED_STATE = "changed_state"
    NO_CHANGE = "no_change"
    REJECTED = "rejected"


class DecisionStep(HarnessModel):
    """One iteration of the within-turn loop (data-model.md SS6, FR-008, FR-012).

    Exactly one decision and one model call per step (invariant I13) is
    enforced structurally: ``decision_id`` and ``model_call_id`` are both
    required singular ids, not lists, so a step carrying a batch cannot be
    constructed at all.
    """

    decision_step_id: DecisionStepId
    turn_cycle_id: TurnCycleId
    # 1-based, contiguous within the attempt.
    step_index: int = Field(ge=1)
    # Assembled fresh for *this* step -- required, and never reused across
    # steps (FR-008, FR-015, invariant I14).
    observation_id: ObservationId
    decision_id: DecisionId
    model_call_id: ModelCallId
    progress: StepProgress
    no_progress_streak_after: int = Field(ge=0)
    visually_degraded: bool
    started_at: Timestamp
    ended_at: Timestamp


# --------------------------------------------------------------------------
# 7. Observation / ObservationEntry
# --------------------------------------------------------------------------


class ObservationEntry(HarnessModel):
    """One piece of structured state, attributed to the catalog entry that produced it.

    ``declaration_id`` is required -- there is no unattributed-value path
    (FR-016, SC-007).
    """

    declaration_id: DeclarationId
    key: str
    value: Any = None
    context: LuaContext


class Observation(HarnessModel):
    """The view assembled for one decision step (FR-012, FR-024).

    ``decision_step_id`` is step-scoped, not turn-scoped: this is what makes
    the observation the effect of the *previous* step's decision, assembled
    fresh every time (FR-008).
    """

    observation_id: ObservationId
    decision_step_id: DecisionStepId
    assembled_at: Timestamp
    catalog_version: CatalogVersionRef
    entries: list[ObservationEntry] = Field(default_factory=list)
    # ScreenCapture references shown to the agent at this step.
    captures: list[CaptureId] = Field(default_factory=list)
    # Which game screen was up (R13).
    screen_identity: str


# --------------------------------------------------------------------------
# 8. ScreenCapture
# --------------------------------------------------------------------------


class ScreeningStatus(StrEnum):
    SCREENED_CLEAN = "screened_clean"
    WITHHELD = "withheld"


class WithheldReason(StrEnum):
    NON_PLAYER_UI = "non_player_ui"
    GEOMETRY_MISMATCH = "geometry_mismatch"
    PROVENANCE_FAILURE = "provenance_failure"
    CAPTURE_FAILED = "capture_failed"


class ScreenCapture(HarnessModel):
    """An image of the game's own view, bound to one run, turn, and decision step
    (FR-015, FR-025, FR-030).

    A withheld capture is never stored and never shown (SC-019): the
    ``model_validator`` below enforces that structurally --
    ``screening_status == withheld`` forces ``blob_ref is None`` and
    ``shown_to_agent is False``, and requires a ``withheld_reason``; the
    reverse (``screened_clean``) forbids one.
    """

    capture_id: CaptureId
    run_id: RunId
    turn_number: int = Field(ge=1)
    # Captures are per step (FR-015) -- required binding.
    decision_step_id: DecisionStepId
    captured_at: Timestamp
    camera_state: dict[str, Any] = Field(default_factory=dict)
    view_declaration_id: DeclarationId
    screening_status: ScreeningStatus
    withheld_reason: WithheldReason | None = None
    shown_to_agent: bool
    retained_as_evidence: bool
    # Content-addressed; null when withheld.
    blob_ref: str | None = None
    capture_path: CapturePath

    @model_validator(mode="after")
    def _enforce_withholding_invariant(self) -> ScreenCapture:
        if self.screening_status == ScreeningStatus.WITHHELD:
            if self.withheld_reason is None:
                raise ValueError("a withheld capture must record its withheld_reason")
            if self.blob_ref is not None:
                raise ValueError("a withheld capture must not carry a blob_ref (SC-019)")
            if self.shown_to_agent:
                raise ValueError("a withheld capture must not be shown_to_agent")
        else:  # screened_clean
            if self.withheld_reason is not None:
                raise ValueError("withheld_reason must be unset when screening_status is clean")
        if self.shown_to_agent and self.screening_status != ScreeningStatus.SCREENED_CLEAN:
            raise ValueError("shown_to_agent requires screening_status == screened_clean")
        return self
