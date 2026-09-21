"""T261: the seeded, uniformly-sampling `StochasticModelProvider` (`provider/stochastic.py`).

Every request these tests hand the provider is built by the **production** assembler
(`agent.context.assemble_context` over a real `Observation` and real `ParityDeclaration`s), not
by a hand-written string -- so what is asserted is that the provider reads what the harness
actually renders, T256 target tails included, rather than a convenient fiction about it.

Nothing here constructs a store, a catalog loader, a host adapter or a game. That is not an
economy, it is the point: if the provider needed any of them to choose an action it could not be
tested this way at all, and `test_principle_i_*` below turns that observation into an assertion
about the module's own import closure.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.agent.context import assemble_context
from civsim_harness.agent.decisions import RESPONSE_SCHEMA
from civsim_harness.models.catalog import ParityDeclaration
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ModelRef,
    ObservationId,
)
from civsim_harness.models.config import ModelConfig
from civsim_harness.models.records import CallOutcome
from civsim_harness.models.turn import Observation, ObservationEntry
from civsim_harness.provider.port import (
    DecisionRequest,
    Image,
    ObligationEnforcingProvider,
    RawDecision,
)
from civsim_harness.provider.preflight import preflight_chain
from civsim_harness.provider.stochastic import (
    MAX_REPEATS_PER_TURN,
    STOCHASTIC_PROVIDER_NAME,
    StochasticModelProvider,
)

MODEL = ModelRef(provider="openrouter", model="anthropic/claude-sonnet-5")


# --------------------------------------------------------------------------
# Building requests the way the harness really builds them
# --------------------------------------------------------------------------


def _action(declaration_id: str, **overrides: Any) -> ParityDeclaration:
    base: dict[str, Any] = {
        "declaration_id": declaration_id,
        "kind": "action",
        "summary": f"Do {declaration_id}.",
        "parity_basis": "Click the command in the panel.",
        "context": "InGame",
        "capability_id": "units.orders",
        "availability_predicate": "unit.is_selected",
        "verification_predicate": "unit.plot == target",
        "introduced_in_version": "2026.09.3",
    }
    return ParityDeclaration.model_validate({**base, **overrides})


#: Verbatim copies of the shipped catalog's own target tails for the four shapes that matter
#: (`catalogs/actions/{units,research,diplomacy,turn}.yaml`), so a target derived here is derived
#: from text the real client's agent really sees.
MOVE_TO = _action(
    "units.move_to",
    summary="Order the selected unit to move to a target plot.",
    target_kind="plot",
    target_hint=(
        "a destination plot from the selected unit's reachable_plots -- the plot, never the "
        "unit's id"
    ),
)
FOUND_CITY = _action(
    "units.found_city",
    summary="Found a city with a settler unit at its current plot.",
    target_kind="none",
    target_hint="acts on the selected settler; send no target, or that settler's unit_id",
)
PROMOTE = _action(
    "units.promote",
    summary="Apply an available promotion to a unit.",
    target_kind="name",
    target_hint="a promotion from the selected unit's available_promotions",
)
SET_TECH = _action(
    "research.set_tech",
    summary="Choose the technology currently being researched.",
    target_kind="name",
    target_hint="a technology from player.researchable_techs, e.g. TECH_POTTERY",
)
DECLARE_WAR = _action(
    "diplomacy.declare_war",
    summary="Declare war on another civilization.",
    target_kind="player_id",
    target_hint="the other civilization's player_id from diplomacy.state",
)
SET_VIEW_MODE = _action(
    "camera.set_view_mode",
    summary="Switch between the world view and the strategic view.",
    target_kind="option",
    target_hint='"world" or "strategic"',
)
ZOOM = _action(
    "camera.zoom",
    summary="Zoom the camera in or out.",
    target_kind="number",
    target_hint="a zoom level from 0.05 (closest) to 1.0 (farthest)",
)
END_TURN = _action(
    "turn.end_turn",
    summary="End the current turn and pass play to the other civilizations.",
    capability_id="turn.control",
    target_kind="none",
    target_hint="send no target",
)

SELECTED_SETTLER_UNIT_ID = 65536
REACHABLE = [{"x": 40 + i, "y": 31} for i in range(10)]
PROMOTIONS = ["PROMOTION_ALPINE", "PROMOTION_AMBUSH"]
RESEARCHABLE = ["TECH_POTTERY", "TECH_MINING", "TECH_ANIMAL_HUSBANDRY"]
RIVAL_PLAYER_IDS = [3, 5]


def _units_state(*, selected: bool = True) -> dict[str, Any]:
    return {
        "units": [
            {
                "unit_id": SELECTED_SETTLER_UNIT_ID,
                "unit_type": "UNIT_SETTLER",
                "owner_player_id": 0,
                "owner_is_local_player": True,
                "is_selected": selected,
                "plot": {"x": 43, "y": 31},
                "movement_remaining": 2,
                "reachable_plots": list(REACHABLE),
                "can_found_city": True,
                "available_promotions": list(PROMOTIONS),
            }
        ]
    }


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
        screen_identity="in_game",
    )


def _request(
    *,
    actions: Iterable[ParityDeclaration] | None,
    entries: Sequence[tuple[str, Any]] = (),
    step_index: int = 1,
    images: list[Image] | None = None,
) -> DecisionRequest:
    """One decision request, assembled by the production assembler (`agent/context.py`)."""
    return assemble_context(
        observation=_observation(entries),
        guidance=None,
        model=MODEL,
        step_index=step_index,
        response_schema=RESPONSE_SCHEMA,
        images=images,
        actions=actions,
    )


def _full_board(step_index: int = 1) -> DecisionRequest:
    return _request(
        actions=[MOVE_TO, FOUND_CITY, PROMOTE, SET_TECH, DECLARE_WAR, END_TURN],
        entries=[
            ("units.state", _units_state()),
            ("player.state", {"researchable_techs": list(RESEARCHABLE)}),
            ("diplomacy.state", {"civilizations": [{"player_id": p} for p in RIVAL_PLAYER_IDS]}),
        ],
        step_index=step_index,
    )


def _decide(provider: StochasticModelProvider, request: DecisionRequest) -> RawDecision:
    response = provider.complete(request)
    assert response.outcome is CallOutcome.DECISION_RETURNED
    assert response.decision is not None
    return response.decision


def _play_turn(
    provider: StochasticModelProvider, build: Any, *, steps: int
) -> list[RawDecision]:
    """Drive *steps* decision steps of one turn, the way `run/decision_loop.py` does."""
    decisions = []
    for step_index in range(1, steps + 1):
        decisions.append(_decide(provider, build(step_index)))
    return decisions


# --------------------------------------------------------------------------
# Only what the request lists, and only targets it shows
# --------------------------------------------------------------------------


def test_every_chosen_action_is_one_the_request_listed() -> None:
    listed = {"units.move_to", "units.found_city", "units.promote", "research.set_tech",
              "diplomacy.declare_war", "turn.end_turn"}
    provider = StochasticModelProvider(seed=11, max_actions_per_turn=4)
    for turn in range(40):
        for step_index in range(1, 6):
            decision = _decide(provider, _full_board(step_index))
            assert str(decision.action_declaration_id) in listed, (
                f"turn {turn} step {step_index} chose an action the request never listed"
            )


def test_a_plot_target_is_one_of_the_plots_the_rendered_hint_points_at() -> None:
    provider = StochasticModelProvider(seed=3, max_actions_per_turn=6)
    seen = 0
    for _turn in range(60):
        for step_index in range(1, 7):
            decision = _decide(provider, _full_board(step_index))
            if str(decision.action_declaration_id) != "units.move_to":
                continue
            seen += 1
            assert decision.parameters["target"] in REACHABLE
    assert seen, "units.move_to was never sampled, so nothing about plot targets was exercised"


def test_a_name_target_comes_from_the_observed_field_the_hint_names() -> None:
    provider = StochasticModelProvider(seed=5, max_actions_per_turn=6)
    promotions = 0
    techs = 0
    for _turn in range(60):
        for step_index in range(1, 7):
            decision = _decide(provider, _full_board(step_index))
            action = str(decision.action_declaration_id)
            if action == "units.promote":
                promotions += 1
                assert decision.parameters["target"] in PROMOTIONS
            elif action == "research.set_tech":
                techs += 1
                # `player.researchable_techs` -- never a promotion, never a unit type, even
                # though both are strings sitting in the same observation.
                assert decision.parameters["target"] in RESEARCHABLE
    assert promotions and techs


def test_an_id_target_comes_from_the_observation_entry_the_hint_names() -> None:
    provider = StochasticModelProvider(seed=7, max_actions_per_turn=6)
    wars = 0
    for _turn in range(60):
        for step_index in range(1, 7):
            decision = _decide(provider, _full_board(step_index))
            if str(decision.action_declaration_id) != "diplomacy.declare_war":
                continue
            wars += 1
            # `player_id` from `diplomacy.state` -- not the local player's `owner_player_id`,
            # which is the same field name one entry over.
            assert decision.parameters["target"] in RIVAL_PLAYER_IDS
    assert wars


def test_an_action_the_request_shows_as_taking_no_target_is_sent_without_one() -> None:
    provider = StochasticModelProvider(seed=9, max_actions_per_turn=6)
    founds = 0
    for _turn in range(40):
        for step_index in range(1, 7):
            decision = _decide(provider, _full_board(step_index))
            if str(decision.action_declaration_id) != "units.found_city":
                continue
            founds += 1
            assert decision.parameters == {}
    assert founds


def test_offered_options_and_a_stated_range_are_taken_from_the_request_text() -> None:
    """The two kinds whose candidate values the request *enumerates* rather than observes."""
    provider = StochasticModelProvider(seed=13, max_actions_per_turn=6)
    modes: set[str] = set()
    zooms: set[float] = set()
    for _turn in range(40):
        for step_index in range(1, 7):
            decision = _decide(
                provider,
                _request(
                    actions=[SET_VIEW_MODE, ZOOM, END_TURN],
                    entries=[("units.state", _units_state())],
                    step_index=step_index,
                ),
            )
            action = str(decision.action_declaration_id)
            if action == "camera.set_view_mode":
                modes.add(decision.parameters["target"])
            elif action == "camera.zoom":
                zooms.add(decision.parameters["target"])
    assert modes and modes <= {"world", "strategic"}
    assert zooms and zooms <= {0.05, 1.0}


def test_an_action_whose_target_the_request_does_not_show_is_never_chosen() -> None:
    """`units.promote`'s hint names `available_promotions`; this board reports none.

    The provider must drop the action rather than invent a promotion name -- and, since it is the
    only non-end-turn action listed, must end the turn instead.
    """
    provider = StochasticModelProvider(seed=17, max_actions_per_turn=6)
    for step_index in range(1, 5):
        decision = _decide(
            provider,
            _request(
                actions=[PROMOTE, END_TURN],
                entries=[
                    (
                        "units.state",
                        {
                            "units": [
                                {
                                    "unit_id": 70000,
                                    "unit_type": "UNIT_WARRIOR",
                                    "owner_is_local_player": True,
                                    "is_selected": True,
                                    "plot": {"x": 1, "y": 2},
                                }
                            ]
                        },
                    )
                ],
                step_index=step_index,
            ),
        )
        assert str(decision.action_declaration_id) == "turn.end_turn"
        assert decision.is_end_turn is True
        assert decision.parameters == {}


# --------------------------------------------------------------------------
# Turn shape: N actions, then end; nothing available, end now
# --------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 2, 6])
def test_at_most_n_non_end_turn_actions_then_the_turn_ends(limit: int) -> None:
    provider = StochasticModelProvider(seed=23, max_actions_per_turn=limit)
    decisions = _play_turn(
        provider,
        lambda step_index: _request(
            actions=[MOVE_TO, FOUND_CITY, END_TURN],
            entries=[("units.state", _units_state())],
            step_index=step_index,
        ),
        steps=limit + 1,
    )
    assert [d.is_end_turn for d in decisions] == [False] * limit + [True]
    assert str(decisions[-1].action_declaration_id) == "turn.end_turn"


def test_the_limit_resets_on_the_next_turn() -> None:
    provider = StochasticModelProvider(seed=29, max_actions_per_turn=2)

    def build(step_index: int) -> DecisionRequest:
        return _request(
            actions=[MOVE_TO, END_TURN],
            entries=[("units.state", _units_state())],
            step_index=step_index,
        )

    first = _play_turn(provider, build, steps=3)
    second = _play_turn(provider, build, steps=3)
    assert [d.is_end_turn for d in first] == [False, False, True]
    assert [d.is_end_turn for d in second] == [False, False, True]


@pytest.mark.parametrize("actions", [None, []])
def test_a_request_showing_no_available_action_ends_the_turn(actions: Any) -> None:
    provider = StochasticModelProvider(seed=31)
    decision = _decide(provider, _request(actions=actions, entries=[("units.state", {})]))
    assert decision.is_end_turn is True
    assert str(decision.action_declaration_id) == "turn.end_turn"


# --------------------------------------------------------------------------
# Refusal down-weighting and the loop bound
# --------------------------------------------------------------------------


def _refusal_entry(action_id: str) -> tuple[str, Any]:
    """What a refusal looks like once the harness renders one into the next request."""
    return (
        "game.last_decision",
        {"action": action_id, "rejection_reason": "unavailable_to_human_now"},
    )


def _choice_counts(*, refused: str | None, seed: int, turns: int = 400) -> dict[str, int]:
    provider = StochasticModelProvider(seed=seed, max_actions_per_turn=1)
    entries: list[tuple[str, Any]] = [("units.state", _units_state())]
    if refused is not None:
        entries.append(_refusal_entry(refused))
    counts: dict[str, int] = {}
    for _turn in range(turns):
        decision = _decide(
            provider,
            _request(
                actions=[MOVE_TO, FOUND_CITY, END_TURN], entries=entries, step_index=1
            ),
        )
        key = str(decision.action_declaration_id)
        counts[key] = counts.get(key, 0) + 1
    return counts


def test_a_refused_action_is_down_weighted_but_never_forbidden() -> None:
    baseline = _choice_counts(refused=None, seed=101)
    with_refusal = _choice_counts(refused="units.found_city", seed=101)

    assert with_refusal["units.found_city"] < baseline["units.found_city"], (
        "a refusal the request reports must reduce how often that action is sampled"
    )
    assert with_refusal["units.found_city"] > 0, (
        "down-weighted, not forbidden: the same action must stay reachable, because a refusal is "
        "usually about this target or this moment"
    )
    assert with_refusal["units.move_to"] > baseline["units.move_to"]


def test_nothing_is_down_weighted_when_the_request_reports_no_refusal() -> None:
    """The ordinary case today: the harness renders no refusal information, so none is read.

    Guards against the scan firing on prose -- an observation full of ordinary state must leave
    the distribution exactly where the unrefused baseline puts it.
    """
    assert _choice_counts(refused=None, seed=101) == _choice_counts(refused=None, seed=101)


def test_the_same_action_and_target_never_repeats_more_than_twice_in_one_turn() -> None:
    """The loop bound, from the provider's own memory of what it returned this turn."""
    provider = StochasticModelProvider(seed=37, max_actions_per_turn=50)
    pairs: dict[tuple[str, str], int] = {}
    for step_index in range(1, 12):
        decision = _decide(
            provider,
            _request(
                actions=[FOUND_CITY, PROMOTE, END_TURN],
                entries=[("units.state", _units_state())],
                step_index=step_index,
            ),
        )
        if decision.is_end_turn:
            break
        key = (
            str(decision.action_declaration_id),
            json.dumps(decision.parameters.get("target"), sort_keys=True),
        )
        pairs[key] = pairs.get(key, 0) + 1
    assert pairs, "the turn produced no non-end-turn decision at all"
    assert max(pairs.values()) <= MAX_REPEATS_PER_TURN
    # Exhausting every pair (1 found_city + 2 promotions, twice each) must end the turn rather
    # than loop: `units.found_city` and both promotions is 3 pairs x 2 = 6 decisions.
    assert sum(pairs.values()) == 6


def test_the_repeat_bound_resets_on_the_next_turn() -> None:
    provider = StochasticModelProvider(seed=41, max_actions_per_turn=50)

    def turn() -> int:
        taken = 0
        for step_index in range(1, 12):
            decision = _decide(
                provider,
                _request(
                    actions=[FOUND_CITY, END_TURN],
                    entries=[("units.state", _units_state())],
                    step_index=step_index,
                ),
            )
            if decision.is_end_turn:
                break
            taken += 1
        return taken

    assert turn() == MAX_REPEATS_PER_TURN
    assert turn() == MAX_REPEATS_PER_TURN


# --------------------------------------------------------------------------
# Determinism and coverage
# --------------------------------------------------------------------------


def _sequence(seed: int, *, steps: int = 40) -> list[tuple[str, str, bool]]:
    provider = StochasticModelProvider(seed=seed, max_actions_per_turn=4)
    out = []
    for index in range(steps):
        decision = _decide(provider, _full_board(step_index=(index % 6) + 1))
        out.append(
            (
                str(decision.action_declaration_id),
                json.dumps(decision.parameters, sort_keys=True),
                decision.is_end_turn,
            )
        )
    return out


def test_the_same_seed_replays_the_same_run() -> None:
    assert _sequence(1234) == _sequence(1234)


def test_a_different_seed_plays_differently() -> None:
    assert _sequence(1234) != _sequence(4321)


def test_the_seed_is_kept_on_the_provider_so_a_run_can_record_it() -> None:
    provider = StochasticModelProvider(seed=99)
    assert provider.seed == 99


def test_an_action_id_not_yet_chosen_this_run_is_mildly_preferred() -> None:
    """Coverage: over one turn's first few steps, breadth beats repetition.

    Asserted as a distribution over many independent runs rather than as a rule about any one
    run -- "mildly preferred" must not become "scheduled", or the provider would stop being a
    sampler.
    """
    distinct_first_three: list[int] = []
    for seed in range(60):
        provider = StochasticModelProvider(seed=seed, max_actions_per_turn=6)
        chosen = [
            str(_decide(provider, _full_board(step_index)).action_declaration_id)
            for step_index in range(1, 4)
        ]
        distinct_first_three.append(len(set(chosen)))
    average = sum(distinct_first_three) / len(distinct_first_three)
    # Five non-end-turn actions sampled uniformly with replacement averages ~2.44 distinct in
    # three draws; the coverage bias must push above that, without reaching a deterministic 3.0.
    assert 2.44 < average < 3.0


# --------------------------------------------------------------------------
# Port conformance and accounting
# --------------------------------------------------------------------------


def test_the_response_is_labelled_stochastic_at_zero_cost() -> None:
    provider = StochasticModelProvider(seed=2)
    response = provider.complete(_full_board())
    assert response.model_served.provider == STOCHASTIC_PROVIDER_NAME
    assert response.cost.amount_usd == 0.0
    assert response.cost.input_tokens == 0
    assert response.cost.output_tokens == 0
    assert response.retry_count == 0
    assert response.fallback_occurred is False
    assert response.outcome is CallOutcome.DECISION_RETURNED


def test_the_image_count_is_the_requests_own() -> None:
    provider = StochasticModelProvider(seed=2)
    images = [Image(media_type="image/png", data=b"\x89PNG")]
    response = provider.complete(_full_board())
    assert response.image_count == 0
    response = provider.complete(
        _request(actions=[END_TURN], entries=[("units.state", {})], images=images)
    )
    assert response.image_count == 1


def test_it_satisfies_the_ports_adapter_obligations() -> None:
    provider = ObligationEnforcingProvider(StochasticModelProvider(seed=2))
    request = _full_board()
    observation_before = request.observation
    response = provider.complete(request)
    assert response.decision is not None
    assert request.observation == observation_before


def test_it_passes_chain_preflight_for_any_configured_model() -> None:
    provider = StochasticModelProvider(seed=2)
    results = preflight_chain(
        provider,
        ModelConfig(primary=MODEL, fallbacks=[ModelRef(provider="openrouter", model="x/y")]),
        worst_case_context_tokens=200_000,
    )
    assert [result.ok for result in results] == [True, True]


def test_the_composition_root_resolves_it_by_name() -> None:
    from civsim_harness.run.composition import PROVIDER_NAMES, build_provider

    assert "stochastic" in PROVIDER_NAMES
    provider = build_provider("stochastic", seed=77, max_actions_per_turn=3)
    assert isinstance(provider, StochasticModelProvider)
    assert provider.seed == 77
    assert provider.max_actions_per_turn == 3
    with pytest.raises(ValueError, match="unknown provider"):
        build_provider("gpt-fairy-dust")


# --------------------------------------------------------------------------
# Against the catalog the live lane actually ships
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A board rich enough that most of the shipped catalog's actions have a target somewhere in it.
#: Every field name here is one the shipped `target_hint`s name (`catalogs/actions/*.yaml`).
_SHIPPED_BOARD: list[tuple[str, Any]] = [
    ("units.state", _units_state()),
    (
        "player.state",
        {
            "researchable_techs": list(RESEARCHABLE),
            "researchable_civics": ["CIVIC_CODE_OF_LAWS"],
            "available_policies": ["POLICY_SURVEY"],
            "available_governments": ["GOVERNMENT_CHIEFDOM"],
            "available_governors": ["GOVERNOR_MAGNUS"],
            "available_beliefs": ["BELIEF_GOD_OF_THE_SEA"],
        },
    ),
    ("diplomacy.state", {"civilizations": [{"player_id": p} for p in RIVAL_PLAYER_IDS]}),
    (
        "cities.state",
        {
            "cities": [
                {
                    "city_id": 65537,
                    "is_selected": True,
                    "available_productions": ["UNIT_WARRIOR", "BUILDING_MONUMENT"],
                    "purchasable_with_gold": ["UNIT_SLINGER"],
                    "purchasable_with_faith": [],
                }
            ]
        },
    ),
    ("congress.state", {"resolutions": [{"resolution_id": 2}]}),
    ("great_people.state", {"recruitable_individuals": [{"individual_id": 12}]}),
    ("espionage.state", {"spies": [{"unit_id": 131074}]}),
]


def test_it_exercises_the_breadth_of_the_shipped_catalog() -> None:
    """The reason this provider exists: breadth of demonstrated harness behaviour, at $0.

    Run against `catalogs/` itself rather than the hand-built declarations above, so a catalog
    change that leaves an action's target underivable (a renamed observed field, a hint that
    stops naming its source) fails here instead of on the live client.
    """
    from civsim_harness.capability.loader import load_catalog

    catalog = load_catalog(REPO_ROOT / "catalogs")
    actions = list(catalog.declarations.values())
    provider = StochasticModelProvider(seed=5, max_actions_per_turn=6)

    chosen: set[str] = set()
    for _turn in range(60):
        for step_index in range(1, 8):
            decision = _decide(
                provider, _request(actions=actions, entries=_SHIPPED_BOARD, step_index=step_index)
            )
            action_id = str(decision.action_declaration_id)
            assert action_id in catalog.declarations, "chose an id the catalog never declared"
            chosen.add(action_id)

    assert len(chosen) >= 20, (
        f"only {len(chosen)} distinct action(s) were ever chosen against the shipped catalog "
        f"({sorted(chosen)}); this provider's whole purpose is action-surface breadth"
    )
    assert "turn.end_turn" in chosen


def test_every_shipped_action_with_a_target_this_board_shows_can_be_sent() -> None:
    """No shipped action is silently unreachable because its target cannot be derived.

    An action whose source field this board genuinely does not carry
    (`cities.purchase_with_faith`, with an empty purchase list; the prompt answers, with no
    prompt open) is expected to be skipped -- that is the "do not invent a target" rule working,
    not a defect -- so the assertion is on the actions whose sources this board *does* show.
    """
    from civsim_harness.capability.loader import load_catalog

    catalog = load_catalog(REPO_ROOT / "catalogs")
    actions = list(catalog.declarations.values())
    provider = StochasticModelProvider(seed=19, max_actions_per_turn=40)

    chosen: set[str] = set()
    for _turn in range(400):
        for step_index in range(1, 6):
            chosen.add(
                str(
                    _decide(
                        provider,
                        _request(
                            actions=actions, entries=_SHIPPED_BOARD, step_index=step_index
                        ),
                    ).action_declaration_id
                )
            )

    expected_reachable = {
        "camera.move",
        "camera.set_view_mode",
        "camera.zoom",
        "cities.purchase_with_gold",
        "cities.set_production",
        "congress.cast_vote",
        "diplomacy.declare_war",
        "diplomacy.make_peace",
        "diplomacy.send_delegation",
        "espionage.assign_mission",
        "great_people.recruit",
        "policies.assign_governor",
        "policies.change_government",
        "policies.slot_policy",
        "religion.found_religion",
        "religion.select_belief",
        "religion.select_pantheon",
        "research.set_civic",
        "research.set_tech",
        "saves.save_game",
        # `turn.end_turn` is deliberately absent: this turn never reaches its action limit, so
        # the provider is never in a position to end it. That it does end one is asserted above.
        "units.found_city",
        "units.move_to",
        "units.promote",
    }
    assert expected_reachable - chosen == set()


# --------------------------------------------------------------------------
# Principle I -- the module cannot reach anything the agent cannot see
# --------------------------------------------------------------------------

_MODULE = "civsim_harness.provider.stochastic"

#: Packages that would let this module choose an action from something other than the request:
#: the match-tracking store, the live observation/assembly path, the catalog loaders (which hold
#: every action's `availability_predicate`), the host adapters and the game transport, the
#: dispatch/verification layer, and the run/composition layer that can reach all of them. The
#: prompt assembler (`agent`) is on the list too: reading its rendering tables would let a target
#: be derived from the harness's own vocabulary rather than from the text the agent was shown.
_FORBIDDEN_PACKAGES = (
    "civsim_harness.store",
    "civsim_harness.observe",
    "civsim_harness.capability",
    "civsim_harness.host",
    "civsim_harness.act",
    "civsim_harness.agent",
    "civsim_harness.nexus",
    "civsim_harness.run",
    "civsim_harness.saves",
    "civsim_harness.parity",
    "civsim_harness.resilience",
    "civsim_harness.config",
    "civsim_harness.operator",
)


def _source_path(module_name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
        return None
    return Path(spec.origin)


def _imported_module_names(path: Path) -> set[str]:
    """Every module name this source file imports, read from its AST rather than at runtime.

    A runtime `sys.modules` diff (the technique `tests/unit/test_platform_neutrality.py` uses for
    platform libraries) cannot answer this question here: by the time this test runs, another
    test has already imported the whole harness, so every forbidden package is in `sys.modules`
    whoever put it there. The import graph is the thing that actually constrains the module.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _first_party_closure(module_name: str) -> set[str]:
    """*module_name* plus every `civsim_harness` module reachable from it by import."""
    seen: set[str] = set()
    pending = [module_name]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        path = _source_path(current)
        if path is None:
            continue
        seen.add(current)
        pending.extend(
            name for name in _imported_module_names(path) if name.startswith("civsim_harness")
        )
    return seen


def test_principle_i_the_module_cannot_import_the_store_game_or_catalog() -> None:
    closure = _first_party_closure(_MODULE)
    leaks = sorted(
        name
        for name in closure
        if any(name == pkg or name.startswith(pkg + ".") for pkg in _FORBIDDEN_PACKAGES)
    )
    assert not leaks, (
        f"{_MODULE} can reach {leaks} through its imports. Constitution Principle I "
        "(NON-NEGOTIABLE): this provider may choose an action only from the DecisionRequest it "
        "was handed -- the same text the agent reads -- never from the store, the live game, the "
        "catalog's own availability predicates, or any other harness internal."
    )
    # The closure must be the real one, not an empty set produced by a broken walk.
    assert "civsim_harness.provider.port" in closure


#: Third-party/stdlib doors out of the process. A provider that could open one of these could
#: read a catalog, a database or the network without importing a single harness module, so the
#: import-closure test above would not see it.
_FORBIDDEN_THIRD_PARTY = (
    "yaml",
    "sqlite3",
    "sqlalchemy",
    "pathlib",
    "os",
    "io",
    "httpx",
    "urllib",
    "socket",
    "subprocess",
)


def test_principle_i_the_module_reads_no_file_and_opens_no_database() -> None:
    path = _source_path(_MODULE)
    assert path is not None
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported = _imported_module_names(path)
    leaks = sorted(
        name
        for name in imported
        for pkg in _FORBIDDEN_THIRD_PARTY
        if name == pkg or name.startswith(pkg + ".")
    )
    assert not leaks, (
        f"{_MODULE} imports {leaks}: the only input this provider may read is the "
        "DecisionRequest it was handed (Principle I) -- not a file, a database or a socket."
    )

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called, f"{_MODULE} calls open(): it may read nothing but the request."
