"""`units.build_improvement`: the first action in this catalog that spends a builder charge.

The gap: `catalogs/actions/units.yaml` declared only select/move_to/found_city/promote, so the
directed goal `use_a_builder` (tests/live/goals/use_a_builder.yaml) was authored *blocked* -- "NO
action in catalogs/actions/ spends a builder charge" -- and the harness had never built an
improvement. These tests drive the real shipped catalog through the production predicate evaluator
(`act/predicates.py`), the production availability renderer (`act/availability.py`) and the
production verifier (`act/verify.py`). The Lua the action dispatches into, and the `units.state`
fields its predicate reads, are pinned separately in `test_unit_builds_lua.py`.

Nothing here constructs a store, a host adapter or a game.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.act.availability import evaluate_action_availability
from civsim_harness.act.predicates import (
    build_predicate_bindings,
    evaluate_predicate,
    resolve_selected_subject_target,
)
from civsim_harness.act.verify import observed_snapshot, verify_execution
from civsim_harness.agent.context import assemble_action_catalog_text
from civsim_harness.capability.loader import load_catalog
from civsim_harness.models.catalog import ParityDeclaration, TargetKind
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ObservationId,
    Timestamp,
)
from civsim_harness.models.decision import ExecutionOutcome
from civsim_harness.models.turn import Observation, ObservationEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"

BUILD_IMPROVEMENT = DeclarationId("units.build_improvement")
NOW: Timestamp = datetime(2026, 9, 21, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fake boards -- one selected unit, exactly as units.state reports it
# --------------------------------------------------------------------------


def _observation(entries: Sequence[tuple[str, Any]]) -> Observation:
    return Observation(
        observation_id=ObservationId("obs-1"),
        decision_step_id=DecisionStepId("step-1"),
        assembled_at=datetime(2026, 9, 21, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="2026.09.8", content_hash="deadbeef"),
        entries=[
            ObservationEntry(
                declaration_id=DeclarationId(key),
                key=key,
                value=value,
                context=LuaContext.IN_GAME,
            )
            for key, value in entries
        ],
        screen_identity="world",
    )


def _builder(**overrides: Any) -> dict[str, Any]:
    unit: dict[str, Any] = {
        "unit_id": 65540,
        "unit_type": "UNIT_BUILDER",
        "owner_player_id": 0,
        "owner_is_local_player": True,
        "is_selected": True,
        "plot": {"x": 10, "y": 12},
        "movement_remaining": 2,
        "max_movement": 2,
        "charges_remaining": 3,
        "available_builds": ["IMPROVEMENT_FARM", "IMPROVEMENT_MINE"],
        "build_options": [
            {
                "improvement_type": "IMPROVEMENT_FARM",
                "name": "Farm",
                "disabled": False,
                "is_recommended": True,
            },
            {
                "improvement_type": "IMPROVEMENT_MINE",
                "name": "Mine",
                "disabled": False,
                "is_recommended": False,
            },
        ],
    }
    unit.update(overrides)
    return unit


def _warrior() -> dict[str, Any]:
    """A non-builder as `units.state` reports it: no build row on its panel, so no build fields."""
    return {
        "unit_id": 65541,
        "unit_type": "UNIT_WARRIOR",
        "owner_player_id": 0,
        "owner_is_local_player": True,
        "is_selected": True,
        "plot": {"x": 11, "y": 12},
        "movement_remaining": 2,
        "charges_remaining": 0,
    }


def _board(unit: dict[str, Any]) -> Observation:
    return _observation([("units.state", {"units": [unit]})])


@pytest.fixture(scope="module")
def declaration() -> ParityDeclaration:
    return load_catalog(CATALOG_ROOT).declarations[BUILD_IMPROVEMENT]


def _holds(declaration: ParityDeclaration, observation: Observation, target: Any) -> bool:
    assert declaration.availability_predicate is not None
    bindings = build_predicate_bindings(observation=observation, target=target)
    return evaluate_predicate(declaration.availability_predicate, bindings)


# --------------------------------------------------------------------------
# The declaration itself
# --------------------------------------------------------------------------


def test_the_catalog_declares_a_charge_spending_action_backed_by_the_unit_orders_capability(
    declaration: ParityDeclaration,
) -> None:
    assert declaration.context is LuaContext.IN_GAME
    assert str(declaration.capability_id) == "units.orders"
    assert declaration.target_kind is TargetKind.NAME


def test_the_rendered_command_line_names_the_improvement_as_the_target(
    declaration: ParityDeclaration,
) -> None:
    """T256: the shape of the command is rendered on the command's own line -- the measured
    failure was a model that sent a unit id where the action wanted something else entirely."""
    line = assemble_action_catalog_text([declaration]).splitlines()[-1]
    assert "units.build_improvement" in line
    assert '{"target": "TECH_POTTERY"}' in line  # the NAME kind's fixed worked example
    assert "available_builds" in line
    assert "IMPROVEMENT_FARM" in line


# --------------------------------------------------------------------------
# The availability predicate
# --------------------------------------------------------------------------


def test_a_selected_builder_may_build_an_improvement_its_panel_is_offering(
    declaration: ParityDeclaration,
) -> None:
    assert _holds(declaration, _board(_builder()), "IMPROVEMENT_FARM") is True


def test_an_improvement_the_panel_is_not_offering_is_refused(
    declaration: ParityDeclaration,
) -> None:
    """Including one the panel lists greyed out -- `available_builds` is the live-button subset."""
    assert _holds(declaration, _board(_builder()), "IMPROVEMENT_PASTURE") is False


def test_a_builder_that_has_spent_every_charge_may_not_build(
    declaration: ParityDeclaration,
) -> None:
    board = _board(_builder(charges_remaining=0, available_builds=[], build_options=[]))
    assert _holds(declaration, board, "IMPROVEMENT_FARM") is False


def test_a_unit_that_is_not_a_builder_may_not_build(declaration: ParityDeclaration) -> None:
    assert _holds(declaration, _board(_warrior()), "IMPROVEMENT_FARM") is False


def test_an_unselected_builder_may_not_build(declaration: ParityDeclaration) -> None:
    assert _holds(declaration, _board(_builder(is_selected=False)), "IMPROVEMENT_FARM") is False


def test_the_action_is_offered_to_the_agent_when_a_builder_is_standing_on_a_buildable_tile(
    declaration: ParityDeclaration,
) -> None:
    """T262: what the agent is shown. `target in unit.available_builds` is decidable before a
    target is named -- an empty build row is a greyed-out button."""
    offered = evaluate_action_availability(declaration, _board(_builder()))
    assert offered.available is True

    empty = _builder(charges_remaining=2, available_builds=[], build_options=[])
    greyed = evaluate_action_availability(declaration, _board(empty))
    assert greyed.available is False
    assert "unit.available_builds" in greyed.reason


def test_with_no_unit_on_the_board_at_all_the_action_is_unavailable_never_unevaluable(
    declaration: ParityDeclaration,
) -> None:
    status = evaluate_action_availability(declaration, _observation([]))
    assert status.available is False
    assert status.reason


# --------------------------------------------------------------------------
# The binder: the improvement name is the target, so the subject is the selected unit
# --------------------------------------------------------------------------


def test_the_subject_is_the_selected_unit_even_though_target_names_an_improvement() -> None:
    """`target` is an improvement type, which matches no `unit_id`; README §4's parenthesis says
    the subject is then the unit the human selected -- the same rule units.promote relies on."""
    bindings = build_predicate_bindings(observation=_board(_builder()), target="IMPROVEMENT_FARM")
    assert bindings["unit"]["unit_id"] == 65540
    assert bindings["unit"]["available_builds"] == ["IMPROVEMENT_FARM", "IMPROVEMENT_MINE"]
    assert bindings["target"] == "IMPROVEMENT_FARM"


def test_an_action_issued_with_no_target_still_resolves_the_selected_builder() -> None:
    resolved = resolve_selected_subject_target(BUILD_IMPROVEMENT, _board(_builder()))
    assert resolved == 65540


# --------------------------------------------------------------------------
# Verification: the charge counter, before and after
# --------------------------------------------------------------------------


def test_the_charge_counter_is_snapshotted_before_the_order_goes_out() -> None:
    snapshot = observed_snapshot(_board(_builder()))
    assert snapshot["observed_charges_remaining"] == 3


def test_a_charge_spent_verifies_as_applied(declaration: ParityDeclaration) -> None:
    verification = verify_execution(
        declaration=declaration,
        pre_observation=_board(_builder(charges_remaining=3)),
        post_observation=_board(_builder(charges_remaining=2)),
        target="IMPROVEMENT_FARM",
        verified_at=NOW,
    )
    assert verification.execution.outcome is ExecutionOutcome.APPLIED


def test_a_swallowed_order_verifies_as_rejected(declaration: ParityDeclaration) -> None:
    verification = verify_execution(
        declaration=declaration,
        pre_observation=_board(_builder(charges_remaining=3)),
        post_observation=_board(_builder(charges_remaining=3)),
        target="IMPROVEMENT_FARM",
        verified_at=NOW,
    )
    assert verification.execution.outcome is ExecutionOutcome.REJECTED


def test_a_board_that_lost_the_unit_is_never_read_as_success(
    declaration: ParityDeclaration,
) -> None:
    """The documented limit, pinned: a Builder that spends its LAST charge leaves the map, and
    this predicate reads that as rejected rather than guessing. Conservative on purpose (FR-011);
    the plot's own `improvement` in map.state is the reading that covers it."""
    verification = verify_execution(
        declaration=declaration,
        pre_observation=_board(_builder(charges_remaining=1)),
        post_observation=_observation([("units.state", {"units": []})]),
        target="IMPROVEMENT_FARM",
        verified_at=NOW,
    )
    assert verification.execution.outcome is ExecutionOutcome.REJECTED
