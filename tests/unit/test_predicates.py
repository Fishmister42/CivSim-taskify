"""Unit tests for the restricted predicate evaluator and its bindings builder (T106).

Covers :func:`~civsim_harness.act.predicates.evaluate_predicate` directly against hand-built
bindings (the documented grammar: literals, ``.`` access, boolean/comparison operators, list
literals, membership, binary ``+``/``-`` between numeric operands -- and what it must reject:
function calls, multiplication/division, arithmetic on non-numeric operands, unknown names), and
:func:`~civsim_harness.act.predicates.build_predicate_bindings` against small
:class:`~civsim_harness.models.turn.Observation` fixtures (namespace merges, field renames, and
subject-namespace resolution against ``target``).

The final test class binds the evaluator to the *actual* catalog under ``catalogs/``:
``turn.end_turn``'s own ``verification_predicate``
(``game.turn_number == observed_turn_number + 1 or game.is_waiting_for_other_players``) uses binary
``+``, which ``catalogs/README.md`` §4 and contracts/capability-catalog.md now both document as
permitted (numeric-only arithmetic, no function calls, no multiplication/division). These tests
assert the real predicate, loaded from the real catalog, evaluates correctly both when the turn
advanced and when it did not -- so a future regression in either the catalog or the evaluator's
grammar is caught here rather than only in prose.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.act.predicates import (
    PredicateEvaluationError,
    build_predicate_bindings,
    evaluate_predicate,
)
from civsim_harness.capability.loader import load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.turn import Observation, ObservationEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"

# --------------------------------------------------------------------------
# evaluate_predicate -- literals, operators, grammar restriction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("predicate", "bindings", "expected"),
    [
        ("true", {}, True),
        ("false", {}, False),
        ("not false", {}, True),
        ("1 == 1", {}, True),
        ("1 != 2", {}, True),
        ("2 < 3 and 3 <= 3", {}, True),
        ("3 > 2 and 2 >= 2", {}, True),
        ("-1 < 0", {}, True),
        ("'a' in ['a', 'b']", {}, True),
        ("'c' in ['a', 'b']", {}, False),
        ("null == null", {}, True),
        ("game.gold == 10 and game.turn == 1", {"game": {"gold": 10, "turn": 1}}, True),
        ("game.gold == 10 and game.turn == 2", {"game": {"gold": 10, "turn": 1}}, False),
        ("game.gold == 10 or game.turn == 2", {"game": {"gold": 5, "turn": 2}}, True),
        ("1 + 1 == 2", {}, True),
        ("5 - 3 == 2", {}, True),
        (
            "game.turn_number == observed_turn_number + 1",
            {"game": {"turn_number": 6}, "observed_turn_number": 5},
            True,
        ),
        (
            "game.turn_number == observed_turn_number + 1",
            {"game": {"turn_number": 5}, "observed_turn_number": 5},
            False,
        ),
        ("unit.movement_remaining - 1 > 0", {"unit": {"movement_remaining": 2}}, True),
        ("unit.movement_remaining - 1 > 0", {"unit": {"movement_remaining": 1}}, False),
    ],
)
def test_evaluate_predicate_basic_grammar(
    predicate: str, bindings: dict[str, Any], expected: bool
) -> None:
    assert evaluate_predicate(predicate, bindings) is expected


def test_evaluate_predicate_and_short_circuits_without_touching_second_operand() -> None:
    # unit.exists is False, so unit.movement_remaining (absent) must never be dereferenced in a
    # way that raises -- this is the exact guard pattern every catalog predicate relies on.
    bindings = {"unit": {"exists": False}}
    assert evaluate_predicate("unit.exists and unit.movement_remaining > 0", bindings) is False


def test_evaluate_predicate_attribute_access_dotted_chain() -> None:
    bindings = {"unit": {"plot": {"x": 3, "y": 4}}}
    assert evaluate_predicate("unit.plot == target", {**bindings, "target": {"x": 3, "y": 4}})
    assert not evaluate_predicate("unit.plot == target", {**bindings, "target": {"x": 0, "y": 0}})


def test_evaluate_predicate_target_in_reachable_plots() -> None:
    bindings = {
        "unit": {"reachable_plots": [{"x": 1, "y": 1}, {"x": 2, "y": 2}]},
        "target": {"x": 2, "y": 2},
    }
    assert evaluate_predicate("target in unit.reachable_plots", bindings) is True


def test_evaluate_predicate_absent_attribute_resolves_to_none_not_a_crash() -> None:
    # A namespace bound to {"exists": False} (no match found) must never crash on an arbitrary
    # attribute read -- every non-"exists" attribute resolves to None (falsy).
    bindings = {"spy": {"exists": False}}
    assert evaluate_predicate("spy.exists and spy.is_available", bindings) is False
    assert evaluate_predicate("not spy.exists", bindings) is True


@pytest.mark.parametrize(
    "predicate",
    [
        "opponent.secret_hand_visible",
        "game.frobnicate()",
        "2 * 3 == 6",
        "6 / 2 == 3",
        "2 ** 3 == 8",
        "[x for x in [1, 2, 3]]",
        "lambda: True",
        "unit.plot['x'] == 1",
        "f'{unit.plot}'",
    ],
)
def test_evaluate_predicate_rejects_disallowed_constructs(predicate: str) -> None:
    with pytest.raises(PredicateEvaluationError):
        evaluate_predicate(predicate, {"game": {}, "unit": {"plot": {"x": 1}}})


@pytest.mark.parametrize(
    "predicate",
    [
        "'a' + 'b' == 'ab'",
        "unit.plot + 1 == 1",
        "true + 1 == 2",
        "unit.movement_remaining - 1 > 0",
    ],
)
def test_evaluate_predicate_rejects_arithmetic_on_non_numeric_operands(predicate: str) -> None:
    # +/- is permitted (catalogs/README.md §4), but only between numeric operands. A string, a
    # mapping (unit.plot), a boolean literal, or an absent field (unit.movement_remaining, not
    # supplied in bindings here) must all still raise, never silently coerce.
    with pytest.raises(PredicateEvaluationError):
        evaluate_predicate(predicate, {"game": {}, "unit": {"plot": {"x": 1}}})


def test_evaluate_predicate_rejects_name_with_no_binding() -> None:
    with pytest.raises(PredicateEvaluationError):
        evaluate_predicate("player.gold > 0", {})


def test_evaluate_predicate_incompatible_comparison_raises_not_crashes() -> None:
    # None (an absent field) compared with < against a number must raise our own typed error, not
    # an opaque Python TypeError.
    with pytest.raises(PredicateEvaluationError):
        evaluate_predicate("unit.movement_remaining > 0", {"unit": {"movement_remaining": None}})


def test_evaluate_predicate_never_returns_true_for_unevaluable_predicate() -> None:
    # FR-011's spirit applied to evaluation itself: an unevaluable predicate is a raise, never a
    # silently-successful True.
    with pytest.raises(PredicateEvaluationError):
        assert evaluate_predicate("congress.frobnicate()", {"congress": {}}) is not True


# --------------------------------------------------------------------------
# build_predicate_bindings -- realising the fixed symbol table from an Observation
# --------------------------------------------------------------------------


def _entry(
    declaration_id: str, value: Any, context: LuaContext = LuaContext.GAME_CORE_TUNER
) -> ObservationEntry:
    return ObservationEntry(
        declaration_id=declaration_id, key=declaration_id, value=value, context=context
    )


def _observation(entries: list[ObservationEntry], *, screen_identity: str = "world") -> Observation:
    return Observation(
        observation_id=ObservationId("obs-1"),
        decision_step_id=DecisionStepId("step-1"),
        assembled_at=datetime(2026, 1, 1),
        catalog_version=CatalogVersionRef(version="2026.09.1", content_hash="deadbeef"),
        entries=entries,
        captures=[],
        screen_identity=screen_identity,
    )


def test_build_bindings_merges_game_namespace_with_screen_rename() -> None:
    observation = _observation(
        [
            _entry(
                "game.screen_state",
                {
                    "screen": "world",
                    "raw_screen_id": "world",
                    "recognized": True,
                    "has_blocking_prompt": False,
                },
                context=LuaContext.IN_GAME,
            ),
            _entry(
                "game.turn_state",
                {
                    "turn_number": 5,
                    "is_local_player_turn": True,
                    "is_waiting_for_other_players": False,
                },
                context=LuaContext.IN_GAME,
            ),
        ]
    )
    bindings = build_predicate_bindings(observation=observation)

    assert bindings["game"]["current_screen"] == "world"
    assert bindings["game"]["has_blocking_prompt"] is False
    assert bindings["game"]["turn_number"] == 5
    assert bindings["game"]["is_local_player_turn"] is True
    assert bindings["game"]["active_prompt_type"] is None
    assert bindings["camera"] == {}


def test_build_bindings_derives_active_prompt_type_for_a_blocking_prompt() -> None:
    observation = _observation(
        [
            _entry(
                "game.screen_state",
                {
                    "screen": "prompt.city_state_quest",
                    "raw_screen_id": "prompt.city_state_quest",
                    "recognized": True,
                    "has_blocking_prompt": True,
                    "prompt_options": ["accept", "decline"],
                },
                context=LuaContext.IN_GAME,
            ),
        ]
    )
    bindings = build_predicate_bindings(observation=observation, target="accept")

    assert bindings["game"]["active_prompt_type"] == "prompt.city_state_quest"
    assert bindings["prompt"]["type"] == "prompt.city_state_quest"
    assert bindings["prompt"]["is_active"] is True
    assert bindings["prompt"]["options"] == ["accept", "decline"]

    predicate = (
        "game.has_blocking_prompt and game.current_screen == 'prompt.city_state_quest' "
        "and target in prompt.options"
    )
    assert evaluate_predicate(predicate, bindings) is True


def test_build_bindings_resolves_matched_subject_namespace() -> None:
    observation = _observation(
        [
            _entry(
                "units.state",
                {
                    "units": [
                        {"unit_id": 1, "owner_is_local_player": True, "movement_remaining": 2.0},
                        {"unit_id": 2, "owner_is_local_player": True, "movement_remaining": 0.0},
                    ]
                },
            ),
        ]
    )
    bindings = build_predicate_bindings(observation=observation, target=1)

    assert bindings["unit"]["exists"] is True
    assert bindings["unit"]["movement_remaining"] == 2.0
    assert evaluate_predicate("unit.exists and unit.movement_remaining > 0", bindings) is True


def test_build_bindings_unmatched_subject_defaults_to_absent() -> None:
    observation = _observation([_entry("units.state", {"units": []})])
    bindings = build_predicate_bindings(observation=observation, target=999)

    assert bindings["unit"] == {"exists": False}
    assert evaluate_predicate("not unit.exists", bindings) is True
    assert evaluate_predicate("unit.exists and unit.movement_remaining > 0", bindings) is False


def test_build_bindings_congress_top_level_field_survives_no_match() -> None:
    observation = _observation(
        [
            _entry(
                "congress.state",
                {"is_in_session": True, "local_player_favor": 3, "active_resolutions": []},
            ),
        ]
    )
    bindings = build_predicate_bindings(observation=observation, target=42)

    assert bindings["congress"]["is_in_session"] is True
    assert bindings["congress"]["exists"] is False
    # local_player_favor is merged into player.diplomatic_favor, not congress.* itself.
    assert bindings["player"]["diplomatic_favor"] == 3


def test_build_bindings_target_binds_raw_value_and_falls_back_on_attribute_access() -> None:
    observation = _observation([])
    bindings = build_predicate_bindings(observation=observation, target=7)

    assert bindings["target"] == 7
    # target.is_revealed on a plain int falls back to None (never crashes).
    assert evaluate_predicate("target.is_revealed", bindings) is False


def test_build_bindings_observed_snapshot_is_merged_verbatim() -> None:
    observation = _observation([])
    bindings = build_predicate_bindings(
        observation=observation, observed_snapshot={"observed_turn_number": 4, "ignored": 1}
    )

    assert bindings["observed_turn_number"] == 4
    assert "ignored" not in bindings


# --------------------------------------------------------------------------
# Real-catalog binding: turn.end_turn's verification_predicate, loaded from the actual catalog,
# must evaluate correctly under the (now arithmetic-permitting) documented grammar. See module
# docstring. This is the regression guard for the harness's single most important action: if this
# predicate cannot evaluate, `turn.end_turn` can never be recorded as `applied` and the run loop
# can never verify a turn ended.
# --------------------------------------------------------------------------


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_real_turn_end_turn_verification_predicate_evaluates_true_when_turn_advanced() -> None:
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("turn.end_turn")
    assert declaration.verification_predicate is not None
    assert "+" in declaration.verification_predicate

    bindings = {
        "game": {"turn_number": 6, "is_waiting_for_other_players": False},
        "observed_turn_number": 5,
    }
    assert evaluate_predicate(declaration.verification_predicate, bindings) is True


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_real_turn_end_turn_verification_predicate_evaluates_false_when_turn_did_not_advance() -> (
    None
):
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("turn.end_turn")
    assert declaration.verification_predicate is not None

    # Turn number unchanged and the harness is not waiting on other players either -- the click
    # was swallowed; the predicate must resolve false, never raise and never default to true.
    bindings = {
        "game": {"turn_number": 5, "is_waiting_for_other_players": False},
        "observed_turn_number": 5,
    }
    assert evaluate_predicate(declaration.verification_predicate, bindings) is False


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_real_turn_end_turn_verification_predicate_true_via_waiting_for_other_players() -> None:
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("turn.end_turn")
    assert declaration.verification_predicate is not None

    # The `or game.is_waiting_for_other_players` branch: turn_number has not yet ticked over (the
    # other civilizations are still taking their turns), but the end-turn click was still accepted.
    bindings = {
        "game": {"turn_number": 5, "is_waiting_for_other_players": True},
        "observed_turn_number": 5,
    }
    assert evaluate_predicate(declaration.verification_predicate, bindings) is True


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_real_turn_end_turn_availability_predicate_evaluates_cleanly() -> None:
    # Unlike the verification predicate above, turn.end_turn's availability_predicate has no
    # arithmetic and evaluates fine under the documented grammar.
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("turn.end_turn")
    assert declaration.availability_predicate is not None

    available_bindings = {"game": {"is_local_player_turn": True, "has_blocking_prompt": False}}
    assert evaluate_predicate(declaration.availability_predicate, available_bindings) is True

    blocked_bindings = {"game": {"is_local_player_turn": True, "has_blocking_prompt": True}}
    assert evaluate_predicate(declaration.availability_predicate, blocked_bindings) is False


def test_selected_subject_stands_in_for_a_missing_target_on_unit_and_city_actions() -> None:
    """catalogs/README.md §4: a unit/city action issued without a `target` acts on the unit/city
    the human selected. MEASURED 2026-09-21: 56 model decisions on units.found_city carried no
    target and were refused before dispatch; the game had the settler selected the whole time."""
    from civsim_harness.act.predicates import resolve_selected_subject_target
    from civsim_harness.models.common import DeclarationId

    observation = _observation(
        [
            _entry(
                "units.state",
                {
                    "units": [
                        {"unit_id": 7, "owner_is_local_player": True, "is_selected": False},
                        {"unit_id": 65536, "owner_is_local_player": True, "is_selected": True},
                    ]
                },
            ),
            _entry("cities.state", {"cities": [{"city_id": 3, "is_selected": False}]}),
        ]
    )

    assert resolve_selected_subject_target(DeclarationId("units.found_city"), observation) == 65536
    # Nothing selected -> None, never a guess.
    assert (
        resolve_selected_subject_target(DeclarationId("cities.set_production"), observation) is None
    )
    # Not a unit/city action -> None.
    assert resolve_selected_subject_target(DeclarationId("turn.end_turn"), observation) is None
    assert resolve_selected_subject_target(DeclarationId("research.set_tech"), observation) is None

    bindings = build_predicate_bindings(observation=observation, target=65536)
    assert evaluate_predicate("unit.is_selected and unit.owner_is_local_player", bindings) is True


def test_a_target_that_names_no_entry_binds_the_selected_subject() -> None:
    """README §4's parenthesis, in the binder itself: for units.move_to the `target` is the
    destination plot, for units.promote it is a promotion -- neither is a unit id -- and the
    subject is the unit the game shows as selected. MEASURED 2026-09-21 (attempt 4): a move order
    was refused because the plot target matched no unit id and `unit` bound to nothing."""
    observation = _observation(
        [
            _entry(
                "units.state",
                {
                    "units": [
                        {"unit_id": 7, "owner_is_local_player": True, "is_selected": False,
                         "movement_remaining": 0, "reachable_plots": []},
                        {"unit_id": 131073, "owner_is_local_player": True, "is_selected": True,
                         "movement_remaining": 2, "reachable_plots": [{"x": 43, "y": 31}]},
                    ]
                },
            ),
        ]
    )
    bindings = build_predicate_bindings(observation=observation, target={"x": 43, "y": 31})

    assert bindings["unit"]["unit_id"] == 131073
    assert evaluate_predicate(
        "unit.is_selected and unit.owner_is_local_player and unit.movement_remaining > 0 "
        "and target in unit.reachable_plots",
        bindings,
    ) is True

    # An explicit id still wins over the selection.
    assert build_predicate_bindings(observation=observation, target=7)["unit"]["unit_id"] == 7
    # Nothing selected and no matching id -> absent, as before.
    none_selected = _observation(
        [_entry("units.state", {"units": [{"unit_id": 7, "is_selected": False}]})]
    )
    assert build_predicate_bindings(observation=none_selected, target={"x": 1, "y": 1})["unit"] == {
        "exists": False
    }
