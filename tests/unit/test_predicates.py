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

    assert evaluate_predicate(declaration.verification_predicate, bindings) is False


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_camera_zoom_polarity_explicit_null_zoom_is_false_not_true() -> None:
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("camera.zoom")
    assert declaration.verification_predicate is not None

    observation = _camera_observation({"mode": "world", "zoom": None, "target_is_revealed": True})
    bindings = build_predicate_bindings(observation=observation, target=0.5)
    assert bindings["camera"]["zoom"] is None

    assert evaluate_predicate(declaration.verification_predicate, bindings) is False


@pytest.mark.skipif(not CATALOG_ROOT.is_dir(), reason="repo catalogs/ directory not present")
def test_camera_set_view_mode_polarity_explicit_null_mode_is_false_not_true() -> None:
    catalog = load_catalog(CATALOG_ROOT)
    registry = CapabilityRegistry(catalog=catalog)
    declaration = registry.resolve("camera.set_view_mode")
    assert declaration.verification_predicate is not None

    observation = _camera_observation({"mode": None, "zoom": 0.5, "target_is_revealed": True})
    bindings = build_predicate_bindings(observation=observation, target="world")
    assert bindings["camera"]["mode"] is None

    assert evaluate_predicate(declaration.verification_predicate, bindings) is False


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

    assert evaluate_predicate(declaration.verification_predicate, bindings) is False


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

    assert evaluate_predicate(declaration.verification_predicate, bindings) is False
