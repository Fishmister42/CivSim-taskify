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

import inspect
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
from civsim_harness.provider.openrouter import OpenRouterProvider  # noqa: E402
from civsim_harness.provider.stochastic import (  # noqa: E402
    STOCHASTIC_MODEL_NAME_BY_POLICY,
    StochasticModelProvider,
)
from civsim_harness.run.composition import (  # noqa: E402
    PROVIDER_POLICY_NAMES,
    build_provider,
    build_runner_dependencies,
)
from live import goal_run  # noqa: E402
from live.goal_run import (  # noqa: E402
    Goal,
    GoalError,
    RunRecords,
    StepRecord,
    assess_feasibility,
    build_bindings,
    check_predicate,
    check_prerequisites,
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
    builds: list[str] | None = None,
    builds_reason: str | None = None,
) -> dict[str, Any]:
    """One `units.state` unit. `available_builds` / `build_options` / `available_builds_reason`
    are 720e30d's unit-panel build row, which the Lua reports for the SELECTED unit only."""
    record: dict[str, Any] = {
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
    if builds is not None:
        record["available_builds"] = list(builds)
        record["build_options"] = [
            {
                "improvement_type": improvement,
                "name": improvement.replace("IMPROVEMENT_", "").title(),
                "disabled": False,
                "is_recommended": index == 0,
            }
            for index, improvement in enumerate(builds)
        ]
    if builds_reason is not None:
        record["available_builds_reason"] = builds_reason
    return record


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


def test_no_shipped_goal_is_blocked_any_more() -> None:
    """Every block the library shipped with has since been closed by real harness work.

    `set_capital_production` and `build_a_builder` were blocked on `city.available_productions`
    always being `[]` (unblocked in cfc6cee, once the production list landed in 1d0b372), and
    `use_a_builder` on there being no action in `catalogs/actions/` that spends a builder charge
    at all (unblocked by 720e30d's `units.build_improvement`). Each unblocking was a `blocked_by`
    deletion, which is the shape the field is for.

    A goal that *is* blocked must still say something concrete -- the loop below keeps that rule
    alive for the next one rather than deleting it with the last block.
    """
    goals = load_goals()
    blocked = {g.goal_id: (g.blocked_by or "") for g in goals.values() if g.is_blocked}
    assert blocked == {}
    for goal_id, reason in blocked.items():  # pragma: no cover - nothing is blocked today
        assert len(reason.split()) >= 5, f"{goal_id}'s blocked_by does not say what is blocking it"


def test_depends_on_resolves_to_a_chain() -> None:
    goals = load_goals()
    chain = resolve_chain(goals["use_a_builder"], goals)
    assert [g.goal_id for g in chain] == ["build_a_builder", "use_a_builder"]
    assert [g.goal_id for g in resolve_chain(goals["change_research"], goals)] == [
        "change_research"
    ]


def test_the_builder_chain_loads_unblocked_and_validates_end_to_end() -> None:
    """build -> use, both parts unblocked, both predicates evaluable, both flipping on the
    observations the catalog declares (720e30d).

    The chain is the one place a goal's end state is another goal's starting state, so it is
    walked here as the driver walks it: part one's baseline and success, then part two's
    prerequisites, baseline and success, each against the units.state / map.state shapes
    `catalogs/observations/units.yaml` actually declares.
    """
    goals = load_goals()
    chain = resolve_chain(goals["use_a_builder"], goals)
    assert [g.goal_id for g in chain] == ["build_a_builder", "use_a_builder"]
    assert all(not g.is_blocked for g in chain)

    # The charge-spending action really is in the catalog now, so its applied-count symbol binds.
    assert "units.build_improvement" in goal_run.action_declaration_ids()
    assert (
        goal_run.applied_action_symbol("units.build_improvement")
        in goal_run.known_fact_names()
    )
    for goal in chain:
        goal_run.validate_predicate(goal.success, label=f"{goal.goal_id}:success")
        for predicate in goal.prerequisites:
            goal_run.validate_predicate(predicate, label=f"{goal.goal_id}:prereq")

    build, use = chain

    # -- part one: a city, no builder yet -> the builder appears ------------------------------
    part_one_start = derive_facts(
        observation(cities=[city(65536, productions=["UNIT_BUILDER"])], units=[])
    )
    assert all(
        check.met
        for check in check_prerequisites(build, build_bindings(part_one_start))[1]
    )
    assert (
        check_predicate(
            build.success, build_bindings(part_one_start, start_facts=part_one_start)
        ).met
        is False
    )
    built = derive_facts(
        observation(
            cities=[city(65536, productions=["UNIT_BUILDER"])],
            units=[unit(131075, "UNIT_BUILDER", charges=3)],
        )
    )
    assert check_predicate(build.success, build_bindings(built, start_facts=part_one_start)).met

    # -- part two starts from part one's end state --------------------------------------------
    assert all(check.met for check in check_prerequisites(use, build_bindings(built))[1])
    assert check_predicate(use.success, build_bindings(built, start_facts=built)).met is False

    spent = derive_facts(
        observation(
            cities=[city(65536)],
            units=[
                unit(
                    131075,
                    "UNIT_BUILDER",
                    selected=True,
                    charges=2,
                    builds=["IMPROVEMENT_FARM"],
                )
            ],
            plots=[{"x": 43, "y": 31, "owner_player_id": 0, "improvement": "IMPROVEMENT_FARM"}],
        )
    )
    assert check_predicate(use.success, build_bindings(spent, start_facts=built)).met


def test_the_last_charge_case_the_action_s_own_verification_cannot_confirm() -> None:
    """A Builder spending its LAST charge is consumed and leaves the map.

    `units.build_improvement`'s verification predicate reads that as rejected on purpose
    (720e30d's DOCUMENTED LIMIT, FR-011: never read an unconfirmable effect as success). The
    goal's success predicate is an aggregate over the player's Builders plus the plot's own
    improvement, so it still reports the item as done -- which is exactly why the goal keeps its
    own reading rather than deferring to the action's.
    """
    use = load_goals()["use_a_builder"]
    start = derive_facts(observation(units=[unit(1, "UNIT_BUILDER", charges=1)]))
    gone = derive_facts(
        observation(
            units=[unit(2, "UNIT_WARRIOR", selected=True)],
            plots=[{"x": 43, "y": 31, "owner_player_id": 0, "improvement": "IMPROVEMENT_MINE"}],
        )
    )
    assert check_predicate(use.success, build_bindings(gone, start_facts=start)).met


def test_the_selected_units_build_buttons_are_derived_for_the_selected_unit_only() -> None:
    """units.state reports `available_builds` only for the unit whose panel is open (720e30d)."""
    facts = derive_facts(
        observation(
            units=[
                unit(1, "UNIT_BUILDER", selected=True, charges=2, builds=["IMPROVEMENT_FARM"]),
                unit(2, "UNIT_BUILDER", charges=2, builds=["IMPROVEMENT_MINE"]),
            ]
        )
    )
    assert facts["player"]["units"]["selected_available_builds"] == ["IMPROVEMENT_FARM"]
    assert facts["player"]["units"]["selected_build_options_count"] == 1

    nothing_selected = derive_facts(observation(units=[unit(2, "UNIT_BUILDER", charges=2)]))
    assert nothing_selected["player"]["units"]["selected_available_builds"] == []
    assert nothing_selected["player"]["units"]["selected_available_builds_reason"] is None

    unasked = derive_facts(
        observation(
            units=[unit(1, "UNIT_BUILDER", selected=True, charges=2, builds_reason="no panel")]
        )
    )
    # An empty list with a reason is distinct from an empty list without one -- the distinction
    # 720e30d added precisely so an unasked query cannot read as "this tile offers nothing".
    assert unasked["player"]["units"]["selected_available_builds"] == []
    assert unasked["player"]["units"]["selected_available_builds_reason"] == "no panel"


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
    """The block is a statement about the harness, so it outranks a satisfied prerequisite.

    Built from a synthetic library rather than whichever shipped goal happens to be blocked
    today, so unblocking a real goal (cfc6cee) never breaks this rule's own test.
    """
    satisfiable = Goal(
        goal_id="blocked_sample",
        title="Blocked sample",
        objective="Do one thing.",
        success="player.cities.count >= 1",
        prerequisites=("player.cities.count >= 1",),
        turn_cap=2,
        blocked_by="a body the harness cannot read yet",
    )
    report = {
        row.goal_id: row
        for row in assess_feasibility(
            observation(cities=[city(65536)]), goals={satisfiable.goal_id: satisfiable}
        )
    }
    blocked = report["blocked_sample"]
    assert all(check.met for check in blocked.prerequisites)
    assert blocked.runnable is False
    assert blocked.blocked_by is not None
    assert "BLOCKED" in format_feasibility(list(report.values()))


def test_feasibility_text_names_the_unmet_prerequisite() -> None:
    text = format_feasibility(assess_feasibility(observation()))
    assert "player.units.settler_count >= 1" in text
    assert "runnable now" in text


# --------------------------------------------------------------------------
# 3b. The provider hand-off, against the REAL composition root
# --------------------------------------------------------------------------
#
# MEASURED 2026-09-21: T262 (307a630) gave `run/composition.build_provider` and the demo driver's
# `resolve_provider` a new required `policy` argument. This driver's only call site sat three
# frames inside a live run, so the drift surfaced as a `TypeError` on the client -- a whole live
# block lost to a signature change the suite could have caught. Every test below builds the
# driver's provider through the real production function, never a fake, for exactly that reason.


@pytest.mark.parametrize("provider_name", ["openrouter", "stochastic", "fake"])
@pytest.mark.parametrize("policy", list(PROVIDER_POLICY_NAMES))
def test_the_driver_can_build_every_provider_through_the_real_composition_root(
    provider_name: str, policy: str
) -> None:
    """`build_goal_provider` is the driver's literal call site -- exercise it, not a copy."""
    provider = goal_run.build_goal_provider(provider_name, seed=7, policy=policy)
    if provider_name == "openrouter":
        # Deliberately None: the composition root builds its own `OpenRouterProvider` default,
        # so this driver never constructs the paid adapter itself (demo_landed_run's own rule).
        assert provider is None
    else:
        assert provider is not None
        assert hasattr(provider, "complete")


@pytest.mark.parametrize("policy", list(PROVIDER_POLICY_NAMES))
def test_the_composition_roots_own_build_provider_takes_the_policy_the_driver_passes(
    policy: str,
) -> None:
    """The production function itself, called with the driver's exact keyword arguments."""
    stochastic = build_provider("stochastic", seed=7, policy=policy)
    assert isinstance(stochastic, StochasticModelProvider)
    assert isinstance(build_provider("openrouter", seed=7, policy=policy), OpenRouterProvider)


def test_the_stochastic_provider_actually_honours_the_policy_the_driver_forwards() -> None:
    """Not merely accepted and dropped: the policy reaches what the store records as the model.

    The two policies must never be indistinguishable in `model_calls` (provider/stochastic.py's
    own P3 note), so a goal run's record says which sampler produced it.
    """
    uniform = goal_run.build_goal_provider("stochastic", seed=7, policy="uniform")
    coverage = goal_run.build_goal_provider("stochastic", seed=7, policy="coverage")
    assert uniform.policy == "uniform"
    assert coverage.policy == "coverage"
    assert STOCHASTIC_MODEL_NAME_BY_POLICY["uniform"] != STOCHASTIC_MODEL_NAME_BY_POLICY[
        "coverage"
    ]


def test_the_driver_offers_the_same_policy_flag_and_default_as_the_demo_driver() -> None:
    parser = goal_run.build_parser()
    args = parser.parse_args(["--goal", "change_research", "out"])
    assert args.provider_policy == goal_run.DEFAULT_PROVIDER_POLICY == "uniform"
    assert parser.parse_args(
        ["--goal", "change_research", "--provider-policy", "coverage", "out"]
    ).provider_policy == "coverage"
    with pytest.raises(SystemExit):
        parser.parse_args(["--goal", "change_research", "--provider-policy", "invented", "out"])


def test_every_call_this_driver_reuses_from_the_demo_driver_still_binds() -> None:
    """The same drift guard, widened to every function this driver reuses rather than copies.

    `demo_landed_run` is shared, live-facing code that other agents edit during a play day; a
    goal run that only discovers a changed signature three frames into a started run costs a
    block. Binding each call here is cheap and fails in CI instead.
    """
    demo = goal_run._demo()
    inspect.signature(demo.read_setup).bind()
    inspect.signature(demo.write_config).bind(
        Path("out"), {}, provider="openrouter", turns=3, use_seed_set=True
    )
    inspect.signature(demo.store_counts).bind(Path("store.db"), "run-1")
    inspect.signature(demo.resolve_provider).bind("stochastic", seed=0, policy="uniform")
    inspect.signature(demo.Recorder).bind(object())
    inspect.signature(demo.get_host_platform).bind()


def test_the_composition_root_still_accepts_the_drivers_dependency_wiring() -> None:
    """`guidance_root` is how the objective reaches the agent -- a rename would silence the goal."""
    inspect.signature(build_runner_dependencies).bind(
        store=object(),
        host=object(),
        catalog_root=Path("catalogs"),
        guidance_root=Path("out"),
        provider=None,
    )


def test_run_goal_accepts_and_forwards_the_policy() -> None:
    """The keyword really reaches `run_goal`, so `main()`'s forwarding cannot silently drop it."""
    signature = inspect.signature(goal_run.run_goal)
    assert "provider_policy" in signature.parameters
    signature.bind(
        load_goals()["change_research"],
        Path("out"),
        provider="stochastic",
        provider_seed=0,
        provider_policy="coverage",
        turns=2,
        store_path=Path("store.db"),
        host=None,
        recorder=None,
        use_seed_set=True,
        force=False,
    )


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
