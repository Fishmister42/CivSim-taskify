"""T262: the agent is shown which commands the game is offering, and why the rest are greyed out.

MEASURED (2026-09-21, `uv run civsim store coverage`): 23 of 38 catalog actions had ever been
attempted and only 9 ever applied. Nine of the fourteen never-applied actions were draws for a
situation that was not on screen -- a promotion with no promotable unit, a pantheon with no
faith, a peace offer with nobody at war, a prompt answer with no prompt up -- refused at dispatch
as `unavailable_to_human_now` without ever reaching the game, because availability predicates
were evaluated only in the executor and never rendered.

These tests drive the production renderer (`agent/context.py`) over fake observations and the
production evaluator (`act/availability.py`) over the real shipped catalog. Nothing here
constructs a store, a host adapter or a game.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.act.availability import (
    ActionAvailability,
    availability_by_action,
    evaluate_action_availability,
)
from civsim_harness.agent.context import (
    AVAILABLE_GROUP_HEADER,
    UNAVAILABLE_GROUP_HEADER,
    UNAVAILABLE_REASON_MARKER,
    assemble_action_catalog_text,
)
from civsim_harness.capability.loader import load_catalog
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.turn import Observation, ObservationEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"


# --------------------------------------------------------------------------
# Fake boards
# --------------------------------------------------------------------------


def _observation(entries: Sequence[tuple[str, Any]]) -> Observation:
    return Observation(
        observation_id=ObservationId("obs-1"),
        decision_step_id=DecisionStepId("step-1"),
        assembled_at=datetime(2026, 9, 21, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="2026.09.3", content_hash="deadbeef"),
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


def _action(declaration_id: str, predicate: str, **overrides: Any) -> ParityDeclaration:
    base: dict[str, Any] = {
        "declaration_id": declaration_id,
        "kind": "action",
        "summary": f"Do {declaration_id}.",
        "parity_basis": "Click the command in the panel.",
        "context": "InGame",
        "capability_id": "units.orders",
        "availability_predicate": predicate,
        "verification_predicate": "unit.plot == target",
        "introduced_in_version": "2026.09.3",
    }
    return ParityDeclaration.model_validate({**base, **overrides})


def _selected_settler() -> tuple[str, Any]:
    return (
        "units.state",
        {
            "units": [
                {
                    "unit_id": 65536,
                    "is_selected": True,
                    "owner_is_local_player": True,
                    "movement_remaining": 2,
                    "can_found_city": True,
                    "available_promotions": [],
                    "plot": {"x": 43, "y": 31},
                    "reachable_plots": [{"x": 44, "y": 31}],
                }
            ]
        },
    )


def _quiet_world() -> tuple[str, Any]:
    return (
        "game.screen_state",
        {"screen": "world", "has_blocking_prompt": False, "prompt_options": []},
    )


def _open_prompt(screen: str) -> tuple[str, Any]:
    return (
        "game.screen_state",
        {"screen": screen, "has_blocking_prompt": True, "prompt_options": ["continue"]},
    )


# --------------------------------------------------------------------------
# Evaluating one action
# --------------------------------------------------------------------------


def test_a_selected_unit_makes_its_own_order_available() -> None:
    found = _action("units.found_city", "unit.is_selected and unit.can_found_city")
    row = evaluate_action_availability(found, _observation([_selected_settler()]))
    assert row == ActionAvailability("units.found_city", True, "")


def test_no_selected_unit_greys_out_a_unit_order_and_says_so() -> None:
    found = _action("units.found_city", "unit.is_selected and unit.can_found_city")
    row = evaluate_action_availability(found, _observation([("units.state", {"units": []})]))
    assert row.available is False
    assert row.reason == "no unit selected"


def test_a_selected_unit_that_cannot_do_it_says_which_part_fails() -> None:
    found = _action("units.found_city", "unit.is_selected and unit.can_found_city")
    board = _observation(
        [
            (
                "units.state",
                {"units": [{"unit_id": 1, "is_selected": True, "can_found_city": False}]},
            )
        ]
    )
    row = evaluate_action_availability(found, board)
    assert row.available is False
    assert row.reason == "the unit cannot found city"


def test_an_empty_choice_list_is_a_greyed_out_button_not_a_bad_target() -> None:
    """`target in <collection>` cannot be decided for a *particular* target before the agent
    picks one -- but whether there is anything at all to pick is on screen, and that is exactly
    the measured pantheon-with-no-faith draw."""
    pantheon = _action("religion.select_pantheon", "target in player.available_beliefs")
    empty = evaluate_action_availability(
        pantheon, _observation([("religion.state", {"available_beliefs": []})])
    )
    assert empty.available is False
    assert empty.reason == "there is nothing to choose: player.available_beliefs is empty"

    offered = evaluate_action_availability(
        pantheon, _observation([("religion.state", {"available_beliefs": ["BELIEF_DANCE"]})])
    )
    assert offered.available is True


def test_a_collection_the_board_never_reported_is_not_guessed_at() -> None:
    pantheon = _action("religion.select_pantheon", "target in player.available_beliefs")
    row = evaluate_action_availability(pantheon, _observation([_quiet_world()]))
    assert row.available is False
    assert row.reason == "the board is not showing player.available_beliefs"


def test_a_prompt_answer_is_available_only_while_that_prompt_is_up() -> None:
    answer = _action(
        "prompts.era_transition",
        'game.has_blocking_prompt and game.current_screen == "prompt.era_transition"',
    )
    quiet = evaluate_action_availability(answer, _observation([_quiet_world()]))
    assert (quiet.available, quiet.reason) == (False, "no blocking prompt")

    other = evaluate_action_availability(answer, _observation([_open_prompt("prompt.pantheon")]))
    assert other.available is False
    assert other.reason == 'game.current_screen is "prompt.pantheon", not "prompt.era_transition"'

    mine = evaluate_action_availability(
        answer, _observation([_open_prompt("prompt.era_transition")])
    )
    assert mine.available is True


def test_it_is_not_your_turn_greys_out_the_end_turn() -> None:
    end_turn = _action(
        "turn.end_turn", "game.is_local_player_turn and not game.has_blocking_prompt"
    )
    board = [("game.turn_state", {"is_local_player_turn": False}), _quiet_world()]
    row = evaluate_action_availability(end_turn, _observation(board))
    assert (row.available, row.reason) == (False, "it is not your turn")


def test_a_blocking_prompt_greys_out_the_end_turn_with_the_negated_reason() -> None:
    end_turn = _action(
        "turn.end_turn", "game.is_local_player_turn and not game.has_blocking_prompt"
    )
    board = [
        ("game.turn_state", {"is_local_player_turn": True}),
        _open_prompt("prompt.natural_disaster"),
    ]
    row = evaluate_action_availability(end_turn, _observation(board))
    assert (row.available, row.reason) == (False, "a prompt is blocking play")


# --------------------------------------------------------------------------
# "Any of these?" -- the subjects the harness resolves from `target` alone
# --------------------------------------------------------------------------


def _relations(*rows: dict[str, Any]) -> tuple[str, Any]:
    return ("diplomacy.state", {"relations": list(rows)})


MAKE_PEACE = 'other_player.has_met and other_player.diplomatic_state == "war"'


def test_peace_is_offered_when_someone_is_at_war_with_you() -> None:
    peace = _action("diplomacy.make_peace", MAKE_PEACE)
    board = _observation(
        [
            _relations(
                {"player_id": 3, "has_met": True, "diplomatic_state": "peace"},
                {"player_id": 4, "has_met": True, "diplomatic_state": "war"},
            )
        ]
    )
    assert evaluate_action_availability(peace, board).available is True


def test_peace_is_greyed_out_when_nobody_is_at_war() -> None:
    """The measured "peace with no war" draw: no `target` names the subject before the agent
    chooses, so the question a human's greyed-out button answers is "is there ANY civilization
    this holds for" -- asked of the list the observation itself shows."""
    peace = _action("diplomacy.make_peace", MAKE_PEACE)
    board = _observation(
        [_relations({"player_id": 3, "has_met": True, "diplomatic_state": "peace"})]
    )
    row = evaluate_action_availability(peace, board)
    assert row.available is False
    assert row.reason == (
        'no other civilization qualifies: other_player.diplomatic_state is "peace", not "war"'
    )


def test_an_empty_subject_list_says_the_board_shows_none() -> None:
    peace = _action("diplomacy.make_peace", MAKE_PEACE)
    row = evaluate_action_availability(peace, _observation([_relations()]))
    assert (row.available, row.reason) == (False, "the board is showing no other civilization")


def test_a_spy_action_is_greyed_out_with_no_spy_and_lit_with_an_available_one() -> None:
    mission = _action("espionage.assign_mission", "spy.exists and spy.is_available")
    none = evaluate_action_availability(mission, _observation([("espionage.state", {"spies": []})]))
    assert (none.available, none.reason) == (False, "the board is showing no spy")

    busy = _observation([("espionage.state", {"spies": [{"unit_id": 7, "is_available": False}]})])
    assert evaluate_action_availability(mission, busy).available is False

    free = _observation([("espionage.state", {"spies": [{"unit_id": 7, "is_available": True}]})])
    assert evaluate_action_availability(mission, free).available is True


# --------------------------------------------------------------------------
# What is deliberately NOT decided before a target is named
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "predicate",
    ["target.is_revealed", "target >= 0.05 and target <= 1.0", 'target == "world"'],
)
def test_a_predicate_that_only_asks_about_the_chosen_target_stays_available(
    predicate: str,
) -> None:
    """Greying these out would grey out a button the game does not: whether the answer is yes
    depends on a value the agent has not chosen yet."""
    camera = _action("camera.move", predicate)
    assert evaluate_action_availability(camera, _observation([_quiet_world()])).available is True


def test_a_non_action_declaration_is_never_greyed_out_and_is_never_listed() -> None:
    observation = ParityDeclaration.model_validate(
        {
            "declaration_id": "units.state",
            "kind": "observation",
            "summary": "Units.",
            "parity_basis": "Look at the map.",
            "context": "InGame",
            "capability_id": "units.read",
            "output_schema": {"type": "object"},
            "introduced_in_version": "2026.09.3",
        }
    )
    board = _observation([_quiet_world()])
    assert evaluate_action_availability(observation, board).available is True
    assert availability_by_action([observation], board) == {}


# --------------------------------------------------------------------------
# Rendering -- the two groups the agent actually reads
# --------------------------------------------------------------------------


FOUND_CITY = _action(
    "units.found_city",
    "unit.is_selected and unit.can_found_city",
    summary="Found a city with a settler unit at its current plot.",
    target_kind="none",
    target_hint="acts on the selected settler; send no target",
)
PROMOTE = _action(
    "units.promote",
    "unit.is_selected and target in unit.available_promotions",
    summary="Apply an available promotion to a unit.",
    target_kind="name",
    target_hint="a promotion from the selected unit's available_promotions",
)


def _rendered(entries: Sequence[tuple[str, Any]]) -> str:
    return assemble_action_catalog_text(
        [FOUND_CITY, PROMOTE], observation=_observation(entries)
    )


def _group(text: str, header: str) -> list[str]:
    """The lines under *header*, up to the blank line that ends its group."""
    lines = text.split("\n")
    group: list[str] = []
    for line in lines[lines.index(header) + 1 :]:
        if not line:
            break
        group.append(line)
    return group


def test_the_catalog_is_rendered_in_two_groups_with_the_reason_on_the_greyed_out_line() -> None:
    text = _rendered([_selected_settler()])

    available = _group(text, AVAILABLE_GROUP_HEADER)
    assert available == [
        "- units.found_city: Found a city with a settler unit at its current plot. "
        '-- target: no target (parameters: {}); acts on the selected settler; send no target'
    ]

    unavailable = _group(text, UNAVAILABLE_GROUP_HEADER)
    assert len(unavailable) == 1
    assert unavailable[0].startswith("- units.promote: Apply an available promotion to a unit.")
    assert unavailable[0].endswith(
        UNAVAILABLE_REASON_MARKER
        + "there is nothing to choose: unit.available_promotions is empty"
    )


def test_an_available_action_never_carries_a_reason() -> None:
    text = _rendered([_selected_settler()])
    assert UNAVAILABLE_REASON_MARKER not in _group(text, AVAILABLE_GROUP_HEADER)[0]


def test_an_empty_group_says_none_without_looking_like_an_action() -> None:
    text = _rendered([("units.state", {"units": []})])
    assert _group(text, AVAILABLE_GROUP_HEADER) == ["(none)"]
    assert len(_group(text, UNAVAILABLE_GROUP_HEADER)) == 2


def test_the_reason_is_rendered_after_the_target_tail_never_inside_the_summary() -> None:
    """The tail's position is load-bearing: `provider/stochastic.py` reads an action's summary
    and its target hint out of this same line, and a reason mixed into either would be harvested
    as prose to sample a target from."""
    line = _group(_rendered([("units.state", {"units": []})]), UNAVAILABLE_GROUP_HEADER)[1]
    assert line.index(" -- target: ") < line.index(UNAVAILABLE_REASON_MARKER)


def test_without_an_observation_the_rendering_is_exactly_what_it_was_before() -> None:
    flat = assemble_action_catalog_text([FOUND_CITY, PROMOTE])
    assert AVAILABLE_GROUP_HEADER not in flat
    assert UNAVAILABLE_GROUP_HEADER not in flat
    assert UNAVAILABLE_REASON_MARKER not in flat
    assert len([line for line in flat.split("\n") if line.startswith("- ")]) == 2


def test_the_predicate_source_is_never_rendered_to_the_agent() -> None:
    """Principle I's other direction: the agent gets the tooltip, not the harness's internals."""
    text = _rendered([("units.state", {"units": []})])
    assert "unit.is_selected" not in text
    assert "availability_predicate" not in text
    for line in _group(text, UNAVAILABLE_GROUP_HEADER):
        reason = line.split(UNAVAILABLE_REASON_MARKER)[1]
        assert reason == "no unit selected"


# --------------------------------------------------------------------------
# Against the real shipped catalog
# --------------------------------------------------------------------------


def test_the_real_catalog_splits_on_a_real_board_the_way_a_human_screen_does() -> None:
    catalog = load_catalog(CATALOG_ROOT)
    actions = [d for d in catalog.declarations.values() if d.kind is DeclarationKind.ACTION]
    board = _observation(
        [
            _quiet_world(),
            ("game.turn_state", {"turn_number": 53, "is_local_player_turn": True}),
            _selected_settler(),
            ("cities.state", {"cities": []}),
            ("research.state", {"researchable_techs": ["TECH_POTTERY"]}),
            ("religion.state", {"available_beliefs": [], "pantheon_selected": False}),
            ("diplomacy.state", {"relations": [{"player_id": 3, "has_met": True,
                                                "diplomatic_state": "peace"}]}),
            ("espionage.state", {"spies": []}),
        ]
    )
    status = availability_by_action(actions, board)
    assert len(status) == len(actions) >= 36

    available = {key for key, row in status.items() if row.available}
    # A settler is selected on a quiet world map: found the city, move it, end the turn.
    assert {"units.found_city", "units.move_to", "turn.end_turn"} <= available
    # The measured wasted draws, every one now shown greyed out with its reason.
    for greyed in (
        "units.promote",
        "religion.select_pantheon",
        "diplomacy.make_peace",
        "prompts.natural_disaster",
        "espionage.assign_mission",
    ):
        assert status[greyed].available is False, greyed
        assert status[greyed].reason, greyed


def test_every_shipped_action_produces_a_verdict_and_a_reason_on_an_empty_board() -> None:
    """Fail closed, and never silently: an action the board says nothing about must be greyed
    out with something a player can read, not left available by default."""
    catalog = load_catalog(CATALOG_ROOT)
    actions = [d for d in catalog.declarations.values() if d.kind is DeclarationKind.ACTION]
    status = availability_by_action(actions, _observation([]))
    for row in status.values():
        assert row.available or row.reason, row.declaration_id
        assert not row.reason.endswith(" "), row.declaration_id
