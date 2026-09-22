"""Verification binds to the thing the order acted on, not to whatever is selected afterwards.

**A verified landing was scored a failure. The record was wrong, and this is the board it was
wrong on** -- replayed out of ``civsim-match-store.db``, turn cycle
``1b68484f1ed240c8864d1cf5094ebd31``, 2026-09-22 21:27Z:

====  ==========================  ======================================================
step  action                      what ``units.state`` said in that step's observation
====  ==========================  ======================================================
2     ``units.build_improvement`` Builder **458754 selected, charges 3**; Settler 393216
      ``target=IMPROVEMENT_MINE`` not selected, charges 0
--    the dispatch answered       ``{"ok": true, "unit_id": 458754,
                                  "plot": {"x": 44, "y": 30},
                                  "improvement": "IMPROVEMENT_MINE",
                                  "charges_remaining": 3}``
3     (the confirm's final read)  Builder 458754 **not selected, charges 2**;
                                  **Settler 393216 selected, charges 0**
====  ==========================  ======================================================

That step-3 observation was assembled at ``21:27:30.312548Z`` and the rejection was stamped
``verified_at 21:27:30.314361Z`` -- **1.8 ms later**. The harness had the right data in its own
final read and scored it ``rejected`` anyway, so no timeout would have helped.

**The cause is a binding, not a bound.** ``unit.charges_remaining == observed_charges_remaining
- 1`` and ``act/predicates.py``'s subject resolution binds ``unit`` to the entry the game shows
as selected whenever ``target`` names no unit -- and this action's target is an improvement name.
Civ VI auto-cycles selection off a unit after a completed order, so by the final read ``unit``
was the Settler: ``0 == 3 - 1`` -> False. ``catalogs/actions/units.yaml`` documented this for the
*last-charge* case only (the Builder is consumed); the real behaviour is far broader and fires on
an ordinary charge.

**The fix removes the dependency rather than tuning it:** the dispatch answer already names the
unit it acted on, so the predicate binds to *that* unit. ``units.move_to`` is unaffected and the
store proves it -- 46 of 46 of its recorded answers carry **no** ``unit_id`` key at all (its Lua
sets ``unit_id = unitId``, which the lone-plot normalisation leaves nil, and a Lua table
constructor drops a nil value), so nothing is named and the selection fallback stands exactly as
before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.act.predicates import acted_subject_ids, build_predicate_bindings
from civsim_harness.act.verify import observed_snapshot, verify_execution
from civsim_harness.capability.loader import load_catalog
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.decision import ExecutionOutcome
from civsim_harness.models.turn import Observation, ObservationEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"

BUILD_IMPROVEMENT = DeclarationId("units.build_improvement")

BUILDER = 458754
SETTLER = 393216
SCOUT = 327681

#: The Lua answer, verbatim from the store record.
DISPATCH_ANSWER: dict[str, Any] = {
    "ok": True,
    "unit_id": BUILDER,
    "plot": {"x": 44, "y": 30},
    "improvement": "IMPROVEMENT_MINE",
    "charges_remaining": 3,
}


def _unit(unit_id: int, *, selected: bool, charges: int, x: int, y: int) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "is_selected": selected,
        "charges_remaining": charges,
        "plot": {"x": x, "y": y},
        "owner_is_local_player": True,
        "available_builds": ["IMPROVEMENT_MINE"],
    }


def _observation(units: list[dict[str, Any]]) -> Observation:
    return Observation(
        observation_id=ObservationId("obs"),
        decision_step_id=DecisionStepId("step"),
        assembled_at=datetime(2026, 9, 22, 21, 27, 30, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="2026.09.21", content_hash="deadbeef"),
        entries=[
            ObservationEntry(
                declaration_id=DeclarationId("units.state"),
                key="units.state",
                value={"units": units},
                context=LuaContext.IN_GAME,
            )
        ],
        screen_identity="world_view",
    )


#: Step 2's own observation, as recorded: the Builder is selected with three charges.
def _pre() -> Observation:
    return _observation(
        [
            _unit(SETTLER, selected=False, charges=0, x=40, y=29),
            _unit(SCOUT, selected=False, charges=0, x=43, y=34),
            _unit(BUILDER, selected=True, charges=3, x=44, y=30),
        ]
    )


#: Step 3's own observation, as recorded: the mine is built, the charge is spent, and the game
#: has cycled the selection onto the Settler.
def _post_selection_moved() -> Observation:
    return _observation(
        [
            _unit(SETTLER, selected=True, charges=0, x=40, y=29),
            _unit(SCOUT, selected=False, charges=0, x=43, y=34),
            _unit(BUILDER, selected=False, charges=2, x=44, y=30),
        ]
    )


#: THE POSITIVE CONTROL. Same landing, same charge spent -- the game simply left the Builder
#: selected. Under today's code this one already verifies, which is what makes the pair a
#: measurement of the *selection* dimension rather than of the fix in general.
def _post_selection_stayed() -> Observation:
    return _observation(
        [
            _unit(SETTLER, selected=False, charges=0, x=40, y=29),
            _unit(SCOUT, selected=False, charges=0, x=43, y=34),
            _unit(BUILDER, selected=True, charges=2, x=44, y=30),
        ]
    )


@pytest.fixture(scope="module")
def declaration() -> Any:
    return load_catalog(CATALOG_ROOT).declarations[BUILD_IMPROVEMENT]


def _verify(declaration: Any, post: Observation, *, answer: dict[str, Any] | None) -> Any:
    ids = acted_subject_ids(BUILD_IMPROVEMENT, answer) if answer is not None else {}
    return verify_execution(
        declaration=declaration,
        pre_observation=_pre(),
        post_observation=post,
        target="IMPROVEMENT_MINE",
        verified_at=datetime(2026, 9, 22, 21, 27, 30, 314361, tzinfo=UTC),
        acted_subject_ids=ids,
    )


# --------------------------------------------------------------------------
# The recorded board
# --------------------------------------------------------------------------


def test_the_mine_that_was_built_is_now_recorded_as_applied(declaration: Any) -> None:
    """The exact record that was scored `rejected`, replayed."""
    result = _verify(declaration, _post_selection_moved(), answer=DISPATCH_ANSWER)
    assert result.execution.outcome is ExecutionOutcome.APPLIED


def test_the_same_board_without_the_dispatch_answer_still_reads_the_selection(
    declaration: Any,
) -> None:
    """The negative half: with nothing naming the acted-on unit, the old binding stands. This is
    what proves the fix is the *binding* and not some other change to the predicate."""
    result = _verify(declaration, _post_selection_moved(), answer=None)
    assert result.execution.outcome is ExecutionOutcome.REJECTED


def test_the_positive_control_a_board_where_selection_never_moved(declaration: Any) -> None:
    """Varies only the dimension the fix constrains -- which unit is selected afterwards. Applied
    both ways, so the fix cannot be 'it now says applied regardless'."""
    for answer in (DISPATCH_ANSWER, None):
        result = _verify(declaration, _post_selection_stayed(), answer=answer)
        assert result.execution.outcome is ExecutionOutcome.APPLIED


def test_a_charge_that_did_not_move_is_still_rejected(declaration: Any) -> None:
    """The fix must not turn a genuine no-op into an apply. Same acted-on unit, charge unchanged."""
    unchanged = _observation(
        [
            _unit(SETTLER, selected=True, charges=0, x=40, y=29),
            _unit(BUILDER, selected=False, charges=3, x=44, y=30),
        ]
    )
    result = _verify(declaration, unchanged, answer=DISPATCH_ANSWER)
    assert result.execution.outcome is ExecutionOutcome.REJECTED


def test_a_builder_consumed_by_its_last_charge_is_not_fabricated(declaration: Any) -> None:
    """The documented last-charge case: the unit leaves the map, so the predicate is unevaluable
    and the step stays rejected -- the conservative direction, unchanged by this fix."""
    gone = _observation([_unit(SETTLER, selected=True, charges=0, x=40, y=29)])
    result = _verify(declaration, gone, answer=DISPATCH_ANSWER)
    assert result.execution.outcome is ExecutionOutcome.REJECTED


# --------------------------------------------------------------------------
# What the dispatch answer is read for, and what it is not
# --------------------------------------------------------------------------


def test_the_answer_names_the_unit_it_acted_on() -> None:
    assert acted_subject_ids(BUILD_IMPROVEMENT, DISPATCH_ANSWER) == {"unit": BUILDER}


def test_a_move_answer_names_nothing_so_nothing_changes() -> None:
    """MEASURED from the store (2026-09-22T23:27Z): 46 of 46 `units.move_to` dispatch answers carry
    no `unit_id` key at all."""
    assert (
        acted_subject_ids(
            DeclarationId("units.move_to"), {"ok": True, "requested_plot": {"x": 1, "y": 2}}
        )
        == {}
    )


def test_a_city_order_answer_names_the_city_it_acted_on() -> None:
    assert acted_subject_ids(
        DeclarationId("cities.set_production"), {"ok": True, "city_id": 65536}
    ) == {"city": 65536}


def test_an_answer_that_is_not_a_mapping_names_nothing() -> None:
    for answer in (None, "ok", 3, ["unit_id", 1]):
        assert acted_subject_ids(BUILD_IMPROVEMENT, answer) == {}


def test_a_domain_with_no_selected_subject_is_never_rebound() -> None:
    """Only the two namespaces the game itself carries a selection for are resolved this way --
    the same table that decides the selection fallback being replaced."""
    assert (
        acted_subject_ids(
            DeclarationId("espionage.assign_mission"), {"ok": True, "spy_unit_id": 88}
        )
        == {}
    )
    assert (
        acted_subject_ids(DeclarationId("research.set_tech"), {"ok": True, "tech": "TECH_POTTERY"})
        == {}
    )


# --------------------------------------------------------------------------
# The pre-execution snapshot binds to the same unit
# --------------------------------------------------------------------------


def test_the_observed_snapshot_is_taken_from_the_acted_on_unit() -> None:
    """`observed_charges_remaining` used to come from whatever was selected *before* the order,
    which is the same guess at the other end of the comparison."""
    snapshot = observed_snapshot(_pre(), acted_subject_ids={"unit": BUILDER})
    assert snapshot["observed_charges_remaining"] == 3

    other = observed_snapshot(_pre(), acted_subject_ids={"unit": SETTLER})
    assert other["observed_charges_remaining"] == 0


def test_bindings_prefer_the_acted_on_subject_over_both_target_and_selection() -> None:
    bindings = build_predicate_bindings(
        observation=_post_selection_moved(),
        target="IMPROVEMENT_MINE",
        acted_subject_ids={"unit": BUILDER},
    )
    assert bindings["unit"]["unit_id"] == BUILDER
    assert bindings["unit"]["charges_remaining"] == 2


def test_an_acted_on_id_that_is_no_longer_on_the_board_binds_nothing(declaration: Any) -> None:
    """Never a silent fall-through to the selection -- that would reintroduce the defect for
    exactly the case (the unit left the map) the old comment was written about."""
    bindings = build_predicate_bindings(
        observation=_post_selection_moved(),
        target="IMPROVEMENT_MINE",
        acted_subject_ids={"unit": 999999},
    )
    assert bindings["unit"] == {"exists": False}
