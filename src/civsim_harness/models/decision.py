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
    #: The declaration says which positional arguments its Lua takes
    #: (``ParityDeclaration.lua_arguments``) and this decision did not supply one of them. Its
    #: own reason rather than ``unavailable_to_human_now``: the game was offering the command --
    #: nothing is greyed out -- the *decision* was incomplete, and folding the two together would
    #: make a malformed decision read, in coverage, as a board that never offered the action.
    MISSING_REQUIRED_ARGUMENT = "missing_required_argument"


class ActionExecution(HarnessModel):
    """The outcome of dispatching one decision's action, and how it was verified.

    ``outcome`` is derived from the action's declared verification predicate
    and must never be asserted directly by an executor (FR-011). This model
    has no predicate evaluator, so it cannot re-derive the verdict; what it
    *can* enforce -- and does, below -- is that ``applied`` never exists
    without the confirming evidence: an ``applied`` execution must carry a
    non-empty ``verification`` whose ``result`` is ``True``. That is FR-011's
    checkable half. "No action is recorded as applied without verification"
    stops being a property of how :mod:`civsim_harness.act.verify` happens to
    be written today and becomes something the record itself refuses to hold;
    a fast path answering ``applied`` straight off a ``{ok=true}`` dispatch,
    skipping the post-observation re-read, cannot be constructed at all.
    Which *one* module may derive that verdict is asserted structurally
    alongside it, in ``tests/unit/test_verification.py``.

    The second rule is bookkeeping: ``rejection_reason`` is present exactly
    when ``outcome == rejected``. ``partially_applied`` is not itself a
    rejection, so a reason is meaningless there too, and ``applied`` never
    carries one.
    """

    outcome: ExecutionOutcome
    rejection_reason: RejectionReason | None = None
    # The predicate declared on the action entry, and its observed result.
    verification: dict[str, Any] = Field(default_factory=dict)
    #: What the action's own Lua answered when it was dispatched -- its ``{ok, reason, ...}``
    #: table, verbatim -- or ``None`` when no dispatch happened (the decision was rejected before
    #: execution) or the capability returned nothing.
    #:
    #: MEASURED LIVE 2026-09-21 (Stage 5, run-ba3ad80d): ``cities.set_production`` was refused 24
    #: times by its own Lua with ``city_not_found`` -- the item name was arriving in the city-id
    #: parameter -- and every one of those refusals reached the ledger as a bare
    #: ``verification_failed``, because the dispatch answer was discarded and only the predicate's
    #: verdict was recorded. "The game refused this order, and here is the game's word for why" and
    #: "the effect was not visible afterwards" are different facts; recording only the second one
    #: cost a whole live stage to diagnose. Kept deliberately separate from ``verification``: this
    #: is what the order said, that is what the board said.
    dispatch_result: dict[str, Any] | None = None
    verified_at: Timestamp

    @model_validator(mode="after")
    def _rejection_reason_matches_outcome(self) -> ActionExecution:
        if self.outcome == ExecutionOutcome.REJECTED and self.rejection_reason is None:
            raise ValueError("a rejected ActionExecution must record its rejection_reason")
        if self.outcome != ExecutionOutcome.REJECTED and self.rejection_reason is not None:
            raise ValueError("rejection_reason must be unset unless outcome == rejected")
        return self

    @model_validator(mode="after")
    def _applied_carries_its_confirming_verification(self) -> ActionExecution:
        if self.outcome != ExecutionOutcome.APPLIED:
            return self
        if not self.verification:
            raise ValueError(
                "an applied ActionExecution must record the verification that confirmed it; "
                "an empty verification means nothing re-read the board (FR-011)"
            )
        if self.verification.get("result") is not True:
            raise ValueError(
                "an applied ActionExecution must record verification['result'] is True -- the "
                "declared predicate's own verdict, evaluated against the post-action "
                "observation; got "
                f"{self.verification.get('result')!r} (FR-011)"
            )
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
