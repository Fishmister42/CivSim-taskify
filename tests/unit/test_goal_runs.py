"""Goal runs (`tests/live/goal_run.py` + `tests/live/goals/`) against the repo's fakes.

Nothing here is marked ``live``: every test below runs with no Civilization VI client, no tuner,
no network and no store file -- the driver's collaborators are injected (see
``goal_run.drive_goal``) and the observations are built with ``tests/store_support/builders.py``'s
own entry shapes, so what is exercised is the goal library, the predicate grammar, the fact
derivation and the three stop rules, not a game.

The one thing these tests are most careful about is **Principle I**: `test_objective_is_all_the
_agent_gets` asserts that the file the composition root's guidance seam reads contains the
objective and *nothing* of the success predicate, the prerequisites, the block reason or the
notes.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

from civsim_harness.models.run import LifecycleState  # noqa: E402
from live import goal_run  # noqa: E402
from live.goal_run import (  # noqa: E402
    Goal,
    GoalError,
    RunRecords,
    StepRecord,
    assess_feasibility,
    build_bindings,
    check_predicate,
    derive_facts,
    drive_goal,
    format_feasibility,
    load_goals,
    parse_goal,
    resolve_chain,
    summarize,
    write_goal_guidance,
)

# --------------------------------------------------------------------------
# Fake observations -- the exact value shapes catalogs/observations/*.yaml declare
# --------------------------------------------------------------------------


def entry(declaration_id: str, value: Any) -> dict[str, Any]:
    return {
        "declaration_id": declaration_id,
        "key": declaration_id,
        "value": value,
        "context": "InGame",
    }


def city(city_id: int, *, name: str = "PASARGADAE", productions: list[str] | None = None,
         queue: list[str] | None = None, population: int = 1) -> dict[str, Any]:
    return {
        "city_id": city_id,
        "name": name,
        "owner_player_id": 0,
        "owner_is_local_player": True,
        "plot": {"x": 43, "y": 30},
        "population": population,
        "available_productions": productions or [],
        "production_queue": queue or [],
    }


def unit(
    unit_id: int,
    unit_type: str,
    *,
    selected: bool = False,
    charges: int | None = None,
    promotions: list[str] | None = None,
    movement: float = 2.0,
) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "unit_type": unit_type,
        "owner_player_id": 0,
        "owner_is_local_player": True,
        "is_selected": selected,
        "plot": {"x": 42, "y": 30},
        "movement_remaining": movement,
        "max_movement": 2,
        "reachable_plots": [{"x": 42, "y": 31}],
        "can_found_city": unit_type == "UNIT_SETTLER",
        "available_promotions": promotions or [],
        "charges_remaining": charges,
    }


def observation(
    *,
    turn: int = 1,
    cities: list[dict[str, Any]] | None = None,
    units: list[dict[str, Any]] | None = None,
    research: str | None = "TECH_POTTERY",
    researchable: list[str] | None = None,
    policies: list[str] | None = None,
    government: str | None = None,
    pantheon: bool = False,
    beliefs: list[str] | None = None,
    relations: list[dict[str, Any]] | None = None,
    gold: float = 25.0,
    plots: list[dict[str, Any]] | None = None,
    screen: str = "world",
    prompt_options: list[str] | None = None,
) -> list[dict[str, Any]]:
    return [
        entry(
            "game.turn_state",
            {
                "turn_number": turn,
                "is_local_player_turn": True,
                "is_waiting_for_other_players": False,
            },
        ),
        entry(
            "game.screen_state",
            {
                "screen": screen,
                "raw_screen_id": "InGame",
                "recognized": True,
                "has_blocking_prompt": screen.startswith("prompt."),
                "prompt_options": prompt_options or [],
            },
        ),
        entry("game.outcome_state", {"is_game_over": False, "outcome": "none"}),
        entry("cities.state", {"cities": cities or []}),
        entry("cities.selection", {"has_selection": False}),
        entry("units.state", {"units": units or []}),
        entry(
            "research.state",
            {
                "current_research": research,
                "current_civic": None,
                "researchable_techs": researchable or ["TECH_MINING", "TECH_SAILING"],
                "researchable_civics": [],
            },
        ),
        entry(
            "government.state",
            {
                "current_government": government,
                "available_governments": [],
                "available_policies": policies or [],
                "available_governors": [],
                "governors": [],
            },
        ),
        entry(
            "religion.state",
            {
                "pantheon_selected": pantheon,
                "religion_founded": False,
                "available_beliefs": beliefs or [],
                "city_majority_religions": [],
            },
        ),
        entry("diplomacy.state", {"relations": relations or []}),
        entry(
            "player.yields",
            {
                "gold_balance": gold,
                "faith_balance": 0,
                "science_per_turn": 2,
                "culture_per_turn": 1,
            },
        ),
        entry("map.state", {"width": 74, "height": 46, "revealed_plots": plots or []}),
    ]


def records(
    *steps: tuple[int, list[dict[str, Any]], str, str],
) -> RunRecords:
    """``(turn, entries, action_declaration_id, outcome)`` per decision step, in order."""
    out = RunRecords()
    for index, (turn, entries, action, outcome) in enumerate(steps, start=1):
        out.steps.append(
            StepRecord(
                turn_number=turn,
                step_index=index,
                entries=tuple(entries),
                action_declaration_id=action,
                outcome=outcome,
                rejection_reason="unavailable_to_human_now" if outcome == "rejected" else None,
            )
        )
    return out


# --------------------------------------------------------------------------
# 1. Goal loading and validation
# --------------------------------------------------------------------------


def test_every_shipped_goal_loads_and_validates() -> None:
    goals = load_goals()
    assert set(goals) == {
        "found_second_city",
        "set_capital_production",
        "change_research",
        "save_named_game",
        "move_unit_to_plot",
        "select_city_then_unit",
        "send_delegation",
        "slot_policy",
        "promote_unit",
        "found_pantheon",
        "answer_first_meeting",
        "build_a_builder",
        "use_a_builder",
    }
    for goal in goals.values():
        assert goal.objective, f"{goal.goal_id} has no objective"
        assert goal.success, f"{goal.goal_id} has no success predicate"
        assert goal.turn_cap >= 1


def test_shipped_blocked_goals_name_their_blocker() -> None:
    goals = load_goals()
    blocked = {g.goal_id: g.blocked_by for g in goals.values() if g.is_blocked}
    assert set(blocked) == {"set_capital_production", "build_a_builder", "use_a_builder"}
    assert "available_productions" in (blocked["set_capital_production"] or "")
    assert "available_productions" in (blocked["build_a_builder"] or "")
    assert "catalog gap" in (blocked["use_a_builder"] or "")


def test_depends_on_resolves_to_a_chain() -> None:
    goals = load_goals()
    chain = resolve_chain(goals["use_a_builder"], goals)
    assert [g.goal_id for g in chain] == ["build_a_builder", "use_a_builder"]
    assert [g.goal_id for g in resolve_chain(goals["change_research"], goals)] == [
        "change_research"
    ]


def _raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "sample",
        "title": "Sample",
        "objective": "Do one thing.",
        "success": "player.cities.count >= 1",
        "turn_cap": 3,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("predicate", "because"),
    [
        ("len(player.cities.ids) > 0", "function calls are outside the grammar"),
        ("player.cities.count * 2 >= 4", "multiplication is outside the grammar"),
        ("sneaky.hidden_state == true", "an unexposed root namespace"),
        ("player.cities.count >=", "not valid syntax"),
        ("player.citties.count >= 2", "a typo'd fact name resolves to nothing"),
        ("player.units.hidden_ai_plan != null", "a fact this module does not derive"),
        ("observed_applied_units_teleport >= 1", "an action the catalog does not declare"),
    ],
)
def test_bad_success_predicate_is_rejected(predicate: str, because: str) -> None:
    with pytest.raises(GoalError):
        parse_goal(_raw(success=predicate), source=because)


def test_bad_prerequisite_predicate_is_rejected() -> None:
    with pytest.raises(GoalError, match="prerequisites\\[1\\]"):
        parse_goal(_raw(prerequisites=["player.cities.count >= 1", "nonsense.field == 1"]))


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": None},
        {"success": ""},
        {"turn_cap": 0},
        {"turn_cap": "many"},
        {"strategy": "rush the capital"},
    ],
)
def test_malformed_goal_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(GoalError):
        parse_goal(_raw(**overrides))


def test_goal_id_must_match_its_file_name(tmp_path: Path) -> None:
    (tmp_path / "alpha.yaml").write_text(
        "id: beta\ntitle: B\nobjective: do it\nsuccess: player.cities.count >= 1\nturn_cap: 2\n",
        encoding="utf-8",
    )
    with pytest.raises(GoalError, match="does not match the file name"):
        load_goals(tmp_path)


def test_depends_on_must_name_a_known_goal(tmp_path: Path) -> None:
    (tmp_path / "alpha.yaml").write_text(
        "id: alpha\ntitle: A\nobjective: do it\nsuccess: player.cities.count >= 1\n"
        "turn_cap: 2\ndepends_on: nowhere\n",
        encoding="utf-8",
    )
    with pytest.raises(GoalError, match="unknown goal"):
        load_goals(tmp_path)


# --------------------------------------------------------------------------
# 2. Fact derivation and predicate evaluation on fake observations
# --------------------------------------------------------------------------


def test_facts_are_derived_from_the_catalog_field_names() -> None:
    facts = derive_facts(
        observation(
            turn=17,
            cities=[city(65536, productions=["UNIT_BUILDER"], queue=["UNIT_BUILDER"])],
            units=[
                unit(131073, "UNIT_WARRIOR", selected=True, promotions=["PROMOTION_BATTLECRY"]),
                unit(131074, "UNIT_SETTLER"),
                unit(131075, "UNIT_BUILDER", charges=3),
            ],
            relations=[
                {"player_id": 1, "has_met": True, "has_delegation": True},
                {"player_id": 2, "has_met": False},
            ],
            plots=[
                {"x": 43, "y": 30, "owner_player_id": 0, "improvement": "IMPROVEMENT_FARM"},
                {"x": 44, "y": 30, "owner_player_id": 0},
                {"x": 45, "y": 30, "owner_player_id": -1, "improvement": "IMPROVEMENT_MINE"},
            ],
        )
    )
    assert facts["game"]["turn_number"] == 17
    assert facts["player"]["player_id"] == 0
    assert facts["player"]["cities"]["count"] == 1
    assert facts["player"]["cities"]["available_productions"] == ["UNIT_BUILDER"]
    assert facts["player"]["cities"]["production_queue"] == ["UNIT_BUILDER"]
    assert facts["player"]["units"]["count"] == 3
    assert facts["player"]["units"]["settler_count"] == 1
    assert facts["player"]["units"]["builder_count"] == 1
    assert facts["player"]["units"]["builder_charges_total"] == 3
    assert facts["player"]["units"]["can_found_city_count"] == 1
    assert facts["player"]["units"]["promotions_available_count"] == 1
    assert facts["player"]["units"]["selected_unit_id"] == 131073
    assert facts["player"]["met_civ_count"] == 1
    assert facts["player"]["delegation_count"] == 1
    # only the local player's own plots, and only those carrying an improvement
    assert facts["player"]["owned_plot_count"] == 2
    assert facts["player"]["owned_improved_plot_count"] == 1


def test_another_civilizations_city_is_not_counted_as_the_players() -> None:
    theirs = city(70000, name="THEIRS")
    theirs["owner_player_id"] = 3
    theirs["owner_is_local_player"] = False
    facts = derive_facts(observation(cities=[city(65536), theirs]))
    assert facts["player"]["cities"]["count"] == 1


def test_found_second_city_success_predicate_moves_from_false_to_true() -> None:
    goal = load_goals()["found_second_city"]
    start = derive_facts(observation(cities=[city(65536)], units=[unit(1, "UNIT_SETTLER")]))

    still_one = build_bindings(
        derive_facts(observation(cities=[city(65536)])), start_facts=start
    )
    assert check_predicate(goal.success, still_one).met is False

    now_two = build_bindings(
        derive_facts(observation(cities=[city(65536), city(65537, name="SECOND")])),
        start_facts=start,
    )
    assert check_predicate(goal.success, now_two).met is True


def test_applied_action_counts_come_from_the_store_record() -> None:
    goal = load_goals()["save_named_game"]
    facts = derive_facts(observation())
    assert check_predicate(goal.success, build_bindings(facts)).met is False
    assert (
        check_predicate(
            goal.success, build_bindings(facts, applied_counts={"saves.save_game": 1})
        ).met
        is True
    )
    # A refused attempt is not an applied one: only `applied` outcomes are counted.
    refused = records((1, observation(), "saves.save_game", "rejected"))
    assert refused.applied_counts() == {}


def test_a_predicate_that_cannot_be_evaluated_is_not_met_and_says_why() -> None:
    # `player.yields` absent -> player.gold is None -> `player.gold >= 25` cannot be evaluated.
    entries = [e for e in observation() if e["declaration_id"] != "player.yields"]
    bindings = build_bindings(derive_facts(entries))
    result = check_predicate("player.gold >= 25", bindings)
    assert result.met is False
    assert result.reason is not None


def test_use_a_builder_accepts_either_reading_the_catalog_exposes() -> None:
    goal = load_goals()["use_a_builder"]
    start = derive_facts(observation(units=[unit(1, "UNIT_BUILDER", charges=3)]))

    spent_a_charge = build_bindings(
        derive_facts(observation(units=[unit(1, "UNIT_BUILDER", charges=2)])), start_facts=start
    )
    assert check_predicate(goal.success, spent_a_charge).met is True

    improved_a_plot = build_bindings(
        derive_facts(
            observation(
                units=[unit(1, "UNIT_BUILDER", charges=3)],
                plots=[{"x": 43, "y": 30, "owner_player_id": 0, "improvement": "IMPROVEMENT_FARM"}],
            )
        ),
        start_facts=start,
    )
    assert check_predicate(goal.success, improved_a_plot).met is True

    unchanged = build_bindings(
        derive_facts(observation(units=[unit(1, "UNIT_BUILDER", charges=3)])), start_facts=start
    )
    assert check_predicate(goal.success, unchanged).met is False


# --------------------------------------------------------------------------
# 3. Feasibility
# --------------------------------------------------------------------------


def test_feasibility_reports_which_goals_are_runnable_now() -> None:
    report = {
        row.goal_id: row
        for row in assess_feasibility(
            observation(cities=[city(65536)], units=[unit(1, "UNIT_WARRIOR", selected=True)])
        )
    }
    assert report["change_research"].runnable is True
    assert report["select_city_then_unit"].runnable is True
    assert report["found_second_city"].runnable is False
    assert report["send_delegation"].runnable is False


def test_a_blocked_goal_is_never_reported_runnable_even_with_its_prerequisites_met() -> None:
    entries = observation(cities=[city(65536, productions=["UNIT_BUILDER"])])
    report = {row.goal_id: row for row in assess_feasibility(entries)}
    blocked = report["set_capital_production"]
    assert all(check.met for check in blocked.prerequisites)
    assert blocked.runnable is False
    assert blocked.blocked_by is not None


def test_feasibility_text_names_the_unmet_prerequisite() -> None:
    text = format_feasibility(assess_feasibility(observation()))
    assert "player.units.settler_count >= 1" in text
    assert "runnable now" in text


# --------------------------------------------------------------------------
# 4. The driver's stop rules, against a fake runner
# --------------------------------------------------------------------------


@dataclass
class FakeStatus:
    lifecycle_state: LifecycleState
    current_turn: int
    current_step: int


class FakeRunner:
    """Enough of `run.runner.Runner` for `drive_goal`: a scripted status stream and a stop flag."""

    def __init__(self, states: list[FakeStatus]) -> None:
        self._states = states
        self.stop_requests: list[str] = []
        self.polls = 0

    def get_status(self, run_id: Any) -> FakeStatus:
        index = min(self.polls, len(self._states) - 1)
        self.polls += 1
        return self._states[index]

    def request_stop(self, run_id: Any) -> None:
        self.stop_requests.append(str(run_id))
        # A stopped run reaches a terminal state, exactly as the real runner's does.
        self._states.append(FakeStatus(LifecycleState.FINISHED, self._states[-1].current_turn, 1))


def _playing(turns: int) -> list[FakeStatus]:
    return [FakeStatus(LifecycleState.PLAYING, t, 1) for t in range(1, turns + 1)]


def test_driver_stops_at_success() -> None:
    goal = load_goals()["found_second_city"]
    scripted = [
        records(
            (1, observation(cities=[city(65536)], units=[unit(1, "UNIT_SETTLER")]),
             "units.move_to", "applied")
        ),
        records(
            (1, observation(cities=[city(65536)], units=[unit(1, "UNIT_SETTLER")]),
             "units.move_to", "applied"),
            (2, observation(cities=[city(65536), city(65537)]), "units.found_city", "applied"),
        ),
    ]
    reads = iter(scripted + [scripted[-1]] * 10)
    runner = FakeRunner(_playing(6))
    result = drive_goal(
        goal,
        runner=runner,
        run_id="run-fake",
        read_records=lambda: next(reads),
        turn_cap=15,
        sleep=lambda _s: None,
    )
    assert result["reached"] is True
    assert result["reached_at_turn"] == 2
    assert result["stop_reason"] == "success"
    assert runner.stop_requests == ["run-fake"]
    # Stopped well inside the cap: the run never played out 15 turns.
    assert result["turns_recorded"] == [1, 2]


def test_driver_stops_at_the_turn_cap_without_success() -> None:
    goal = load_goals()["found_second_city"]
    at_cap = records(
        (1, observation(cities=[city(65536)], units=[unit(1, "UNIT_SETTLER")]),
         "units.move_to", "applied"),
        (2, observation(cities=[city(65536)], units=[unit(1, "UNIT_SETTLER")]),
         "turn.end_turn", "applied"),
        (3, observation(cities=[city(65536)], units=[unit(1, "UNIT_SETTLER")]),
         "turn.end_turn", "applied"),
    )
    runner = FakeRunner(_playing(6))
    result = drive_goal(
        goal,
        runner=runner,
        run_id="run-fake",
        read_records=lambda: at_cap,
        turn_cap=3,
        sleep=lambda _s: None,
    )
    assert result["reached"] is False
    assert result["stop_reason"] == "turn_cap"
    assert runner.stop_requests == ["run-fake"]
    assert result["success_check"]["met"] is False


def test_driver_stops_when_the_starting_observation_makes_the_goal_infeasible() -> None:
    goal = load_goals()["found_second_city"]  # needs a settler
    no_settler = records(
        (1, observation(cities=[city(65536)], units=[unit(1, "UNIT_WARRIOR")]),
         "turn.end_turn", "applied")
    )
    runner = FakeRunner(_playing(6))
    result = drive_goal(
        goal,
        runner=runner,
        run_id="run-fake",
        read_records=lambda: no_settler,
        turn_cap=15,
        sleep=lambda _s: None,
    )
    assert result["prerequisites_met"] is False
    assert result["stop_reason"] == "prerequisites_not_met"
    assert runner.stop_requests == ["run-fake"]


def test_force_plays_a_goal_whose_prerequisites_do_not_hold() -> None:
    goal = load_goals()["found_second_city"]
    no_settler = records(
        (1, observation(cities=[city(65536)], units=[unit(1, "UNIT_WARRIOR")]),
         "turn.end_turn", "applied")
    )
    runner = FakeRunner([FakeStatus(LifecycleState.PLAYING, 1, 1), FakeStatus(
        LifecycleState.FINISHED, 1, 1)])
    result = drive_goal(
        goal,
        runner=runner,
        run_id="run-fake",
        read_records=lambda: no_settler,
        turn_cap=15,
        force=True,
        sleep=lambda _s: None,
    )
    assert result["prerequisites_met"] is False
    assert runner.stop_requests == []


def test_a_paused_run_is_this_parts_terminal_state() -> None:
    goal = load_goals()["save_named_game"]
    some = records((1, observation(), "saves.save_game", "applied"))
    runner = FakeRunner([FakeStatus(LifecycleState.PAUSED, 1, 3)])
    result = drive_goal(
        goal,
        runner=runner,
        run_id="run-fake",
        read_records=lambda: some,
        turn_cap=2,
        sleep=lambda _s: None,
    )
    assert result["final_state"] == "paused"
    assert result["reached"] is True


# --------------------------------------------------------------------------
# 5. result.json / summary.md shape, and Principle I
# --------------------------------------------------------------------------


def test_result_shape_carries_everything_the_issue_entry_quotes() -> None:
    goal = load_goals()["move_unit_to_plot"]
    moved = records(
        (1, observation(units=[unit(1, "UNIT_WARRIOR", selected=True)]),
         "units.move_to", "applied"),
        (1, observation(units=[unit(1, "UNIT_WARRIOR", selected=True)]),
         "cities.set_production", "rejected"),
    )
    runner = FakeRunner([FakeStatus(LifecycleState.PLAYING, 1, 1)])
    result = drive_goal(
        goal,
        runner=runner,
        run_id="run-fake",
        read_records=lambda: moved,
        turn_cap=3,
        sleep=lambda _s: None,
    )
    for key in (
        "goal",
        "reached",
        "reached_at_turn",
        "steps",
        "turns_recorded",
        "actions",
        "actions_applied",
        "success_predicate",
        "success_check",
        "prerequisites_met",
        "final_state",
    ):
        assert key in result, key
    assert result["actions"]["units.move_to"] == {"applied": 1}
    assert result["actions"]["cities.set_production"] == {"unavailable_to_human_now": 1}
    assert result["actions_applied"] == {"units.move_to": 1}
    # the whole thing must survive the JSON round trip result.json performs
    assert json.loads(json.dumps(result, default=str))["goal"] == "move_unit_to_plot"


def test_summary_markdown_is_pasteable_and_states_principle_one() -> None:
    goal = load_goals()["change_research"]
    runner = FakeRunner([FakeStatus(LifecycleState.FINISHED, 1, 1)])
    part = drive_goal(
        goal,
        runner=runner,
        run_id="run-fake",
        read_records=lambda: records(
            (1, observation(research="TECH_MINING"), "research.set_tech", "applied")
        ),
        turn_cap=2,
        sleep=lambda _s: None,
    )
    text = summarize(
        {
            "goal": goal.goal_id,
            "provider": "openrouter",
            "started_at": "2026-09-21T18:00:00Z",
            "finished_at": "2026-09-21T18:05:00Z",
            "parts": [part],
            "recording": {"frames": 12, "gif": "harness-landed-run.gif", "gif_kb": 900,
                          "keyframes": ["keyframe_0_t000s.png"]},
        }
    )
    assert "### Goal run: change_research" in text
    assert "`research.set_tech`" in text
    assert "Principle I" in text
    assert "success predicate (harness-side)" in text


def test_objective_is_all_the_agent_gets(tmp_path: Path) -> None:
    """Principle I, enforced on the one file that reaches the agent's context."""
    goal = Goal(
        goal_id="sample",
        title="Sample",
        objective="Found a second city.",
        success="player.cities.count >= 2",
        prerequisites=("player.units.settler_count >= 1",),
        turn_cap=4,
        blocked_by="a body gap nobody should read about",
        notes="an internal note about the catalog",
    )
    written = write_goal_guidance(goal, tmp_path).read_text(encoding="utf-8")
    assert "Found a second city." in written
    assert goal.success not in written
    assert "player.cities.count" not in written
    assert "settler_count" not in written
    assert "blocked" not in written.lower()
    assert "an internal note" not in written


def test_every_shipped_objective_is_free_of_harness_vocabulary() -> None:
    """The objective is prose about the game, never about the harness's own machinery."""
    forbidden = (
        "observed_applied_",
        "observed_start_",
        "declaration_id",
        "predicate",
        "cities.state",
        "units.state",
        "blocked_by",
    )
    for goal in load_goals().values():
        lowered = goal.objective.lower()
        for token in forbidden:
            assert token not in lowered, f"{goal.goal_id} objective mentions {token!r}"


def test_the_module_never_writes_a_predicate_into_the_guidance_preamble() -> None:
    assert "success" not in goal_run.GOAL_GUIDANCE_PREAMBLE.lower()
    assert "predicate" not in goal_run.GOAL_GUIDANCE_PREAMBLE.lower()
