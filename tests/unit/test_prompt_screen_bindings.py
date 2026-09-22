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

import re
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


# ---------------------------------------------------------------------------
# `prompts.ai_diplomatic_approach`'s verification, after the turn-42 live negative
# ---------------------------------------------------------------------------

_GREETING_ACCEPT = "Would you like to visit our nearby city and sample our hospitality?"
_GREETING_DECLINE = "Thanks for the introduction, but we have no time for further pleasantries."


def _verifies(registry: CapabilityRegistry, *, target: str, after: dict[str, Any]) -> bool:
    """The real catalog predicate for the approach, through the real evaluator, against the
    post-execution observation the run loop re-assembles."""
    from civsim_harness.act.predicates import build_predicate_bindings, evaluate_predicate

    declaration = registry.resolve(DeclarationId("prompts.ai_diplomatic_approach"))
    assert declaration.verification_predicate is not None
    bindings = build_predicate_bindings(observation=_observation(after), target=target)
    return evaluate_predicate(declaration.verification_predicate, bindings)


def _conversation(options: Sequence[str]) -> dict[str, Any]:
    return {
        "screen": "prompt.diplomatic_approach",
        "raw_screen_id": "DiplomacyActionView",
        "recognized": True,
        "has_blocking_prompt": bool(options),
        "prompt_options": list(options),
    }


def test_a_statement_answer_verifies_on_the_leader_having_replied(
    registry: CapabilityRegistry,
) -> None:
    """MEASURED 2026-09-21, 12:35 EDT: a correct answer leaves the conversation OPEN -- the leader
    replies and the session stays up until its own exit choice is taken. The old predicate asked
    for the conversation to be gone, so a landed answer could never verify."""
    assert (
        _verifies(registry, target=_GREETING_ACCEPT, after=_conversation(["Goodbye"])) is True
    )


def test_the_exit_answer_verifies_on_the_conversation_being_gone(
    registry: CapabilityRegistry,
) -> None:
    """Taking the exit ends the session: nothing is offered, so the chosen option is not either.
    One predicate covers both endings without pretending they are the same event."""
    world = {
        "screen": "world",
        "raw_screen_id": "InGame",
        "recognized": True,
        "has_blocking_prompt": False,
        "prompt_options": [],
    }
    assert _verifies(registry, target=_GREETING_DECLINE, after=world) is True


def test_an_answer_that_changed_nothing_still_fails_verification(
    registry: CapabilityRegistry,
) -> None:
    """MEASURED 2026-09-21: `prompt_options` was byte-identical at all 16 steps of
    `run-d0933ca8...` -- the swallowed answer this predicate exists to catch. Loosening the
    predicate must not lose that."""
    unchanged = _conversation([_GREETING_ACCEPT, _GREETING_DECLINE])
    assert _verifies(registry, target=_GREETING_ACCEPT, after=unchanged) is False


# ---------------------------------------------------------------------------
# The promptType the executor actually puts on the wire (MEASURED LIVE 2026-09-22)
# ---------------------------------------------------------------------------
#
# Everything above this line checks the screen -> action direction. Nothing checked the direction
# that is actually dispatched, action -> screen, and that is the direction that was wrong: the
# production `ActionExecutor` re-derived it inline with a prefix swap instead of calling
# `prompt_screen_for_declaration_id`, so `prompts.ai_diplomatic_approach` went out as the
# nonexistent `prompt.ai_diplomatic_approach` and `lua/ingame/screens.lua`'s first guard answered
# `{"reason": "unknown_prompt", "ok": false}`. The board could not clear the prompt, so
# `game.has_blocking_prompt` stayed true, so no run on it could end a turn.
#
# The suite was green throughout. `tests/unit/test_executor_host_click.py` is the one unit test
# that dispatches this declaration, and its Lua stub is
# `function CivSim_Screens_RespondToPrompt(promptType, optionId) return { ok = false } end` --
# it discards `promptType`, so any string at all passes. Hence the check below asserts on the
# string itself, against `screens.lua`'s own list rather than against a copy of it: a list copied
# into a test can drift with the thing it is meant to pin.

_KNOWN_SCREENS_RE = re.compile(
    r"CIVSIM_KNOWN_SCREENS\s*=\s*\{(.*?)\}", re.DOTALL
)


def _known_screens_from_lua() -> frozenset[str]:
    """The `CIVSIM_KNOWN_SCREENS` list, read out of `lua/ingame/screens.lua` itself."""
    source = (REPO_ROOT / "lua" / "ingame" / "screens.lua").read_text(encoding="utf-8")
    match = _KNOWN_SCREENS_RE.search(source)
    assert match is not None, "CIVSIM_KNOWN_SCREENS is not where this test expects it"
    screens = frozenset(re.findall(r'"([^"]+)"', match.group(1)))
    # A silently-empty parse would make every assertion below vacuously true.
    assert "prompt.diplomatic_approach" in screens
    return screens


def _dispatched_prompt_type(registry: CapabilityRegistry, action: str) -> str:
    """The first positional argument the production executor hands `CivSim_Screens.respond`."""
    from civsim_harness.act.executor import _build_arguments

    declaration_id = DeclarationId(action)
    arguments = _build_arguments(
        declaration_id=declaration_id,
        capability=registry.capability_for(declaration_id),
        parameters={},
        target="an option",
    )
    assert len(arguments) == 2, "respond(promptType, optionId) takes exactly two arguments"
    return str(arguments[0])


def test_the_greeting_dispatches_the_screen_id_the_lua_knows_not_the_declarations_own_name(
    registry: CapabilityRegistry,
) -> None:
    """The exact bug, pinned. `prompts.ai_diplomatic_approach` answers `prompt.diplomatic_approach`
    -- the catalog names the action for what the human does and the screen for what is on screen,
    and the prefix rule cannot bridge that. Before the fix this produced
    `prompt.ai_diplomatic_approach`, which live returned `{"reason": "unknown_prompt", "ok": false}`
    with no `prompt`/`option` echo -- the signature of `screens.lua`'s first guard, not of any
    later branch."""
    assert (
        _dispatched_prompt_type(registry, "prompts.ai_diplomatic_approach")
        == "prompt.diplomatic_approach"
    )


def test_every_prompt_action_dispatches_a_screen_id_screens_lua_actually_knows(
    registry: CapabilityRegistry,
) -> None:
    """The general form, so the next declaration whose name does not match its screen is caught
    when it is added rather than on a wedged live board. Asserted for every `prompts.*` action in
    the real shipped catalog, against `screens.lua`'s own `CIVSIM_KNOWN_SCREENS`."""
    known = _known_screens_from_lua()
    catalog = load_catalog(CATALOG_ROOT)
    actions = sorted(
        str(declaration_id)
        for declaration_id in catalog.declarations
        if str(declaration_id).startswith("prompts.")
    )
    assert actions, "no prompts.* declarations found -- the catalog did not load"
    unknown = {
        action: dispatched
        for action in actions
        if (dispatched := _dispatched_prompt_type(registry, action)) not in known
    }
    assert unknown == {}, (
        "these prompt actions dispatch a promptType lua/ingame/screens.lua does not know, so "
        f"CivSim_Screens.respond would answer 'unknown_prompt': {unknown}"
    )


def test_the_dispatched_screen_id_round_trips_back_to_the_same_action(
    registry: CapabilityRegistry,
) -> None:
    """The two directions must agree. They are one table now (`PROMPT_ACTION_BY_SCREEN`), and this
    is what says so -- the executor no longer owns a second derivation that could drift from it."""
    catalog = load_catalog(CATALOG_ROOT)
    for declaration_id in sorted(str(d) for d in catalog.declarations):
        if not declaration_id.startswith("prompts."):
            continue
        screen = _dispatched_prompt_type(registry, declaration_id)
        assert str(prompt_declaration_id_for_screen(screen)) == declaration_id


# ---------------------------------------------------------------------------
# Absence vs unobservability, carried from the client's value to the stall record
# ---------------------------------------------------------------------------
#
# The Lua half of this is in `tests/unit/test_screens_lua.py`; these are the Python half. What
# they pin is that the REASON survives the whole way: the probe's `screen_probe_reason` reaches
# `ScreenIdentityResult.unrecognized_reason` and then the `unknown_screen` event's detail, so an
# operator reading a stalled run can tell "a modal I cannot name is up" from "I could not read
# the popup stack at all". Those stall identically and need completely different fixes.


def test_an_unrecognised_screen_carries_the_clients_own_reason() -> None:
    """MEASURED 2026-09-22 ~17:15: the `HistoricMoments` card was showing and the probe answered
    `world` / `recognized: true`. It now answers unknown, and says which branch fired."""
    result = interpret_screen_state(
        {
            "screen": "unknown",
            "raw_screen_id": "HistoricMoments",
            "recognized": False,
            "has_blocking_prompt": False,
            "prompt_options": [],
            "screen_probe_reason": "unnamed_popup_showing",
            "popup_stack_depth": 1,
            "popup_stack_ids": ["HistoricMoments"],
        }
    )
    assert result.recognized is False
    assert result.unrecognized_reason == "unnamed_popup_showing"

    route = route_prompt(
        screen=result,
        run_id=RunId("run-1"),
        turn_number=70,
        step_index=3,
        occurred_at=datetime(2026, 9, 22, tzinfo=UTC),
    )
    assert route.status is PromptRouteStatus.unknown_screen
    assert route.event is not None
    assert route.event.detail["raw_screen_id"] == "HistoricMoments"
    assert route.event.detail["reason"] == "unnamed_popup_showing"


def test_a_recognised_screen_carries_no_reason() -> None:
    """The positive control, varying the dimension the rule constrains -- whether the probe could
    identify the board -- and nothing else. Same call, same shape, recognised board: no reason,
    no stall."""
    result = interpret_screen_state(
        {
            "screen": "world",
            "raw_screen_id": "InGame",
            "recognized": True,
            "has_blocking_prompt": False,
            "prompt_options": [],
            "popup_stack_depth": 0,
            "popup_stack_ids": [],
        }
    )
    assert result.recognized is True
    assert result.unrecognized_reason is None
    route = route_prompt(
        screen=result,
        run_id=RunId("run-1"),
        turn_number=70,
        step_index=3,
        occurred_at=datetime(2026, 9, 22, tzinfo=UTC),
    )
    assert route.status is PromptRouteStatus.no_prompt


def test_an_unreadable_popup_stack_is_not_recorded_as_an_empty_board() -> None:
    """The spine rule at the Python boundary. `popup_stack_unreadable` and a quiet world both
    produce "no blocking prompt"; only one of them may authorise actions, and the record has to
    keep them apart."""
    result = interpret_screen_state(
        {
            "screen": "unknown",
            "raw_screen_id": "InGame",
            "recognized": False,
            "has_blocking_prompt": False,
            "prompt_options": [],
            "screen_probe_reason": "popup_stack_unreadable",
            "screen_probe_error": "attempt to call a nil value",
        }
    )
    assert result.recognized is False
    assert result.unrecognized_reason == "popup_stack_unreadable"


def test_an_unrecognised_screen_with_no_stated_reason_says_so_rather_than_inventing_one() -> None:
    """A stall the client did not explain must read as unexplained. Defaulting the field to a
    plausible string is how 131 withheld captures came to look accounted for -- the same shape,
    in the capture writer, found the same day."""
    result = interpret_screen_state(
        {
            "screen": "unknown",
            "raw_screen_id": "SomeScreen",
            "recognized": False,
            "has_blocking_prompt": False,
        }
    )
    assert result.unrecognized_reason is None
    route = route_prompt(
        screen=result,
        run_id=RunId("run-1"),
        turn_number=1,
        step_index=0,
        occurred_at=datetime(2026, 9, 22, tzinfo=UTC),
    )
    assert route.event is not None
    assert route.event.detail["reason"] == "no_reason_reported_by_client"


def test_recognised_true_alongside_a_probe_reason_resolves_toward_the_stall() -> None:
    """The two readings of that pair are not equally safe: one authorises actions against a board
    and the other stalls it. A producer that emits both is believed in the safe direction, and
    both strings are kept so the drift is visible rather than silently corrected."""
    result = interpret_screen_state(
        {
            "screen": "world",
            "raw_screen_id": "InGame",
            "recognized": True,
            "has_blocking_prompt": False,
            "prompt_options": [],
            "screen_probe_reason": "popup_stack_unreadable",
        }
    )
    assert result.recognized is False
    assert result.unrecognized_reason == "recognized_with_probe_reason:popup_stack_unreadable"
