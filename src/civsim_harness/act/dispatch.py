"""Action dispatch (T107).

Resolves the step's single requested action to a catalog declaration, evaluates its
``availability_predicate``, and rejects an unregistered action as ``not_in_catalog`` and an
unavailable one as ``unavailable_to_human_now`` -- with the reason recorded and **the action never
performed** (FR-017). This module never calls Nexus and never executes anything; its only job is
deciding whether an attempt may proceed at all. Actual execution lives wherever the run loop drives
the catalog's own ``capability_id``/Lua path (T110, a later wave); confirming what that execution
did lives in :mod:`civsim_harness.act.verify` (T108).

A rejection here is exactly the signal the no-progress backstop (``run/no_progress.py``, T111, a
later wave) increments its counter on (FR-014) -- this module does not touch that counter itself
(it has no turn-attempt state to hold one), it only produces the :class:`DispatchOutcome` the run
loop feeds into it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from civsim_harness.act.predicates import (
    PredicateEvaluationError,
    build_predicate_bindings,
    evaluate_predicate,
)
from civsim_harness.capability.registry import CapabilityRegistry, WrongContextError
from civsim_harness.errors import CatalogError
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration
from civsim_harness.models.common import DeclarationId, LuaContext, Timestamp
from civsim_harness.models.decision import ActionExecution, ExecutionOutcome, RejectionReason
from civsim_harness.models.turn import Observation


class DispatchStatus(Enum):
    """The outcome of one :func:`dispatch_action` call."""

    authorized = "authorized"
    """The declaration exists, is declared for the requested context, and its
    ``availability_predicate`` evaluated truthy -- the caller may proceed to execute it."""

    rejected = "rejected"
    """The action may not be attempted -- see ``rejection_reason``. Never performed."""


@dataclass(frozen=True)
class DispatchOutcome:
    """The tagged outcome of :func:`dispatch_action` (mirrors the ``CaptureResult``/``InputResult``
    "report, never raise for an expected-but-unhappy state" pattern already used by ``host.port``).
    """

    status: DispatchStatus
    declaration: ParityDeclaration | None = None
    rejection_reason: RejectionReason | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is DispatchStatus.authorized:
            if self.declaration is None:
                raise ValueError(
                    "DispatchOutcome.status is 'authorized' but no declaration was supplied"
                )
            if self.rejection_reason is not None:
                raise ValueError(
                    "DispatchOutcome.status is 'authorized' but a rejection_reason was supplied"
                )
        else:
            if self.declaration is not None:
                raise ValueError(
                    "DispatchOutcome.status is 'rejected' but a declaration was supplied"
                )
            if self.rejection_reason is None:
                raise ValueError(
                    "DispatchOutcome.status is 'rejected' but no rejection_reason was given"
                )


def dispatch_action(
    *,
    registry: CapabilityRegistry,
    context: LuaContext,
    action_declaration_id: DeclarationId,
    observation: Observation,
    target: Any = None,
) -> DispatchOutcome:
    """Resolve *action_declaration_id* and evaluate its availability, never executing anything.

    Rejection reasons:

    - :attr:`~civsim_harness.models.decision.RejectionReason.NOT_IN_CATALOG` --
      *action_declaration_id* does not resolve in *registry*, or resolves to a non-action
      declaration (an observation/view id supplied where an action was expected is not a
      catalog action either).
    - :attr:`~civsim_harness.models.decision.RejectionReason.ILLEGAL_IN_CONTEXT` -- the declaration
      exists but is declared for the other Lua context (research R3): a
      :class:`~civsim_harness.capability.registry.WrongContextError` from
      :meth:`~civsim_harness.capability.registry.CapabilityRegistry.authorize`.
    - :attr:`~civsim_harness.models.decision.RejectionReason.UNAVAILABLE_TO_HUMAN_NOW` -- the
      declaration's ``availability_predicate`` evaluated falsy, or could not be evaluated at all
      (a predicate that cannot be evaluated is treated as unavailable, never as available by
      default -- fail closed, matching FR-011's "never assert without verification" spirit applied
      to availability rather than to the executor's own claim).
    """
    try:
        declaration = registry.resolve(action_declaration_id)
    except CatalogError as exc:
        return DispatchOutcome(
            status=DispatchStatus.rejected,
            rejection_reason=RejectionReason.NOT_IN_CATALOG,
            detail={"action_declaration_id": str(action_declaration_id), "reason": exc.message},
        )

    if declaration.kind is not DeclarationKind.ACTION:
        return DispatchOutcome(
            status=DispatchStatus.rejected,
            rejection_reason=RejectionReason.NOT_IN_CATALOG,
            detail={
                "action_declaration_id": str(action_declaration_id),
                "reason": (
                    f"declaration_id resolves to kind={declaration.kind.value}, not an action"
                ),
            },
        )

    try:
        declaration = registry.authorize(action_declaration_id, context)
    except WrongContextError as exc:
        return DispatchOutcome(
            status=DispatchStatus.rejected,
            rejection_reason=RejectionReason.ILLEGAL_IN_CONTEXT,
            # exc.detail is merged before "reason" is set so it can never clobber this explicit,
            # already-informative message with its own (absent, here, but not guaranteed in
            # general) "reason" key -- see the identical fix in act.verify._rejected.
            detail={
                "action_declaration_id": str(action_declaration_id),
                **exc.detail,
                "reason": exc.message,
            },
        )

    assert declaration.availability_predicate is not None  # guaranteed for kind == action

    bindings = build_predicate_bindings(observation=observation, target=target)
    try:
        available = evaluate_predicate(declaration.availability_predicate, bindings)
    except PredicateEvaluationError as exc:
        return DispatchOutcome(
            status=DispatchStatus.rejected,
            rejection_reason=RejectionReason.UNAVAILABLE_TO_HUMAN_NOW,
            detail={
                "action_declaration_id": str(action_declaration_id),
                "predicate": declaration.availability_predicate,
                **exc.detail,
                "reason": f"availability_predicate could not be evaluated: {exc.message}",
            },
        )

    if not available:
        return DispatchOutcome(
            status=DispatchStatus.rejected,
            rejection_reason=RejectionReason.UNAVAILABLE_TO_HUMAN_NOW,
            detail={
                "action_declaration_id": str(action_declaration_id),
                "predicate": declaration.availability_predicate,
            },
        )

    return DispatchOutcome(status=DispatchStatus.authorized, declaration=declaration)


def rejection_to_execution(outcome: DispatchOutcome, *, verified_at: Timestamp) -> ActionExecution:
    """Build the :class:`~civsim_harness.models.decision.ActionExecution` for a rejected dispatch.

    A dispatch-time rejection is never executed, so it is never verified either -- there is
    nothing for :mod:`civsim_harness.act.verify` to do with it. This is the direct record of "the
    action never performed" (FR-017): ``outcome=rejected`` with the same ``rejection_reason``
    :func:`dispatch_action` produced, and *outcome.detail* preserved verbatim under
    ``verification["dispatch_detail"]`` so the reason survives into the persisted record without
    this module needing to know anything about how the caller assembles the surrounding
    :class:`~civsim_harness.models.decision.Decision`.
    """
    if outcome.status is not DispatchStatus.rejected:
        raise ValueError("rejection_to_execution requires a rejected DispatchOutcome")
    assert outcome.rejection_reason is not None
    return ActionExecution(
        outcome=ExecutionOutcome.REJECTED,
        rejection_reason=outcome.rejection_reason,
        verification={"dispatch_detail": dict(outcome.detail)},
        verified_at=verified_at,
    )
