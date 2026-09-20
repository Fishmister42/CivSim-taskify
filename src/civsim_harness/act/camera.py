"""Camera validation (T132, T125; research R8, FR-026).

Camera moves, zoom changes, and view-mode toggles are declared actions
(``catalogs/actions/camera.yaml``) precisely because, once images enter the agent's context, the
camera *is* an information channel: pointing it at an unrevealed plot and capturing it would hand
the agent fog-of-war contents through the image channel, bypassing the structured parity filter
entirely (research R8, Principle I). Declaring camera control as an action rather than treating it
as harness plumbing is what makes a camera request rejectable and auditable, exactly like an
illegal unit order.

**Why this is not just another call to ``act.dispatch.dispatch_action``.** That function's generic
availability-predicate failure is reported as ``RejectionReason.UNAVAILABLE_TO_HUMAN_NOW`` --
correct for an ordinary action, but a camera parity violation is categorically different: it is an
attempted image-channel leak, not merely "not available right now". FR-026 names its own rejection
reason for exactly that distinction, ``RejectionReason.OUT_OF_PARITY_CAMERA``, and this module is
the one place that produces it.

**Three checks, one per declared camera action**, each read from
``catalogs/actions/camera.yaml``'s own ``availability_predicate`` -- via the same restricted
evaluator ``act.dispatch``/``act.verify`` already use -- rather than a second, hand-maintained copy
of the bound (R9's "no shortcut" discipline: the catalog stays the single source of truth):

- ``camera.move`` -- the requested target plot must be one the run has revealed
  (``target.is_revealed``).
- ``camera.zoom`` -- the requested zoom must fall within the range the standard UI permits
  (``target >= 0.05 and target <= 1.0``).
- ``camera.set_view_mode`` -- the requested mode must be one a human can toggle directly
  (``target == "world" or target == "strategic"``); the other three views in
  ``catalogs/observations/views.yaml`` (``city_screen``, ``diplomacy``, ``congress``) are reached by
  their own screen-opening actions, not this generic toggle.

This module returns the same :class:`~civsim_harness.act.dispatch.DispatchOutcome` shape
``act.dispatch.dispatch_action`` returns, deliberately -- so a rejected outcome from here feeds
straight into ``act.dispatch.rejection_to_execution`` unmodified, and an authorized one carries the
resolved declaration ready for the run loop to dispatch exactly like any other authorized action.
Validation only: this module never calls Nexus and never executes anything, matching every other
pure builder in this wave (``act.dispatch``, ``act.verify``).
"""

from __future__ import annotations

from typing import Any, Final

from civsim_harness.act.dispatch import DispatchOutcome, DispatchStatus
from civsim_harness.act.predicates import PredicateEvaluationError, evaluate_predicate
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError
from civsim_harness.models.common import DeclarationId
from civsim_harness.models.decision import RejectionReason

#: The three declared camera actions this module validates (``catalogs/actions/camera.yaml``,
#: T131). No other action is ever routed through here -- a caller must still use
#: ``act.dispatch.dispatch_action`` for anything else.
CAMERA_MOVE: Final[DeclarationId] = DeclarationId("camera.move")
CAMERA_ZOOM: Final[DeclarationId] = DeclarationId("camera.zoom")
CAMERA_SET_VIEW_MODE: Final[DeclarationId] = DeclarationId("camera.set_view_mode")

CAMERA_ACTION_DECLARATION_IDS: Final[frozenset[DeclarationId]] = frozenset(
    {CAMERA_MOVE, CAMERA_ZOOM, CAMERA_SET_VIEW_MODE}
)


def validate_camera_action(
    *,
    registry: CapabilityRegistry,
    action_declaration_id: DeclarationId,
    target: Any,
) -> DispatchOutcome:
    """Validate one of the three declared camera actions against its own
    ``availability_predicate``, producing ``OUT_OF_PARITY_CAMERA`` on failure rather than
    ``act.dispatch.dispatch_action``'s generic ``UNAVAILABLE_TO_HUMAN_NOW`` (FR-026, research R8).

    *target* is whatever the decision named as the action's parameter -- a plot-shaped value
    carrying ``is_revealed`` for ``camera.move``, a plain number for ``camera.zoom``, a plain
    string for ``camera.set_view_mode``. Bound to the predicate's ``target`` name exactly the way
    ``act.dispatch.dispatch_action`` binds its own *target* parameter; none of the three declared
    predicates reference any other namespace today, so no ``Observation`` is needed to evaluate
    them.

    Returns ``DispatchOutcome(status=authorized, declaration=...)`` once the predicate evaluates
    truthy -- ready for the caller to dispatch exactly like any other authorized action; actual
    execution and post-execution verification reuse the same generic machinery every other
    declared action uses (the action's own ``capability_id``/Lua path, then
    ``act.verify.verify_execution`` against its ``verification_predicate``), since nothing about a
    *validated* camera action is special.

    Rejects with :attr:`~civsim_harness.models.decision.RejectionReason.NOT_IN_CATALOG` for any
    *action_declaration_id* outside :data:`CAMERA_ACTION_DECLARATION_IDS` -- this module only ever
    validates the three declared camera actions, never a general-purpose replacement for
    ``dispatch_action``. Never asserts availability when the predicate cannot be evaluated (e.g. a
    non-numeric zoom target): an unevaluable predicate is rejected as
    ``OUT_OF_PARITY_CAMERA``, the same fail-closed discipline ``act.dispatch.dispatch_action``
    already applies to its own predicate evaluation (FR-011's spirit applied to camera parity, not
    just to verification).
    """
    if action_declaration_id not in CAMERA_ACTION_DECLARATION_IDS:
        return DispatchOutcome(
            status=DispatchStatus.rejected,
            rejection_reason=RejectionReason.NOT_IN_CATALOG,
            detail={
                "action_declaration_id": str(action_declaration_id),
                "reason": (
                    "act.camera only validates the declared camera.* actions "
                    f"({sorted(str(d) for d in CAMERA_ACTION_DECLARATION_IDS)})"
                ),
            },
        )

    try:
        declaration = registry.resolve(action_declaration_id)
    except CatalogError as exc:
        return DispatchOutcome(
            status=DispatchStatus.rejected,
            rejection_reason=RejectionReason.NOT_IN_CATALOG,
            detail={"action_declaration_id": str(action_declaration_id), "reason": exc.message},
        )

    assert declaration.availability_predicate is not None  # guaranteed for kind == action

    bindings = {"target": target}
    try:
        available = evaluate_predicate(declaration.availability_predicate, bindings)
    except PredicateEvaluationError as exc:
        return DispatchOutcome(
            status=DispatchStatus.rejected,
            rejection_reason=RejectionReason.OUT_OF_PARITY_CAMERA,
            detail={
                "action_declaration_id": str(action_declaration_id),
                "predicate": declaration.availability_predicate,
                **exc.detail,
                "reason": f"camera availability_predicate could not be evaluated: {exc.message}",
            },
        )

    if not available:
        return DispatchOutcome(
            status=DispatchStatus.rejected,
            rejection_reason=RejectionReason.OUT_OF_PARITY_CAMERA,
            detail={
                "action_declaration_id": str(action_declaration_id),
                "predicate": declaration.availability_predicate,
                "target": target,
            },
        )

    return DispatchOutcome(status=DispatchStatus.authorized, declaration=declaration)
