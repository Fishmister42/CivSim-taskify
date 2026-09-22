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

from collections.abc import Mapping
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
    # `prompt.unit_promotion` and its two offered promotions: a shape the shipped catalog still
    # claims. This test used to be written against `prompt.city_state_quest`, retired 2026-09-21
    # (catalogs/README.md §6) because no such prompt exists in Civ VI.
    observation = _observation(
        [
            _entry(
                "game.screen_state",
                {
                    "screen": "prompt.unit_promotion",
                    "raw_screen_id": "prompt.unit_promotion",
                    "recognized": True,
                    "has_blocking_prompt": True,
                    "prompt_options": ["battlecry", "tortoise"],
                },
                context=LuaContext.IN_GAME,
            ),
        ]
    )
    bindings = build_predicate_bindings(observation=observation, target="battlecry")

    assert bindings["game"]["active_prompt_type"] == "prompt.unit_promotion"
    assert bindings["prompt"]["type"] == "prompt.unit_promotion"
    assert bindings["prompt"]["is_active"] is True
    assert bindings["prompt"]["options"] == ["battlecry", "tortoise"]

    predicate = (
        "game.has_blocking_prompt and game.current_screen == 'prompt.unit_promotion' "
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


# --------------------------------------------------------------------------
# T255 -- city.is_selected is produced by overlaying cities.selection onto cities.state
# --------------------------------------------------------------------------


def _city_observation(
    *, selection: dict[str, Any] | None, cities: list[dict[str, Any]]
) -> Observation:
    entries = [_entry("cities.state", {"cities": cities})]
    if selection is not None:
        entries.append(_entry("cities.selection", selection))
    return _observation(entries)


_CITY_ORDER_PREDICATE = (
    "city.is_selected and city.owner_is_local_player and target in city.available_productions"
)


def test_the_selected_city_comes_from_the_ingame_selection_declaration() -> None:
    """MEASURED 2026-09-21: `cities.state` runs in GameCore_Tuner, where `UI` does not exist, so
    its entries never carried `is_selected` and every city order (`city.is_selected and ...`) was
    structurally unavailable. The InGame `cities.selection` declaration now names the selected
    city and the binder overlays it -- the entry's own fields stay exactly as cities.state read
    them."""
    from civsim_harness.act.predicates import resolve_selected_subject_target
    from civsim_harness.models.common import DeclarationId

    observation = _city_observation(
        selection={"has_selection": True, "selected_city_id": 3, "owner_is_local_player": True},
        cities=[
            {"city_id": 1, "owner_is_local_player": True, "available_productions": ["X"]},
            {
                "city_id": 3,
                "owner_is_local_player": True,
                "available_productions": ["BUILDING_MONUMENT"],
            },
        ],
    )

    assert resolve_selected_subject_target(DeclarationId("cities.set_production"), observation) == 3

    bindings = build_predicate_bindings(observation=observation, target="BUILDING_MONUMENT")
    assert bindings["city"]["city_id"] == 3
    assert bindings["city"]["is_selected"] is True
    assert evaluate_predicate(_CITY_ORDER_PREDICATE, bindings) is True

    by_id = build_predicate_bindings(observation=observation, target=1)
    assert by_id["city"]["city_id"] == 1
    assert by_id["city"]["is_selected"] is False


def test_no_selection_binds_no_city_and_refuses_rather_than_guessing() -> None:
    from civsim_harness.act.predicates import resolve_selected_subject_target
    from civsim_harness.models.common import DeclarationId

    for selection in (None, {"has_selection": False}, {"has_selection": False, "reason": "x"}):
        observation = _city_observation(
            selection=selection,
            cities=[{"city_id": 3, "owner_is_local_player": True, "available_productions": ["A"]}],
        )
        assert (
            resolve_selected_subject_target(DeclarationId("cities.set_production"), observation)
            is None
        )
        bindings = build_predicate_bindings(observation=observation, target="A")
        assert bindings["city"] == {"exists": False}
        assert evaluate_predicate(_CITY_ORDER_PREDICATE, bindings) is False


def test_an_entry_that_reports_its_own_selection_is_never_overridden_by_the_overlay() -> None:
    """`units.state` reports `is_selected` itself; a body that answered is authoritative, and the
    overlay applies only where the field is absent."""
    from civsim_harness.act.predicates import resolve_selected_subject_target
    from civsim_harness.models.common import DeclarationId

    observation = _city_observation(
        selection={"has_selection": True, "selected_city_id": 3},
        cities=[{"city_id": 3, "owner_is_local_player": True, "is_selected": False}],
    )
    assert (
        resolve_selected_subject_target(DeclarationId("cities.set_production"), observation) is None
    )
    bindings = build_predicate_bindings(observation=observation, target="A")
    assert bindings["city"] == {"exists": False}  # nothing selected -> nothing bound, no guess
    by_id = build_predicate_bindings(observation=observation, target=3)
    assert by_id["city"]["is_selected"] is False


# --------------------------------------------------------------------------
# T258 -- player.gold / player.faith and the per-turn rates come from player.yields
# --------------------------------------------------------------------------


def test_the_top_bar_gives_player_gold_and_faith_their_first_producer() -> None:
    """catalogs/README.md §4 has listed `player.gold` and `player.faith` since T106 with no
    observation producing them; `player.yields` (the top bar) now does, and the per-turn rates
    ride along under their own names."""
    observation = _observation(
        [
            _entry(
                "player.yields",
                {
                    "science_per_turn": 6.5,
                    "gold_per_turn": 2.4,
                    "gold_balance": 41,
                    "faith_balance": 12,
                },
                LuaContext.IN_GAME,
            )
        ]
    )
    bindings = build_predicate_bindings(observation=observation)
    assert bindings["player"]["gold"] == 41
    assert bindings["player"]["faith"] == 12
    assert bindings["player"]["science_per_turn"] == 6.5
    assert evaluate_predicate("player.gold >= 40 and player.gold_per_turn > 0", bindings) is True


# --------------------------------------------------------------------------
# 2026-09-21 -- `city.available_productions` is filled, so `cities.set_production` is reachable
# --------------------------------------------------------------------------

#: The real catalog predicate, verbatim from catalogs/actions/cities.yaml.
_SET_PRODUCTION_AVAILABILITY = (
    "city.is_selected and city.owner_is_local_player and target in city.available_productions"
)
_SET_PRODUCTION_VERIFICATION = "target in city.production_queue"

#: A `cities.state` entry of exactly the shape lua/gamecore/cities.lua now produces for an owned
#: city: the type names in `available_productions`, the panel's own rows (with their greyed-out
#: and placement-gated ones) in `production_options`.
_OWNED_CITY_WITH_PRODUCTIONS = {
    "city_id": 65538,
    "name": "Pasargadae",
    "owner_player_id": 0,
    "owner_is_local_player": True,
    "plot": {"x": 10, "y": 12},
    "population": 3,
    "available_productions": ["UNIT_BUILDER", "UNIT_WARRIOR", "BUILDING_MONUMENT"],
    "production_options": [
        {
            "type": "UNIT_BUILDER",
            "name": "Builder",
            "kind": "unit",
            "production_required": 50,
            "turns": 5,
            "disabled": False,
            "requires_placement": False,
        },
        {
            "type": "DISTRICT_CAMPUS",
            "name": "Campus",
            "kind": "district",
            "production_required": 54,
            "turns": 7,
            "disabled": False,
            "requires_placement": True,
        },
    ],
    "production_queue": [],
}


def test_a_filled_available_productions_makes_set_production_available() -> None:
    """MEASURED 2026-09-21 (399 steps, 30 runs): `available_productions` was `[]` in every
    observation on record, so this predicate's last conjunct could never hold and no city ever
    produced anything. With the list filled, the whole predicate holds for an item on it."""
    observation = _city_observation(
        selection={"has_selection": True, "selected_city_id": 65538},
        cities=[_OWNED_CITY_WITH_PRODUCTIONS],
    )

    bindings = build_predicate_bindings(observation=observation, target="UNIT_BUILDER")

    assert bindings["city"]["is_selected"] is True
    assert bindings["city"]["available_productions"] == [
        "UNIT_BUILDER",
        "UNIT_WARRIOR",
        "BUILDING_MONUMENT",
    ]
    assert evaluate_predicate(_SET_PRODUCTION_AVAILABILITY, bindings) is True


def test_an_empty_available_productions_is_exactly_the_bug_that_was_measured() -> None:
    """The regression guard: the predicate is false, and false for the *list*, not for the
    selection -- which is what made the empty list so hard to see in the ledger."""
    observation = _city_observation(
        selection={"has_selection": True, "selected_city_id": 65538},
        cities=[{**_OWNED_CITY_WITH_PRODUCTIONS, "available_productions": []}],
    )

    bindings = build_predicate_bindings(observation=observation, target="UNIT_BUILDER")

    assert bindings["city"]["is_selected"] is True
    assert bindings["city"]["owner_is_local_player"] is True
    assert evaluate_predicate(_SET_PRODUCTION_AVAILABILITY, bindings) is False


def test_an_item_the_panel_only_greys_out_is_not_bindable_as_a_target() -> None:
    """`production_options` lists the Campus a human can see; `available_productions` withholds it
    because the click that finishes it is a plot click the harness cannot make."""
    observation = _city_observation(
        selection={"has_selection": True, "selected_city_id": 65538},
        cities=[_OWNED_CITY_WITH_PRODUCTIONS],
    )

    bindings = build_predicate_bindings(observation=observation, target="DISTRICT_CAMPUS")

    assert evaluate_predicate(_SET_PRODUCTION_AVAILABILITY, bindings) is False


def test_set_production_binds_the_selected_city_while_target_names_the_item() -> None:
    """A city order's `target` is the production item, not a city id, so the subject comes from
    the selection overlay (catalogs/README.md §4's parenthesis). Both must hold at once for the
    action to dispatch at all."""
    from civsim_harness.act.predicates import resolve_selected_subject_target
    from civsim_harness.models.common import DeclarationId

    observation = _city_observation(
        selection={"has_selection": True, "selected_city_id": 65538},
        cities=[
            {"city_id": 7, "owner_is_local_player": True, "available_productions": []},
            _OWNED_CITY_WITH_PRODUCTIONS,
        ],
    )

    selected = resolve_selected_subject_target(
        DeclarationId("cities.set_production"), observation
    )
    assert selected == 65538
    bindings = build_predicate_bindings(observation=observation, target="UNIT_BUILDER")
    assert bindings["city"]["city_id"] == 65538
    assert evaluate_predicate(_SET_PRODUCTION_AVAILABILITY, bindings) is True


def test_the_verification_predicate_reads_the_queue_the_order_replaced() -> None:
    """`cities.set_production`'s verification is `target in city.production_queue`; the queue's
    head is what `BuildQueue:GetCurrentProductionTypeHash()` names, which is where an ordinary
    panel click puts the item (VALUE_REPLACE_AT, slot 0)."""
    before = _city_observation(
        selection={"has_selection": True, "selected_city_id": 65538},
        cities=[_OWNED_CITY_WITH_PRODUCTIONS],
    )
    after = _city_observation(
        selection={"has_selection": True, "selected_city_id": 65538},
        cities=[{**_OWNED_CITY_WITH_PRODUCTIONS, "production_queue": ["UNIT_BUILDER"]}],
    )

    assert (
        evaluate_predicate(
            _SET_PRODUCTION_VERIFICATION,
            build_predicate_bindings(observation=before, target="UNIT_BUILDER"),
        )
        is False
    )
    assert (
        evaluate_predicate(
            _SET_PRODUCTION_VERIFICATION,
            build_predicate_bindings(observation=after, target="UNIT_BUILDER"),
        )
        is True
    )


# --------------------------------------------------------------------------
# T308 -- `camera` namespace wiring. Before this fix, `build_predicate_bindings` hardcoded
# `"camera": {}` unconditionally and was never updated to source it from `camera.read_state`
# (added T221), even though that declaration reliably backs `mode`/`zoom`/`target_plot`/
# `target_is_revealed`. Consequence: `camera.move`/`camera.zoom`/`camera.set_view_mode`'s own
# `verification_predicate`s could never be True regardless of the real camera state -- 17+
# recorded refusals that were never evidence about the camera at all.
#
# `camera` is merged from `camera.read_state` the same way `game`/`player` are merged from their
# own sources (see `_CAMERA_SOURCES`, `predicates.py`) -- no rename table, since the schema's own
# field names (`mode`, `zoom`, `target_plot`, `target_is_revealed`) already match what the
# predicates reference.
# --------------------------------------------------------------------------


def _camera_observation(camera_state: dict[str, Any] | None) -> Observation:
    """An observation carrying a single `camera.read_state` entry, or none at all (`None`) --
    the shape a real capture takes when the Lua read failed entirely (T221's own header: every
    field read is independently pcall-guarded; an unreadable field comes back absent)."""
    entries = []
    if camera_state is not None:
        entries.append(_entry("camera.read_state", camera_state, context=LuaContext.IN_GAME))
    return _observation(entries)


def test_build_bindings_wires_camera_namespace_from_camera_read_state() -> None:
    """THE LOAD-BEARING FIX. Confirmed failing against the unfixed binder (pre-fix,
    `build_predicate_bindings` hardcoded `"camera": {}`, so every assertion below read `None`
    instead of the value actually captured, e.g. `bindings["camera"]["mode"]` was `None`, not
    `"world"`). `camera` must now be a real merge of `camera.read_state`'s own body, exactly like
    `game`/`player` are merges of their own backing declarations."""
    observation = _camera_observation(
        {
            "mode": "world",
            "zoom": 0.4,
            "target_is_revealed": True,
            "target_plot": {"x": 5, "y": 9},
        }
    )
    bindings = build_predicate_bindings(observation=observation)

    assert bindings["camera"]["mode"] == "world"
    assert bindings["camera"]["zoom"] == 0.4
    assert bindings["camera"]["target_is_revealed"] is True
    assert bindings["camera"]["target_plot"] == {"x": 5, "y": 9}
    # An observation with no camera.read_state entry at all still merges to `{}` -- the same
    # value the old hardcoded binding always produced, so the fix cannot regress the "no capture
    # yet" case, only add the "capture exists" one.
    assert build_predicate_bindings(observation=_camera_observation(None))["camera"] == {}


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
@pytest.mark.parametrize(
    ("declaration_id", "camera_state", "target", "expected"),
    [
        (
            "camera.zoom",
            {"mode": "world", "zoom": 0.5, "target_is_revealed": True},
            0.5,
            True,
        ),
        (
            "camera.zoom",
            {"mode": "world", "zoom": 0.5, "target_is_revealed": True},
            0.6,
            False,
        ),
        (
            "camera.set_view_mode",
            {"mode": "strategic", "zoom": 0.5, "target_is_revealed": True},
            "strategic",
            True,
        ),
        (
            "camera.set_view_mode",
            {"mode": "world", "zoom": 0.5, "target_is_revealed": True},
            "strategic",
            False,
        ),
        (
            "camera.move",
            {
                "mode": "world",
                "zoom": 0.5,
                "target_is_revealed": True,
                "target_plot": {"x": 3, "y": 4},
            },
            {"x": 3, "y": 4},
            True,
        ),
        (
            "camera.move",
            {
                "mode": "world",
                "zoom": 0.5,
                "target_is_revealed": False,
                "target_plot": {"x": 3, "y": 4},
            },
            {"x": 3, "y": 4},
            False,
        ),
    ],
)
def test_real_camera_verification_predicates_evaluate_correctly(
    declaration_id: str, camera_state: dict[str, Any], target: Any, expected: bool
) -> None:
    """The three camera actions' own `verification_predicate`, loaded from the real shipped
    catalog (not a hand-written stand-in), resolves and evaluates correctly against a real
    `camera.read_state` observation -- both when the camera genuinely matches the requested
    target and when it does not. This is exactly what T308 proved broken: before the fix, every
    one of these read `None` for every `camera.*` field and could never be `True`, so a camera
    action that actually worked was always recorded `rejected`."""
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve(declaration_id)
    assert declaration.verification_predicate is not None

    observation = _camera_observation(camera_state)
    bindings = build_predicate_bindings(observation=observation, target=target)

    assert evaluate_predicate(declaration.verification_predicate, bindings) is expected


# --------------------------------------------------------------------------
# Polarity (mandatory per T308's brief): all three camera verification predicates compare with
# `==` or read a boolean field for truthiness -- never `!=`/`not in`. An absent or explicitly
# `null` field must resolve the predicate `False` (an under-report the harness can live with),
# never `True` (a fabricated `applied` that "nothing in the data would ever reveal", per T310/
# T311). Each test checks the real catalog predicate against a plausible, in-range `target` (the
# only kind that reaches `verify_execution` in practice, since `act.dispatch` already rejects any
# decision whose `availability_predicate` fails -- e.g. `camera.zoom`'s own
# `target >= 0.05 and target <= 1.0` cannot pass with `target=None`), so this is the realistic
# "camera state missing, decision otherwise normal" shape, not a contrived double-absence.
# --------------------------------------------------------------------------


def _assert_never_confirms(predicate: str, bindings: dict[str, Any]) -> None:
    """*predicate* must not confirm against *bindings* -- and must not do so by any route.

    T314 widened what "not confirmed" can look like. These polarity tests were written (T308)
    when an absent field always produced a decided ``False``; a comparison against an absent
    operand is now *unevaluable* instead, which
    :func:`~civsim_harness.act.verify.verify_execution` maps to the same ``rejected`` and
    ``act.dispatch``/``act.availability`` map to the same "not available now". The invariant these
    tests exist to pin is unchanged and is stated directly here -- **never ``True``** -- rather
    than being pinned to the particular falsy route the evaluator happened to take in 2026-09,
    which is what a bare ``is False`` was really asserting.
    """
    try:
        result = evaluate_predicate(predicate, bindings)
    except PredicateEvaluationError:
        return
    assert result is False


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
@pytest.mark.parametrize(
    ("declaration_id", "target"),
    [
        ("camera.zoom", 0.5),
        ("camera.set_view_mode", "world"),
        ("camera.move", {"x": 3, "y": 4}),
    ],
)
def test_camera_verification_predicate_polarity_no_capture_is_false_not_true(
    declaration_id: str, target: Any
) -> None:
    """No `camera.read_state` entry at all (the Lua read failed, or none was ever captured) must
    resolve every camera verification predicate `False`, never `True`."""
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve(declaration_id)
    assert declaration.verification_predicate is not None

    observation = _camera_observation(None)
    bindings = build_predicate_bindings(observation=observation, target=target)
    assert bindings["camera"] == {}

    _assert_never_confirms(declaration.verification_predicate, bindings)


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_camera_zoom_polarity_explicit_null_zoom_is_false_not_true() -> None:
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("camera.zoom")
    assert declaration.verification_predicate is not None

    observation = _camera_observation({"mode": "world", "zoom": None, "target_is_revealed": True})
    bindings = build_predicate_bindings(observation=observation, target=0.5)
    assert bindings["camera"]["zoom"] is None

    _assert_never_confirms(declaration.verification_predicate, bindings)


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_camera_set_view_mode_polarity_explicit_null_mode_is_false_not_true() -> None:
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("camera.set_view_mode")
    assert declaration.verification_predicate is not None

    observation = _camera_observation({"mode": None, "zoom": 0.5, "target_is_revealed": True})
    bindings = build_predicate_bindings(observation=observation, target="world")
    assert bindings["camera"]["mode"] is None

    _assert_never_confirms(declaration.verification_predicate, bindings)


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_camera_move_polarity_explicit_null_target_plot_is_false_not_true() -> None:
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("camera.move")
    assert declaration.verification_predicate is not None

    observation = _camera_observation(
        {"mode": "world", "zoom": 0.5, "target_is_revealed": True, "target_plot": None}
    )
    bindings = build_predicate_bindings(observation=observation, target={"x": 1, "y": 2})
    assert bindings["camera"]["target_plot"] is None

    _assert_never_confirms(declaration.verification_predicate, bindings)


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_camera_move_polarity_absent_target_is_revealed_is_false_not_true() -> None:
    """`target_is_revealed` absent from the captured body entirely (not merely `null`) -- the
    boolean-truthiness half of `camera.move`'s predicate, distinct from its `==` half above."""
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("camera.move")
    assert declaration.verification_predicate is not None

    observation = _camera_observation({"mode": "world", "zoom": 0.5, "target_plot": {"x": 1, "y": 2}})
    bindings = build_predicate_bindings(observation=observation, target={"x": 1, "y": 2})
    assert bindings["camera"].get("target_is_revealed") is None

    _assert_never_confirms(declaration.verification_predicate, bindings)


# --------------------------------------------------------------------------
# T321 (2026-09-22, headless lane): `camera.zoom` verified `camera.zoom == target`, an EXACT float
# equality against a value the engine does not return. MEASURED from `decision_steps`, by replaying
# each rejected step against the observation the confirm poll actually read (the next step's
# recorded observation IS that final re-read -- its `assembled_at` is 1 ms before the step's
# `verified_at`, on all 118 camera steps):
#
#   requested 0.05 -> engine re-read 0.049999713897705   (error 2.861e-07)
#   requested 1.0  -> engine re-read 0.99999952316284    (error 4.768e-07, exactly 2^-21)
#
# 39 of 47 `verification_failed` zoom steps had the camera demonstrably ON the requested target and
# were recorded `rejected`. The remaining 8 are genuine: the camera never left 0.70710706710815.
#
# **Why the whole suite stayed green.** The parametrized
# `test_real_camera_verification_predicates_evaluate_correctly`
# above IS a positive control and it DOES pass -- because it compares `0.5` against a target of
# `0.5`, an exactly-equal float the engine never produces. A positive control that production
# cannot reach is not a positive control: it pins the success direction with the one input the real
# system cannot generate, so the defect stays invisible. The cases below are the same control fed
# the values the engine ACTUALLY returns.
#
# **Where the tolerance lives, and why there.** A float comparison needs a declared tolerance, and a
# tolerance is a number someone can quietly widen until anything passes. So it is NOT a constant in
# the evaluator: it is written into `catalogs/actions/camera.yaml`'s own predicate text as a pair of
# `-`/`<=` conjuncts the grammar already supports (catalogs/README.md §4 allows binary `+`/`-`
# between numerics). The evaluator is UNCHANGED by this task. Consequences that matter:
#   * widening the tolerance means editing the declaration, in the diff, where review sees it;
#   * no other predicate silently inherits a fuzzy `==`;
#   * `d74a4c7`'s design is preserved rather than worked around -- `_eval_binop` already raises
#     `PredicateEvaluationError` on a non-numeric operand, so an absent `camera.zoom` or an absent
#     `target` makes `camera.zoom - target` UNEVALUABLE, exactly as the `==` form became. The
#     polarity tests above still pass unchanged, which is the proof.
#
# **Magnitude, against the measurement rather than taste.** Largest observed error on a zoom that
# LANDED: 4.768e-07. Smallest observed error on a zoom that genuinely did NOT land: 0.293. Six
# orders of magnitude of separation, so 1e-3 is not a judgement call between close numbers -- it
# sits ~2000x above the observed engine error and ~293x below the smallest real miss.
# ASSUMPTION, stated because it is an extrapolation: every requested zoom in the store was an
# endpoint (0.05 or 1.0). The engine's error on a MID-RANGE request is not measured. 1e-3 is chosen
# with that margin in mind; if a mid-range zoom ever lands outside it the negative controls below
# are what will say so.
# --------------------------------------------------------------------------


#: The exact values the engine re-read for each requested zoom, lifted from `decision_steps`.
_MEASURED_ENGINE_ZOOM: Mapping[float, float] = {
    0.05: 0.049999713897705,
    1.0: 0.99999952316284,
}


def _real_camera_zoom_predicate() -> str:
    catalog = load_catalog(CATALOG_ROOT)
    declaration = CapabilityRegistry(catalog=catalog).resolve("camera.zoom")
    assert declaration.verification_predicate is not None
    return declaration.verification_predicate


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
@pytest.mark.parametrize(("target", "engine_zoom"), sorted(_MEASURED_ENGINE_ZOOM.items()))
def test_real_camera_zoom_confirms_the_value_the_engine_actually_returns(
    target: float, engine_zoom: float
) -> None:
    """THE POSITIVE CONTROL PRODUCTION CAN REACH. The camera went where it was told; the record
    must say `applied`. Confirmed FAILING against the unfixed catalog (`camera.zoom == target`
    returns False for both rows, which is precisely the 39 mis-recorded refusals)."""
    observation = _camera_observation(
        {"mode": "world", "zoom": engine_zoom, "target_is_revealed": True}
    )
    bindings = build_predicate_bindings(observation=observation, target=target)

    assert evaluate_predicate(_real_camera_zoom_predicate(), bindings) is True


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
@pytest.mark.parametrize(
    ("target", "engine_zoom", "why"),
    [
        # The real misses, measured: the camera never left its resting zoom.
        (0.05, 0.70710706710815, "camera stayed at its resting zoom; 8 such steps in the store"),
        (1.0, 0.70710706710815, "same, for a zoom-out request"),
        # THE RATCHET. Just outside 1e-3. If anyone widens the declared tolerance even to 2e-3,
        # this row goes red and names the thing they broke -- which is the whole point of putting
        # the number in the catalog rather than in a constant nobody diffs.
        (0.5, 0.5011, "1.1e-3 away: outside the declared tolerance, and must stay a refusal"),
        (0.5, 0.4989, "same magnitude on the low side"),
    ],
)
def test_real_camera_zoom_still_refuses_a_genuinely_wrong_zoom(
    target: float, engine_zoom: float, why: str
) -> None:
    """THE NEGATIVE CONTROL. A tolerance is only honest if something still fails. Every row here
    must stay `False` -- a camera that did not go where it was told is a real refusal and the
    record must keep saying so."""
    observation = _camera_observation(
        {"mode": "world", "zoom": engine_zoom, "target_is_revealed": True}
    )
    bindings = build_predicate_bindings(observation=observation, target=target)

    assert evaluate_predicate(_real_camera_zoom_predicate(), bindings) is False, why


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_camera_zoom_tolerance_is_declared_in_the_catalog_not_hidden_in_the_evaluator() -> None:
    """The tolerance must be visible in the declaration a reviewer reads. `==` alone cannot express
    one, so its absence from the predicate text is exactly the state this task found; and an
    evaluator-side epsilon would make every other `==` in the catalog silently fuzzy."""
    predicate = _real_camera_zoom_predicate()

    assert "==" not in predicate, (
        "camera.zoom must not verify by exact float equality -- that is the T321 defect"
    )
    assert "0.001" in predicate, "the tolerance must be a literal in the catalog declaration"


# --------------------------------------------------------------------------
# T313 (2026-09-22, live lane): `units.found_city`'s verification used to be `not unit.exists` --
# whether the Settler disappeared -- which fabricates, because a Settler also disappears when it
# is captured or killed, not only when it founds a city. `run-f9aea1fc1c7346eca0e72cf6d8492882`
# proved it live: `n_cities` walks 0 -> 1 (`LOC_CITY_NAME_PASARGADAE`, persisting across a turn
# boundary) while the confirm bound (a separate, out-of-scope concern) expired first and the
# founding step was recorded `rejected` -- but fixing the bound alone would leave a captured
# Settler indistinguishable from a founded city, since both make `unit.exists` false.
#
# The fix, verbatim from the owner: count cities. Verification now reads
# `player.city_count != null and player.city_count > observed_city_count` -- a real count from
# `cities.state`, compared before/after through the same `observed_*` mechanism
# `turn.end_turn`'s own `observed_turn_number + 1` already uses (catalogs/README.md §4), not a new
# one. These tests bind the REAL predicate text, loaded from the shipped catalog, exactly as the
# `turn.end_turn` tests above do -- so a future catalog or evaluator regression is caught here,
# not only in prose.
# --------------------------------------------------------------------------


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_real_found_city_verification_predicate_capture_case_is_rejected_not_applied() -> None:
    """THE CASE THAT MATTERS. The Settler vanished (`unit.exists` false, e.g. captured or killed
    by Georgia, MEASURED the same day on the live board) and no city appeared (`city_count`
    unchanged, 1 -> 1). The new predicate must reject this. Confirmed FAILING against the unfixed
    predicate below (`not unit.exists` reads the `unit.exists: False` also present in these
    bindings and returns `True` -- exactly the fabrication this replaces)."""
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("units.found_city")
    assert declaration.verification_predicate is not None
    assert declaration.verification_predicate.strip() == (
        "player.city_count != null and player.city_count > observed_city_count"
    )

    bindings = {
        "unit": {"exists": False},  # the settler is gone -- captured, not founding
        "player": {"city_count": 1},
        "observed_city_count": 1,  # unchanged: no city appeared
    }
    assert evaluate_predicate(declaration.verification_predicate, bindings) is False


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_real_found_city_verification_predicate_real_recorded_shape_0_to_1_is_applied() -> None:
    """The real recorded shape from `run-f9aea1fc1c7346eca0e72cf6d8492882`: `n_cities` 0 -> 1
    across consecutive observations. Must be `applied`."""
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("units.found_city")
    assert declaration.verification_predicate is not None

    bindings = {
        "unit": {"exists": False},
        "player": {"city_count": 1},
        "observed_city_count": 0,
    }
    assert evaluate_predicate(declaration.verification_predicate, bindings) is True


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_found_city_verification_predicate_polarity_absent_city_count_is_false_not_true() -> None:
    """Polarity: `cities.state` was not observed (or its `cities` field was missing/null), so
    `player.city_count` binds to `None` (never coerced to `0`). The predicate's leading
    `player.city_count != null` conjunct must short-circuit this straight to `False` -- never
    `True`, and never an unhandled `TypeError` from comparing `None > observed_city_count` (Python
    raises on that; the guard exists so this predicate never reaches it)."""
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("units.found_city")
    assert declaration.verification_predicate is not None

    bindings = {"player": {"city_count": None}, "observed_city_count": 0}
    assert evaluate_predicate(declaration.verification_predicate, bindings) is False


def test_build_bindings_computes_player_city_count_from_cities_state() -> None:
    """The binder half of T313: `player.city_count` counts only `cities.state` entries that are
    the local player's own -- another civilization's city currently in vision must never move it
    -- and is `None`, not `0`, when `cities.state` carries no usable `cities` list at all."""
    observation = _observation(
        [
            _entry(
                "cities.state",
                {
                    "cities": [
                        {"city_id": 1, "owner_is_local_player": True},
                        {"city_id": 2, "owner_is_local_player": True},
                        {"city_id": 3, "owner_is_local_player": False},  # a met civ's city, visible
                    ]
                },
            ),
        ]
    )
    bindings = build_predicate_bindings(observation=observation)
    assert bindings["player"]["city_count"] == 2

    empty_local_only = _observation([_entry("cities.state", {"cities": []})])
    assert build_predicate_bindings(observation=empty_local_only)["player"]["city_count"] == 0

    no_entry = _observation([])
    assert build_predicate_bindings(observation=no_entry)["player"]["city_count"] is None

    null_cities_field = _observation([_entry("cities.state", {"cities": None})])
    assert build_predicate_bindings(observation=null_cities_field)["player"]["city_count"] is None


# --------------------------------------------------------------------------
# T314 (2026-09-22, live lane): `==` against a NULL `target`. T310 enumerated the fabrication
# axis for the observation field (`X != <literal>`, `not X`, `not (target in X)`); T311 extended
# it to the LEFT operand of `in`/`not in`. Neither asked about `==` with a null `target`, where
# `None == None` is `True` -- so `research.set_civic`'s `player.current_civic == target` and
# `policies.change_government`'s `player.current_government == target` both recorded a no-op as
# `applied` (MEASURED through the real `verify_execution` before the fix:
# `outcome=applied progress=changed_state`). Both were reachable only because their availability
# predicates read collections that happen to be empty today -- an accident, not a control, and
# commit `0989e3b` (landed today) removes it for `researchable_civics`.
#
# The fix is central, in `_refuse_unresolved_operand`: a comparison either of whose operands
# resolved to `None` by ABSENCE is unevaluable, for every operator and every declaration --
# unless the predicate's own source writes the literal `null` there, which is an author asking
# about absence on purpose. The tests below vary **target presence/absence while holding the
# action identity and the observation content fixed**, which is the axis the defect lives on; a
# twin that varied the action would encode these fixtures rather than the contract.
# --------------------------------------------------------------------------

#: (declaration_id, backing observation declaration, its state field, a real value for it).
_EQUALITY_TARGET_ACTIONS = [
    ("research.set_civic", "research.state", "current_civic", "CIVIC_CODE_OF_LAWS"),
    ("policies.change_government", "government.state", "current_government", "GOVERNMENT_CHIEFDOM"),
]


def _equality_action_bindings(
    backing: str, field: str, field_value: Any, *, target: Any, present: bool = True
) -> dict[str, Any]:
    body: dict[str, Any] = {field: field_value} if present else {}
    observation = _observation([_entry(backing, body)])
    return build_predicate_bindings(observation=observation, target=target)


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
@pytest.mark.parametrize(("declaration_id", "backing", "field", "value"), _EQUALITY_TARGET_ACTIONS)
def test_t314_equality_verification_positive_control_still_confirms(
    declaration_id: str, backing: str, field: str, value: str
) -> None:
    """THE POSITIVE CONTROL. A correctly-supplied, non-null target that genuinely matches what the
    game reports must still confirm -- otherwise the action has been broken, not fixed."""
    catalog = load_catalog(CATALOG_ROOT)
    declaration = CapabilityRegistry(catalog=catalog).resolve(declaration_id)
    assert declaration.verification_predicate is not None

    bindings = _equality_action_bindings(backing, field, value, target=value)
    assert evaluate_predicate(declaration.verification_predicate, bindings) is True


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
@pytest.mark.parametrize(("declaration_id", "backing", "field", "value"), _EQUALITY_TARGET_ACTIONS)
def test_t314_equality_verification_null_target_never_confirms(
    declaration_id: str, backing: str, field: str, value: str
) -> None:
    """THE DEFECT. Identical action, identical observation content to the positive control above
    -- only the target's PRESENCE varies. A null target must never confirm. Before the fix this
    resolved `True` whenever the field was also absent, and `verify_execution` recorded
    `applied`."""
    catalog = load_catalog(CATALOG_ROOT)
    declaration = CapabilityRegistry(catalog=catalog).resolve(declaration_id)
    assert declaration.verification_predicate is not None

    # (a) the game reports a real value, the decision carried no target.
    _assert_never_confirms(
        declaration.verification_predicate,
        _equality_action_bindings(backing, field, value, target=None),
    )
    # (b) the field is present-but-null AND the decision carried no target -- `None == None`.
    _assert_never_confirms(
        declaration.verification_predicate,
        _equality_action_bindings(backing, field, None, target=None),
    )
    # (c) the field is ABSENT from the body entirely AND the decision carried no target.
    _assert_never_confirms(
        declaration.verification_predicate,
        _equality_action_bindings(backing, field, None, target=None, present=False),
    )


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
@pytest.mark.parametrize(("declaration_id", "backing", "field", "value"), _EQUALITY_TARGET_ACTIONS)
def test_t314_equality_verification_absent_field_with_real_target_never_confirms(
    declaration_id: str, backing: str, field: str, value: str
) -> None:
    """The other half of the same comparison: target PRESENT, observation field absent. This was
    already safe (`None == "CIVIC_..."` is `False`) and must stay that way -- the fix must not
    have turned an under-report into a confirmation."""
    catalog = load_catalog(CATALOG_ROOT)
    declaration = CapabilityRegistry(catalog=catalog).resolve(declaration_id)
    assert declaration.verification_predicate is not None

    _assert_never_confirms(
        declaration.verification_predicate,
        _equality_action_bindings(backing, field, None, target=value),
    )
    _assert_never_confirms(
        declaration.verification_predicate,
        _equality_action_bindings(backing, field, None, target=value, present=False),
    )
    # A real value that simply is not the one asked for stays a decided, readable `False`.
    bindings = _equality_action_bindings(backing, field, "SOMETHING_ELSE", target=value)
    assert evaluate_predicate(declaration.verification_predicate, bindings) is False


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
@pytest.mark.parametrize(("declaration_id", "backing", "field", "value"), _EQUALITY_TARGET_ACTIONS)
def test_t314_verify_execution_records_rejected_not_applied_for_a_null_target(
    declaration_id: str, backing: str, field: str, value: str
) -> None:
    """End to end through the real `verify_execution` -- the function that turns a predicate's
    verdict into the ledger row. Same action, same observations; only the target varies."""
    from civsim_harness.act.verify import verify_execution
    from civsim_harness.models.decision import ExecutionOutcome

    catalog = load_catalog(CATALOG_ROOT)
    declaration = CapabilityRegistry(catalog=catalog).resolve(declaration_id)
    observation = _observation([_entry(backing, {field: value})])

    null_target = verify_execution(
        declaration=declaration,
        pre_observation=observation,
        post_observation=observation,
        target=None,
        verified_at=datetime(2026, 9, 22),
    )
    assert null_target.execution.outcome is ExecutionOutcome.REJECTED

    real_target = verify_execution(
        declaration=declaration,
        pre_observation=observation,
        post_observation=observation,
        target=value,
        verified_at=datetime(2026, 9, 22),
    )
    assert real_target.execution.outcome is ExecutionOutcome.APPLIED


# -- the general mechanism, not just these two declarations -------------------------------------


@pytest.mark.parametrize(
    "predicate",
    [
        "player.current_civic == target",  # T314's own shape
        "player.current_civic != target",
        "target in player.available_policies",  # T311's left-operand axis
        "target not in player.available_policies",
        "not (target in player.available_policies)",  # the wrapped form a `False` would negate
    ],
)
def test_t314_null_target_is_unevaluable_for_every_comparison_operator(predicate: str) -> None:
    bindings = {
        "player": {"current_civic": "CIVIC_X", "available_policies": ["POLICY_X"]},
        "target": None,
    }
    with pytest.raises(PredicateEvaluationError):
        evaluate_predicate(predicate, bindings)


@pytest.mark.parametrize(
    "predicate",
    [
        'game.current_screen != "prompt.era_transition"',  # T310's field axis
        'game.current_screen == "prompt.era_transition"',
        "not (target in city.purchasable_with_gold)",
    ],
)
def test_t314_absent_observation_field_is_unevaluable_for_every_comparison_operator(
    predicate: str,
) -> None:
    bindings: dict[str, Any] = {"game": {}, "city": {}, "target": "SOMETHING"}
    with pytest.raises(PredicateEvaluationError):
        evaluate_predicate(predicate, bindings)


def test_t314_explicit_null_literal_stays_an_answerable_presence_test() -> None:
    """The one idiom this grammar has for asking about absence ON PURPOSE must keep working, in
    both directions -- `espionage.assign_mission`'s `spy.mission != null` and
    `units.found_city`'s `player.city_count != null` are exactly that, and a per-operand refusal
    would have destroyed both (it did, in the first draft of this fix)."""
    assert evaluate_predicate("spy.mission != null", {"spy": {"mission": "MISSION_X"}}) is True
    assert evaluate_predicate("spy.mission != null", {"spy": {}}) is False
    assert evaluate_predicate("spy.mission == null", {"spy": {}}) is True

    found_city = "player.city_count != null and player.city_count > observed_city_count"
    assert (
        evaluate_predicate(found_city, {"player": {"city_count": None}, "observed_city_count": 1})
        is False
    )
    assert (
        evaluate_predicate(found_city, {"player": {"city_count": 2}, "observed_city_count": 1})
        is True
    )


def test_t314_or_still_confirms_off_a_decidable_branch_when_the_other_is_unevaluable() -> None:
    """`turn.end_turn`'s shape. An unevaluable operand must not swallow an `or` that some other
    branch genuinely satisfies -- the fix must cost no answer the evaluator could already give."""
    predicate = "game.turn_number == observed_turn_number and game.is_waiting_for_other_players"
    assert (
        evaluate_predicate(
            predicate.replace(" and ", " or "),
            {"game": {"is_waiting_for_other_players": True}, "observed_turn_number": 5},
        )
        is True
    )
    # ...and `unknown and false` is still `false`, not unevaluable.
    assert (
        evaluate_predicate(
            predicate,
            {"game": {"is_waiting_for_other_players": False}, "observed_turn_number": 5},
        )
        is False
    )
