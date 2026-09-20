"""Unit tests for camera validation (T125; FR-026, research R8).

Each of the three declared camera actions (``catalogs/actions/camera.yaml``) is exercised once
rejected and once authorized: an unrevealed target plot, an out-of-range zoom, and a non-human
view mode are each rejected with ``out_of_parity_camera`` and recorded (never
``unavailable_to_human_now``, the generic reason ``act.dispatch.dispatch_action`` would produce for
the same predicate failure) -- the whole point of ``act.camera`` existing as a separate module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from civsim_harness.act.camera import (
    CAMERA_ACTION_DECLARATION_IDS,
    CAMERA_MOVE,
    CAMERA_SET_VIEW_MODE,
    CAMERA_ZOOM,
    validate_camera_action,
)
from civsim_harness.act.dispatch import DispatchOutcome, DispatchStatus, rejection_to_execution
from civsim_harness.capability.loader import Catalog, load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.models.common import DeclarationId
from civsim_harness.models.decision import ExecutionOutcome, RejectionReason

_REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGS_ROOT = _REPO_ROOT / "catalogs"


@pytest.fixture(scope="module")
def real_catalog() -> Catalog:
    """The real, checked-in ``catalogs/`` tree -- camera.py binds to the authored
    ``catalogs/actions/camera.yaml``, not a fixture stand-in (task instructions)."""
    return load_catalog(CATALOGS_ROOT)


@pytest.fixture
def registry(real_catalog: Catalog) -> CapabilityRegistry:
    return CapabilityRegistry(catalog=real_catalog)


# --------------------------------------------------------------------------
# camera.move -- target must be a revealed plot
# --------------------------------------------------------------------------


def test_unrevealed_target_plot_is_rejected_out_of_parity(registry: CapabilityRegistry) -> None:
    outcome = validate_camera_action(
        registry=registry,
        action_declaration_id=CAMERA_MOVE,
        target={"x": 4, "y": 7, "is_revealed": False},
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA


def test_revealed_target_plot_is_authorized(registry: CapabilityRegistry) -> None:
    outcome = validate_camera_action(
        registry=registry,
        action_declaration_id=CAMERA_MOVE,
        target={"x": 4, "y": 7, "is_revealed": True},
    )

    assert outcome.status is DispatchStatus.authorized
    assert outcome.declaration is not None
    assert outcome.declaration.declaration_id == CAMERA_MOVE


# --------------------------------------------------------------------------
# camera.zoom -- zoom must be within the range the standard UI permits
# --------------------------------------------------------------------------


def test_out_of_range_zoom_is_rejected_out_of_parity(registry: CapabilityRegistry) -> None:
    outcome = validate_camera_action(
        registry=registry, action_declaration_id=CAMERA_ZOOM, target=5.0
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA


def test_negative_zoom_is_rejected_out_of_parity(registry: CapabilityRegistry) -> None:
    outcome = validate_camera_action(
        registry=registry, action_declaration_id=CAMERA_ZOOM, target=-0.1
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA


def test_in_range_zoom_is_authorized(registry: CapabilityRegistry) -> None:
    outcome = validate_camera_action(
        registry=registry, action_declaration_id=CAMERA_ZOOM, target=0.5
    )

    assert outcome.status is DispatchStatus.authorized


# --------------------------------------------------------------------------
# camera.set_view_mode -- mode must be one a human can toggle
# --------------------------------------------------------------------------


def test_non_human_view_mode_is_rejected_out_of_parity(registry: CapabilityRegistry) -> None:
    """``city_screen`` exists as a declared view (catalogs/observations/views.yaml) but is reached
    by its own screen-opening action, never this generic toggle."""
    outcome = validate_camera_action(
        registry=registry, action_declaration_id=CAMERA_SET_VIEW_MODE, target="city_screen"
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA


def test_nonexistent_view_mode_is_rejected_out_of_parity(registry: CapabilityRegistry) -> None:
    outcome = validate_camera_action(
        registry=registry, action_declaration_id=CAMERA_SET_VIEW_MODE, target="orbital"
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA


@pytest.mark.parametrize("mode", ["world", "strategic"])
def test_human_togglable_view_mode_is_authorized(
    registry: CapabilityRegistry, mode: str
) -> None:
    outcome = validate_camera_action(
        registry=registry, action_declaration_id=CAMERA_SET_VIEW_MODE, target=mode
    )

    assert outcome.status is DispatchStatus.authorized


# --------------------------------------------------------------------------
# Rejections are recorded, not silently dropped
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declaration_id", "target"),
    [
        (CAMERA_MOVE, {"x": 1, "y": 1, "is_revealed": False}),
        (CAMERA_ZOOM, 99.0),
        (CAMERA_SET_VIEW_MODE, "diplomacy"),
    ],
)
def test_rejection_is_recorded_as_an_action_execution(
    registry: CapabilityRegistry, declaration_id: DeclarationId, target: object
) -> None:
    """A camera rejection feeds straight into ``act.dispatch.rejection_to_execution`` unmodified
    -- proving the two modules' DispatchOutcome shapes are genuinely interchangeable -- and the
    action is never performed (FR-026)."""
    outcome = validate_camera_action(
        registry=registry, action_declaration_id=declaration_id, target=target
    )
    assert outcome.status is DispatchStatus.rejected

    execution = rejection_to_execution(outcome, verified_at=datetime(2026, 9, 20, tzinfo=UTC))

    assert execution.outcome is ExecutionOutcome.REJECTED
    assert execution.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA
    assert execution.verification["dispatch_detail"]["action_declaration_id"] == str(
        declaration_id
    )


# --------------------------------------------------------------------------
# Only the three declared camera actions are ever validated here
# --------------------------------------------------------------------------


def test_camera_action_declaration_ids_cover_exactly_the_declared_three() -> None:
    assert CAMERA_ACTION_DECLARATION_IDS == {CAMERA_MOVE, CAMERA_ZOOM, CAMERA_SET_VIEW_MODE}


def test_a_non_camera_action_is_rejected_as_not_in_catalog(registry: CapabilityRegistry) -> None:
    outcome = validate_camera_action(
        registry=registry,
        action_declaration_id=DeclarationId("turn.end_turn"),
        target=None,
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.NOT_IN_CATALOG


def test_unresolvable_declaration_id_is_rejected_as_not_in_catalog(
    registry: CapabilityRegistry,
) -> None:
    """camera.py never routes an unknown id through the availability predicate path -- membership
    in CAMERA_ACTION_DECLARATION_IDS is checked first, so this can never reach registry.resolve."""
    outcome = validate_camera_action(
        registry=registry,
        action_declaration_id=DeclarationId("camera.nonexistent"),
        target=None,
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.NOT_IN_CATALOG


def test_authorized_outcome_matches_dispatch_outcome_shape(
    registry: CapabilityRegistry,
) -> None:
    """The return type is genuinely act.dispatch.DispatchOutcome, not a lookalike."""
    outcome = validate_camera_action(
        registry=registry, action_declaration_id=CAMERA_ZOOM, target=0.5
    )

    assert isinstance(outcome, DispatchOutcome)
