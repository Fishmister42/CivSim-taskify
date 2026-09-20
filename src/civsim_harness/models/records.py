"""ModelCall, SavePoint, RunEvent (T026).

data-model.md SS12-14.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from civsim_harness.models.common import (
    Cost,
    DecisionStepId,
    EventId,
    HarnessModel,
    ModelCallId,
    ModelRef,
    RunId,
    SavePointId,
    Timestamp,
    TurnCycleId,
)
from civsim_harness.telemetry.redaction import redact_value

# --------------------------------------------------------------------------
# 12. ModelCall
# --------------------------------------------------------------------------


class CallOutcome(StrEnum):
    """A successful call returns exactly one decision -- ``DECISION_RETURNED`` is
    singular by design, since a call serves exactly one decision step
    (contracts/model-provider-port.md). ``EMPTY_RESPONSE`` is a failed call,
    never a decision to do nothing.
    """

    DECISION_RETURNED = "decision_returned"
    EMPTY_RESPONSE = "empty_response"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    CONTEXT_REJECTED = "context_rejected"


class ModelCall(HarnessModel):
    """One request to the provider layer for one decision step's decision (FR-040).

    Attributed to the step it served: a turn contains as many ``ModelCall``
    records as it contained decision steps, and no fewer, since a step
    without a ``model_call_id`` cannot be constructed (FR-042, SC-012, see
    ``DecisionStep`` in models/turn.py).
    """

    model_call_id: ModelCallId
    run_id: RunId
    turn_cycle_id: TurnCycleId
    decision_step_id: DecisionStepId
    model_requested: ModelRef
    # May differ from model_requested -- fallback.
    model_served: ModelRef
    latency_ms: int = Field(ge=0)
    # From provider-reported usage; not independently priced.
    cost: Cost
    retry_count: int = Field(ge=0)
    fallback_occurred: bool
    # Images actually sent; must equal the observation's image count
    # (FR-039, SC-017) -- enforced by the caller that has both records, not
    # here, since this model has no access to the Observation it served.
    image_count: int = Field(ge=0)
    outcome: CallOutcome


# --------------------------------------------------------------------------
# 13. SavePoint
# --------------------------------------------------------------------------

_SAVE_NAME_PATTERN = re.compile(r"^civsim__.+__t\d{4,}$")


class RetentionStatus(StrEnum):
    """``ELIGIBLE`` has exactly one cause: ``Run.archived_at`` is set (FR-036, R17).

    Not age, not a quota, not a retention window, not the run reaching a
    terminal state, not thinning -- see contracts/match-store-port.md A2.
    This model cannot enforce that cross-record invariant on its own (it
    would need the owning ``Run``); it is enforced by ``archive_run`` in the
    store adapter, which is the *only* code path permitted to produce this
    value.
    """

    RETAINED = "retained"
    ELIGIBLE = "eligible"
    REMOVED = "removed"


class SavePoint(HarnessModel):
    """A named, addressable save bound to a run and turn (FR-032).

    Addressable by run, turn, and lineage without inspecting the filesystem
    or the game client -- ``save_name`` is a naming convention
    (``civsim__<run_id>__t<turn:04d>``), not the address.
    """

    save_point_id: SavePointId
    run_id: RunId
    turn_number: int = Field(ge=1)
    save_name: str = Field(pattern=_SAVE_NAME_PATTERN.pattern)
    taken_at: Timestamp
    # Filesystem-confirmed, size-stable (R5).
    verified: bool
    # Parent run and turn where applicable (branch lineage).
    lineage: dict[str, Any] = Field(default_factory=dict)
    retention_status: RetentionStatus
    # Set when recovery finds the save absent.
    missing: bool = False


# --------------------------------------------------------------------------
# 14. RunEvent
# --------------------------------------------------------------------------


class RunEventType(StrEnum):
    """The full set of non-turn occurrences on a run's timeline (data-model.md SS14).

    ``stall`` is deliberately absent: it denoted a turn exceeding its time
    budget, and there is no time budget any more. A backstop-ended turn is
    ``turn_ended_on_no_progress`` -- a normal, recorded turn ending, not a
    fault -- while a genuinely stuck *game* surfaces as ``hang_detected`` or
    ``unknown_screen``.
    """

    LIFECYCLE_TRANSITION = "lifecycle_transition"
    LIFECYCLE_COMMAND_RECEIVED = "lifecycle_command_received"
    PREPARATION_MISMATCH = "preparation_mismatch"
    CRASH_DETECTED = "crash_detected"
    HANG_DETECTED = "hang_detected"
    UNRESPONSIVE_DETECTED = "unresponsive_detected"
    UNKNOWN_SCREEN = "unknown_screen"
    SAVE_TAKEN = "save_taken"
    SAVE_FAILED = "save_failed"
    SAVE_MISSING = "save_missing"
    RESUMED = "resumed"
    TURN_ABANDONED = "turn_abandoned"
    TURN_ENDED_ON_NO_PROGRESS = "turn_ended_on_no_progress"
    OBSERVATION_ASSEMBLY_FAILED = "observation_assembly_failed"
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_RETRY = "provider_retry"
    PROVIDER_FALLBACK = "provider_fallback"
    CONTEXT_REJECTED = "context_rejected"
    MODEL_CHAIN_EXHAUSTED = "model_chain_exhausted"
    IMAGE_WITHHELD = "image_withheld"
    CAPTURE_FAILED = "capture_failed"
    BRANCH_CREATED = "branch_created"
    BRANCH_ABANDONED = "branch_abandoned"
    RUN_ARCHIVED = "run_archived"
    GAME_BUILD_CHANGE_ACCEPTED = "game_build_change_accepted"
    DISK_HEADROOM_LOW = "disk_headroom_low"
    PERSISTENCE_FAILURE = "persistence_failure"
    RECOVERY_LIMIT_REACHED = "recovery_limit_reached"


class RunEvent(HarnessModel):
    """Any non-turn occurrence on a run's timeline.

    ``detail`` is redacted on the way in -- data-model.md's own annotation
    for this field is "type-specific, credential-redacted", so the
    redaction is applied here structurally rather than left to whoever
    constructs the event to remember (T014, FR-043, SC-018).
    """

    event_id: EventId
    run_id: RunId
    turn_number: int | None = None
    step_index: int | None = None
    event_type: RunEventType
    occurred_at: Timestamp
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("detail", mode="before")
    @classmethod
    def _redact_detail(cls, value: Any) -> Any:
        if value is None:
            return {}
        return redact_value(dict(value))
