"""Decision and ActionExecution (T024).

data-model.md SS9. The single action the agent issued at one decision step,
recorded with its out-of-game execution metadata (FR-012).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from civsim_harness.models.common import (
    DecisionId,
    DecisionStepId,
    DeclarationId,
    HarnessModel,
    ModelCallId,
    Timestamp,
)


class DecisionTrigger(StrEnum):
    """Why the agent issued this decision (FR-010)."""

    PROACTIVE = "proactive"
    PROMPT_RESPONSE = "prompt_response"


class ExecutionOutcome(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"
    PARTIALLY_APPLIED = "partially_applied"


class RejectionReason(StrEnum):
    NOT_IN_CATALOG = "not_in_catalog"
    UNAVAILABLE_TO_HUMAN_NOW = "unavailable_to_human_now"
    ILLEGAL_IN_CONTEXT = "illegal_in_context"
    OUT_OF_PARITY_CAMERA = "out_of_parity_camera"
    VERIFICATION_FAILED = "verification_failed"


class ActionExecution(HarnessModel):
    """The outcome of dispatching one decision's action, and how it was verified.

    ``outcome`` is derived from the action's declared verification predicate
    and must never be asserted directly by an executor (FR-011) -- this
    model cannot enforce *that* on its own (it has no access to the
    predicate evaluator), but it does enforce the one thing it can:
    ``rejection_reason`` is present exactly when ``outcome == rejected`` or
    ``partially_applied`` is not itself a rejection, so a reason is
    meaningless there too. ``applied`` never carries a rejection reason.
    """

    outcome: ExecutionOutcome
    rejection_reason: RejectionReason | None = None
    # The predicate declared on the action entry, and its observed result.
    verification: dict[str, Any] = Field(default_factory=dict)
    verified_at: Timestamp

    @model_validator(mode="after")
    def _rejection_reason_matches_outcome(self) -> ActionExecution:
        if self.outcome == ExecutionOutcome.REJECTED and self.rejection_reason is None:
            raise ValueError("a rejected ActionExecution must record its rejection_reason")
        if self.outcome != ExecutionOutcome.REJECTED and self.rejection_reason is not None:
            raise ValueError("rejection_reason must be unset unless outcome == rejected")
        return self


class Decision(HarnessModel):
    """The single action the agent issued at one decision step (FR-012).

    ``decision_step_id`` replaces any turn-level ``order_index`` from an
    earlier revision -- position within the turn comes entirely from the
    step's own ``step_index``. ``prompt_type`` is required exactly when
    ``trigger == prompt_response`` (FR-010), enforced below.
    """

    decision_id: DecisionId
    decision_step_id: DecisionStepId
    # Catalog entry -- required, including for end-turn (turn.end_turn).
    action_declaration_id: DeclarationId
    parameters: dict[str, Any] = Field(default_factory=dict)
    # As stated by the agent.
    reasoning: str
    trigger: DecisionTrigger
    prompt_type: str | None = None
    # True for the declared end-turn action (FR-008).
    is_end_turn: bool = False
    model_call_id: ModelCallId
    execution: ActionExecution

    @model_validator(mode="after")
    def _prompt_type_matches_trigger(self) -> Decision:
        if self.trigger == DecisionTrigger.PROMPT_RESPONSE and self.prompt_type is None:
            raise ValueError("prompt_type is required when trigger == prompt_response")
        if self.trigger == DecisionTrigger.PROACTIVE and self.prompt_type is not None:
            raise ValueError("prompt_type must be unset when trigger == proactive")
        return self
