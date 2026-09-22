"""Unit tests for camera validation (T125, T276; FR-026, research R8).

Each of the three declared camera actions (``catalogs/actions/camera.yaml``) is exercised once
rejected and once authorized: an unrevealed target plot, an out-of-range zoom, and a non-human
view mode are each rejected with ``out_of_parity_camera`` and recorded (never
``unavailable_to_human_now``, the generic reason ``act.dispatch.dispatch_action`` would produce for
the same predicate failure) -- the whole point of ``act.camera`` existing as a separate module.

T276 adds the check the predicate alone could not make. ``camera.zoom``'s declared predicate is
the *union* of two views' ranges, so a strategic-view zoom asked for in world mode satisfies it
while naming a camera state no view in the catalog declares. **Every bound in these tests is read
out of the loaded catalog, never written down here**: the point is that the check and the
declaration cannot drift apart, which a hard-coded ``(0.2, 1.0)`` would quietly allow.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.act.availability import CROSS_VIEW_ZOOM, CrossViewZoom, _sets_camera_zoom
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
from civsim_harness.models.catalog import DeclarationKind
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.decision import ExecutionOutcome, RejectionReason
from civsim_harness.models.turn import Observation, ObservationEntry

_REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGS_ROOT = _REPO_ROOT / "catalogs"

WORLD_VIEW = DeclarationId("views.world")
STRATEGIC_VIEW = DeclarationId("views.strategic")
CAMERA_STATE = DeclarationId("camera.read_state")


def _board(camera_state: Mapping[str, Any] | None) -> Observation:
    """A board reporting *camera_state* as its ``camera.read_state`` entry, or reporting none."""
    entries = (
        []
        if camera_state is None
        else [
            ObservationEntry(
                declaration_id=CAMERA_STATE,
                key=str(CAMERA_STATE),
                value=dict(camera_state),
                context=LuaContext.IN_GAME,
            )
        ]
    )
    return Observation(
        observation_id=ObservationId("obs-camera"),
        decision_step_id=DecisionStepId("step-camera"),
        assembled_at=datetime(2026, 9, 22, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="2026.09.3", content_hash="deadbeef"),
        entries=entries,
        screen_identity="world",
    )


def _in_view(catalog: Catalog, view: DeclarationId) -> Observation:
    """A board whose camera sits in *view*'s declared mode, at the midpoint of its declared range.

    Both the mode and the zoom are read from the view's own ``camera_requirements`` -- this helper
    never states a number of its own.
    """
    requirements = catalog.declarations[view].camera_requirements
    assert requirements is not None
    low, high = requirements.zoom_range
    return _board(
        {"mode": requirements.mode.value, "zoom": (low + high) / 2, "target_is_revealed": True}
    )


def _zoom_range(catalog: Catalog, view: DeclarationId) -> tuple[float, float]:
    requirements = catalog.declarations[view].camera_requirements
    assert requirements is not None
    return requirements.zoom_range


#: The real, checked-in ``catalogs/`` tree -- camera.py binds to the authored
#: ``catalogs/actions/camera.yaml``, not a fixture stand-in (task instructions).
_CATALOG = load_catalog(CATALOGS_ROOT)

#: The board the pre-T276 tests below run against: the camera sitting in the world view, which is
#: where a run starts (``run/composition.py``'s own ``DEFAULT_VIEW_DECLARATION_ID``). Built from
#: the view's own declaration, so it is the world view by the catalog's definition of one.
WORLD_BOARD = _in_view(_CATALOG, WORLD_VIEW)


@pytest.fixture(scope="module")
def real_catalog() -> Catalog:
    return _CATALOG


@pytest.fixture
def registry(real_catalog: Catalog) -> CapabilityRegistry:
    return CapabilityRegistry(catalog=real_catalog)


def _validated(
    *,
    registry: CapabilityRegistry,
    action_declaration_id: DeclarationId,
    target: Any,
    observation: Observation = WORLD_BOARD,
    cross_view: CrossViewZoom = CROSS_VIEW_ZOOM,
) -> DispatchOutcome:
    """:func:`validate_camera_action` with the camera in the world view unless a test says
    otherwise -- the context the checks below were always implicitly written against."""
    return validate_camera_action(
        registry=registry,
        action_declaration_id=action_declaration_id,
        target=target,
        observation=observation,
        cross_view=cross_view,
    )


# --------------------------------------------------------------------------
# camera.move -- target must be a revealed plot
# --------------------------------------------------------------------------


def test_unrevealed_target_plot_is_rejected_out_of_parity(registry: CapabilityRegistry) -> None:
    outcome = _validated(
        registry=registry,
        action_declaration_id=CAMERA_MOVE,
        target={"x": 4, "y": 7, "is_revealed": False},
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA


def test_revealed_target_plot_is_authorized(registry: CapabilityRegistry) -> None:
    outcome = _validated(
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
    outcome = _validated(
        registry=registry, action_declaration_id=CAMERA_ZOOM, target=5.0
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA


def test_negative_zoom_is_rejected_out_of_parity(registry: CapabilityRegistry) -> None:
    outcome = _validated(
        registry=registry, action_declaration_id=CAMERA_ZOOM, target=-0.1
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA


def test_in_range_zoom_is_authorized(registry: CapabilityRegistry) -> None:
    outcome = _validated(
        registry=registry, action_declaration_id=CAMERA_ZOOM, target=0.5
    )

    assert outcome.status is DispatchStatus.authorized


# --------------------------------------------------------------------------
# camera.zoom -- and within the range the view the camera is IN declares (T276)
# --------------------------------------------------------------------------


def _strategic_only_zoom(catalog: Catalog) -> float:
    """A zoom the strategic view declares and the world view does not.

    Read from the two declarations, never written down: the premise of the whole check is that
    ``camera.zoom``'s predicate is the union of these two ranges, so if the catalog is ever
    authored such that no such zoom exists, this asserts rather than passing vacuously.
    """
    world_low, world_high = _zoom_range(catalog, WORLD_VIEW)
    strategic_low, strategic_high = _zoom_range(catalog, STRATEGIC_VIEW)
    assert strategic_low < world_low or strategic_high > world_high, (
        "the strategic view no longer declares a zoom the world view does not; "
        "re-derive this scenario from the catalog rather than deleting the check"
    )
    return strategic_low if strategic_low < world_low else strategic_high


def test_a_strategic_zoom_asked_for_in_world_mode_is_refused_naming_the_view_and_range(
    registry: CapabilityRegistry, real_catalog: Catalog
) -> None:
    """The live failure of 2026-09-21: `camera.zoom` was issued with 0.05 while the camera was in
    world mode. It satisfies the declared predicate -- which is the union of both views' ranges --
    and names a camera state no view declares and no human occupies. Refused here, before
    dispatch, rather than discovered afterwards when the captures fail the provenance gate."""
    zoom = _strategic_only_zoom(real_catalog)
    world_low, world_high = _zoom_range(real_catalog, WORLD_VIEW)

    outcome = _validated(registry=registry, action_declaration_id=CAMERA_ZOOM, target=zoom)

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA
    reason = outcome.detail["reason"]
    # The refusal names the view it violated and that view's own declared range, both read back
    # out of the catalog here -- a refusal that says only "out of parity" is not actionable.
    assert str(WORLD_VIEW) in reason
    assert f"{world_low:g}" in reason and f"{world_high:g}" in reason
    assert str(STRATEGIC_VIEW) in reason


def test_the_same_zoom_is_authorized_in_the_view_that_declares_it(
    registry: CapabilityRegistry, real_catalog: Catalog
) -> None:
    """The other half of Principle I: the strategic view's own declared range must stay reachable.
    Clamping `camera.zoom` to the world range would have fixed the first failure by breaking this
    one."""
    zoom = _strategic_only_zoom(real_catalog)

    outcome = _validated(
        registry=registry,
        action_declaration_id=CAMERA_ZOOM,
        target=zoom,
        observation=_in_view(real_catalog, STRATEGIC_VIEW),
    )

    assert outcome.status is DispatchStatus.authorized


def test_a_zoom_the_current_view_declares_is_authorized(
    registry: CapabilityRegistry, real_catalog: Catalog
) -> None:
    low, high = _zoom_range(real_catalog, WORLD_VIEW)

    for zoom in (low, (low + high) / 2, high):
        outcome = _validated(registry=registry, action_declaration_id=CAMERA_ZOOM, target=zoom)
        assert outcome.status is DispatchStatus.authorized, zoom


def test_a_board_reporting_no_camera_mode_is_not_refused_on_an_invented_range(
    registry: CapabilityRegistry, real_catalog: Catalog
) -> None:
    """With no camera mode reported there is no current view to measure against, and this module
    never invents a restriction. The capture side still fails closed on an unreadable camera state
    (`parity/screening.py`'s provenance gate), which is where that belongs."""
    zoom = _strategic_only_zoom(real_catalog)

    for board in (_board(None), _board({"zoom": zoom, "target_is_revealed": True})):
        outcome = _validated(
            registry=registry,
            action_declaration_id=CAMERA_ZOOM,
            target=zoom,
            observation=board,
        )
        assert outcome.status is DispatchStatus.authorized


def test_the_cross_view_answer_is_held_rather_than_guessed(
    registry: CapabilityRegistry, real_catalog: Catalog
) -> None:
    """Whether a target legal for a *different* view should be refused or should perform the view
    change first depends on what a real scroll-wheel zoom does to mode and zoom together on this
    build -- a client-gated measurement (tasks.md T277). The seam is a parameter, and the
    unmeasured branch fails loudly instead of quietly re-admitting the argument."""
    zoom = _strategic_only_zoom(real_catalog)

    with pytest.raises(NotImplementedError, match="T277"):
        _validated(
            registry=registry,
            action_declaration_id=CAMERA_ZOOM,
            target=zoom,
            cross_view=CrossViewZoom.SWITCH_VIEW_FIRST,
        )


# --------------------------------------------------------------------------
# camera.set_view_mode -- mode must be one a human can toggle
# --------------------------------------------------------------------------


def test_non_human_view_mode_is_rejected_out_of_parity(registry: CapabilityRegistry) -> None:
    """``city_screen`` exists as a declared view (catalogs/observations/views.yaml) but is reached
    by its own screen-opening action, never this generic toggle."""
    outcome = _validated(
        registry=registry, action_declaration_id=CAMERA_SET_VIEW_MODE, target="city_screen"
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA


def test_nonexistent_view_mode_is_rejected_out_of_parity(registry: CapabilityRegistry) -> None:
    outcome = _validated(
        registry=registry, action_declaration_id=CAMERA_SET_VIEW_MODE, target="orbital"
    )

    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.OUT_OF_PARITY_CAMERA


@pytest.mark.parametrize("mode", ["world", "strategic"])
def test_human_togglable_view_mode_is_authorized(
    registry: CapabilityRegistry, mode: str
) -> None:
    outcome = _validated(
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
    outcome = _validated(
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
    outcome = _validated(
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
    outcome = _validated(
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
    outcome = _validated(
        registry=registry, action_declaration_id=CAMERA_ZOOM, target=0.5
    )

    assert isinstance(outcome, DispatchOutcome)


# --------------------------------------------------------------------------
# T321 (2026-09-22, headless lane): the coupling that nearly shipped a Principle I regression.
#
# `_sets_camera_zoom` decides whether the T276 per-view range guard applies, and it used to decide
# it by matching the EXACT text of a verification predicate: `camera.zoom == target`. Its own
# comment presented that as a virtue -- "which is how `_sets_camera_zoom` finds it without this
# module hard-coding the id `camera.zoom`". Changing `camera.zoom`'s predicate to a tolerance form
# (T321) removed that shape, and the guard stopped applying: a zoom of 0.05 in world mode -- a
# camera state no view declares and no human occupies -- went from `rejected` to `authorized`.
#
# It failed in the DANGEROUS direction (authorising, not refusing), and only these two pre-existing
# tests caught it. Naming what held: `test_a_strategic_zoom_asked_for_in_world_mode_is_refused...`
# and `test_the_cross_view_answer_is_held_rather_than_guessed` both went red immediately, on a
# change whose diff touched only a catalog predicate and no Python at all. They earned their keep.
#
# But those two tests only fail because the shipped catalog happens to have exactly one zoom action.
# The matcher's real defect is that a non-match reads as "not a zoom action" -- "nothing there" --
# which is this project's own named failure mode: an allowlist read as a detector produces a
# confident false all-clear. The remedy is the one already adopted elsewhere: make the unknown
# explicit. The matcher now keys on the SYMBOLS the declaration constrains (`camera.zoom` against
# `target`) rather than the shape of the comparison between them, so any future rewrite of the
# comparison keeps working; and the ratchet below asserts the guard still finds its action, so a
# silent zero can never be mistaken for "no zoom action exists".
# --------------------------------------------------------------------------


def test_exactly_one_shipped_action_is_recognised_as_setting_the_camera_zoom(
    real_catalog: Catalog,
) -> None:
    """THE RATCHET. The T276 view-range guard only runs on declarations `_sets_camera_zoom`
    recognises, and a declaration it fails to recognise is silently ungeared rather than loudly
    broken. Pin the recognition to the catalog: exactly one shipped action must be found, and it
    must be `camera.zoom`. Confirmed FAILING against the T321 catalog with the pre-T321 matcher
    (zero actions recognised, guard silently disabled, 0.05-in-world-mode authorised)."""
    recognised = {
        declaration.declaration_id
        for declaration in real_catalog.declarations.values()
        if declaration.kind is DeclarationKind.ACTION and _sets_camera_zoom(declaration)
    }

    assert recognised == {CAMERA_ZOOM}, (
        "the per-view zoom guard no longer recognises the action it exists to guard -- if "
        "camera.zoom's verification predicate changed shape, teach the matcher the symbols it "
        "constrains, never re-tighten it to one comparison's text"
    )


def test_the_zoom_guard_survives_a_predicate_rewrite_that_keeps_its_meaning(
    real_catalog: Catalog,
) -> None:
    """The matcher must key on WHAT the declaration constrains, not HOW it writes the constraint.
    Each rewrite below says the same thing -- this action puts `camera.zoom` at `target` -- and the
    guard must recognise every one of them. The exact-text matcher recognised only the first."""
    declaration = real_catalog.declarations[CAMERA_ZOOM]

    for predicate in (
        "camera.zoom == target\n",
        "camera.zoom - target <= 0.001 and target - camera.zoom <= 0.001\n",
        "target == camera.zoom\n",
        "target - camera.zoom <= 0.001 and camera.zoom - target <= 0.001\n",
    ):
        rewritten = declaration.model_copy(update={"verification_predicate": predicate})
        assert _sets_camera_zoom(rewritten) is True, predicate


def test_a_declaration_that_does_not_constrain_the_zoom_is_not_recognised(
    real_catalog: Catalog,
) -> None:
    """THE NEGATIVE CONTROL. Loosening the matcher must not make it match everything -- an action
    that never mentions `camera.zoom`, or mentions it without a `target`, is not the zoom action."""
    declaration = real_catalog.declarations[CAMERA_ZOOM]

    for predicate in (
        "camera.mode == target\n",
        "camera.target_plot == target and camera.target_is_revealed\n",
        "camera.zoom <= 1.0\n",  # constrains the zoom, but not against the requested target
        "game.turn_number == observed_turn_number + 1\n",
    ):
        rewritten = declaration.model_copy(update={"verification_predicate": predicate})
        assert _sets_camera_zoom(rewritten) is False, predicate
