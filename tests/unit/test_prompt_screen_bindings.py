"""The three prompt screens mapped on 2026-09-21, bound end to end on the Python side.

`lua/ingame/screens.lua` can now report `prompt.era_dedication`, `prompt.congress_intro` and
`prompt.congress_vote`, but a screen id the probe can name is only half of an answerable prompt.
The other half is Python: `act/prompts.py` has to derive the action id from the screen id,
`capability/registry.py` has to resolve that id in the real shipped catalog, `act/dispatch.py` has
to find the action available against an observation that carries the prompt, and
`capability/executor.py` has to resolve it to `CivSim_Screens.respond`. MEASURED 2026-09-21
(gameplay blocks 11 and 12): a derived id that was not registered paused the run on `CatalogError`
before any model call, which is the exact failure this module exists to catch at test time.

Everything here drives the real `catalogs/` tree. Nothing constructs a store, a host adapter or a
game.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.act.dispatch import DispatchStatus, dispatch_action
from civsim_harness.act.prompts import (
    PromptRouteStatus,
    prompt_declaration_id_for_screen,
    prompt_screen_for_declaration_id,
    route_prompt,
)
from civsim_harness.capability.loader import load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.models.catalog import TargetKind
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ObservationId,
    RunId,
)
from civsim_harness.models.decision import RejectionReason
from civsim_harness.models.turn import Observation, ObservationEntry
from civsim_harness.observe.screen_identity import interpret_screen_state

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"

#: screen id -> the action that answers it, and one option a human could pick there.
_NEW_PROMPTS: Sequence[tuple[str, str, str]] = (
    ("prompt.era_dedication", "prompts.era_dedication", "MONUMENTALITY"),
    ("prompt.congress_intro", "prompts.congress_intro", "accept"),
    ("prompt.congress_vote", "prompts.congress_vote", "next"),
)


@pytest.fixture(scope="module")
def registry() -> CapabilityRegistry:
    return CapabilityRegistry(catalog=load_catalog(CATALOG_ROOT))


def _screen_state(screen: str, options: Sequence[str], **extra: Any) -> dict[str, Any]:
    return {
        "screen": screen,
        "raw_screen_id": "DedicationPopup",
        "recognized": True,
        "has_blocking_prompt": True,
        "prompt_options": list(options),
        **extra,
    }


def _observation(screen_state: dict[str, Any]) -> Observation:
    return Observation(
        observation_id=ObservationId("obs-1"),
        decision_step_id=DecisionStepId("step-1"),
        assembled_at=datetime(2026, 9, 21, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="2026.09.8", content_hash="deadbeef"),
        entries=[
            ObservationEntry(
                declaration_id=DeclarationId("game.screen_state"),
                key="game.screen_state",
                value=screen_state,
                context=LuaContext.IN_GAME,
            )
        ],
        screen_identity=screen_state["screen"],
    )


@pytest.mark.parametrize(("screen", "action", "option"), _NEW_PROMPTS)
def test_the_screen_id_derives_its_action_id_by_the_catalogs_own_convention(
    screen: str, action: str, option: str
) -> None:
    """`prompt.<family>` answers to `prompts.<family>`; none of the three needs an exception
    entry in `PROMPT_ACTION_BY_SCREEN` the way `prompt.diplomatic_approach` does."""
    assert str(prompt_declaration_id_for_screen(screen)) == action
    assert prompt_screen_for_declaration_id(DeclarationId(action)) == screen


@pytest.mark.parametrize(("screen", "action", "option"), _NEW_PROMPTS)
def test_the_derived_action_exists_in_the_real_catalog_and_takes_an_option(
    registry: CapabilityRegistry, screen: str, action: str, option: str
) -> None:
    declaration = registry.resolve(DeclarationId(action))
    assert declaration.context is LuaContext.IN_GAME
    assert str(declaration.capability_id) == "prompts.orders"
    assert declaration.target_kind is TargetKind.OPTION


@pytest.mark.parametrize(("screen", "action", "option"), _NEW_PROMPTS)
def test_routing_the_prompt_hands_the_loop_that_action_and_its_options(
    registry: CapabilityRegistry, screen: str, action: str, option: str
) -> None:
    route = route_prompt(
        screen=interpret_screen_state(_screen_state(screen, [option])),
        run_id=RunId("run-1"),
        turn_number=57,
        step_index=0,
        occurred_at=datetime(2026, 9, 21, tzinfo=UTC),
        registry=registry,
    )
    assert route.status is PromptRouteStatus.prompt_decision
    assert str(route.action_declaration_id) == action
    assert route.prompt_type == screen
    assert route.options == (option,)


@pytest.mark.parametrize(("screen", "action", "option"), _NEW_PROMPTS)
def test_the_action_is_available_for_an_offered_option_and_only_then(
    registry: CapabilityRegistry, screen: str, action: str, option: str
) -> None:
    observation = _observation(_screen_state(screen, [option]))
    authorized = dispatch_action(
        registry=registry,
        context=LuaContext.IN_GAME,
        action_declaration_id=DeclarationId(action),
        observation=observation,
        target=option,
    )
    assert authorized.status is DispatchStatus.authorized

    # An option the screen is not offering is not a button on it -- refused before anything runs.
    refused = dispatch_action(
        registry=registry,
        context=LuaContext.IN_GAME,
        action_declaration_id=DeclarationId(action),
        observation=observation,
        target="something the screen never showed",
    )
    assert refused.status is DispatchStatus.rejected
    assert refused.rejection_reason is RejectionReason.UNAVAILABLE_TO_HUMAN_NOW


@pytest.mark.parametrize(("screen", "action", "option"), _NEW_PROMPTS)
def test_the_action_is_unavailable_while_a_different_screen_is_up(
    registry: CapabilityRegistry, screen: str, action: str, option: str
) -> None:
    quiet_world = {
        "screen": "world",
        "raw_screen_id": "InGame",
        "recognized": True,
        "has_blocking_prompt": False,
        "prompt_options": [],
    }
    outcome = dispatch_action(
        registry=registry,
        context=LuaContext.IN_GAME,
        action_declaration_id=DeclarationId(action),
        observation=_observation(quiet_world),
        target=option,
    )
    assert outcome.status is DispatchStatus.rejected
    assert outcome.rejection_reason is RejectionReason.UNAVAILABLE_TO_HUMAN_NOW


def test_an_open_congress_session_with_nothing_to_answer_routes_to_no_prompt() -> None:
    """MEASURED 2026-09-21 (game turns 56-57): an end turn advanced with the congress session on
    screen, its Next button greyed out and Submit not yet shown. `congress` is a screen, not a
    prompt, and nothing may route it as one."""
    route = route_prompt(
        screen=interpret_screen_state(
            {
                "screen": "congress",
                "raw_screen_id": "WorldCongressPopup",
                "recognized": True,
                "has_blocking_prompt": False,
                "prompt_options": [],
            }
        ),
        run_id=RunId("run-1"),
        turn_number=56,
        step_index=0,
        occurred_at=datetime(2026, 9, 21, tzinfo=UTC),
    )
    assert route.status is PromptRouteStatus.no_prompt


def test_the_dedication_chooser_carries_its_counts_into_the_rendered_observation() -> None:
    """`prompt_selections_allowed` / `_made` / `_selected_options` ride along in
    `game.screen_state`'s own value, which is what the agent is shown -- a half-made choice has to
    be distinguishable from a finished one."""
    value = _screen_state(
        "prompt.era_dedication",
        ["FREE INQUIRY", "MONUMENTALITY"],
        prompt_selections_allowed=2,
        prompt_selections_made=1,
        prompt_selected_options=["MONUMENTALITY"],
    )
    entry = _observation(value).entries[0]
    assert entry.value["prompt_selections_allowed"] == 2
    assert entry.value["prompt_selections_made"] == 1
    assert entry.value["prompt_selected_options"] == ["MONUMENTALITY"]
    # The interpreted result keeps the options a human can click; the counts stay on the raw value.
    assert interpret_screen_state(value).prompt_options == ("FREE INQUIRY", "MONUMENTALITY")


@pytest.mark.parametrize(("screen", "action", "option"), _NEW_PROMPTS)
def test_every_prompt_action_dispatches_to_the_one_generic_respond_function(
    registry: CapabilityRegistry, screen: str, action: str, option: str
) -> None:
    """`prompts.orders` is generic: every declaration on it answers through
    `CivSim_Screens.respond(promptType, optionId)`, not a function named after the declaration."""
    from civsim_harness.capability.executor import _resolve_function_key

    declaration = registry.resolve(DeclarationId(action))
    assert (
        _resolve_function_key(
            declaration_id=DeclarationId(action),
            capability_id=declaration.capability_id,
            table_keys=frozenset({"probe", "probe_own_state", "respond"}),
        )
        == "respond"
    )
