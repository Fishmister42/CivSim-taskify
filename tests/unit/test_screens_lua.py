"""`lua/ingame/screens.lua` executed for real, with the game's globals stubbed.

The harness ships Lua it cannot run headlessly, and until T253 the only check on a Lua edit short
of a live client was the executor's regex over its dispatch table. This module runs the file in an
embedded Lua 5.4 (`lupa`) with `ContextPtr`, `UIManager` and a control tree stubbed to the shape
Firaxis's own UI scripts use, so what is asserted is the file's *logic* -- which screen id a set of
open states resolves to, how a `DiplomacyActionView` MODE is read out of its control visibility,
which options are offered, which call dismisses a popup or answers a statement, and what comes back
when a call fails. What it cannot assert is that the real client exposes `CallCallback` on a
control obtained from another state, or that `UIManager:DequeuePopup` accepts another state's
context from InGame; both stay UNVERIFIED LIVE and are said so in the Lua.

Skipped, not failed, where `lupa` is absent: it is not a project dependency. Run with
``uv run --with lupa pytest tests/unit/test_screens_lua.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

lupa = pytest.importorskip("lupa")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCREENS_LUA = REPO_ROOT / "lua" / "ingame" / "screens.lua"

# Stubs for the Firaxis globals the file touches. `LookUpControl("/InGame/<State>")` answers a
# control for each state in `open_states`/`hidden_states` and nil otherwise, exactly like the live
# client does for a `<LuaContext>` ID that is not present. Each popup state also gets its own close
# control at `/InGame/<State>/<Control>`, named as the shipped XML names it, whose `CallCallback`
# stands in for the real button's callback running inside the popup's own Lua state.
# `UIManager:DequeuePopup` records the call and, unless scripted to fail, hides the control.
_STUBS = """
local M = {}
M.dequeued = {}
M.clicked = {}
M.set_hidden = {}
M.fail_dequeue = false
M.callback_closes = true
M.callback_errors = false
M.controls = {}

-- The close control each acknowledge-only popup's own button carries, from the shipped XML:
-- techciviccompletedpopup.xml (CloseButton), boostunlockedpopup.xml (ContinueButton) and
-- civ6_styles.xml's ModalScreen template, which greatworkshowcase.xml includes (ModalScreenClose).
local CLOSE_CONTROLS = {
    TechCivicCompletedPopup = "CloseButton",
    BoostUnlockedPopup = "ContinueButton",
    GreatWorkShowcase = "ModalScreenClose",
}

local function make_control(name, hidden)
    local c = { name = name, hidden = hidden }
    function c:IsHidden() return self.hidden end
    function c:SetHide(value)
        M.set_hidden[#M.set_hidden + 1] = self.name
        self.hidden = value
    end
    -- The three Forge control methods the diplomacy mode probe uses. A control with no text and
    -- no children answers nil for both, exactly as a plain container does.
    function c:GetText() return self.text end
    function c:GetChildren() return self.children end
    function c:IsDisabled() return self.disabled == true end
    return c
end

local function make_button(stateName, controlName, ctx)
    local b = { name = controlName }
    function b:CallCallback(eventType)
        if M.callback_errors then error("stubbed CallCallback failure") end
        M.clicked[#M.clicked + 1] = stateName .. "/" .. controlName
        if M.callback_closes then ctx.hidden = true end
    end
    return b
end

function M.reset(open_states, hidden_states)
    M.dequeued = {}
    M.clicked = {}
    M.set_hidden = {}
    M.fail_dequeue = false
    M.callback_closes = true
    M.callback_errors = false
    M.diplomacy_closes = true
    M.controls = {}
    for _, s in ipairs(open_states) do M.controls["/InGame/" .. s] = make_control(s, false) end
    for _, s in ipairs(hidden_states) do M.controls["/InGame/" .. s] = make_control(s, true) end
    for state, control in pairs(CLOSE_CONTROLS) do
        local ctx = M.controls["/InGame/" .. state]
        if ctx ~= nil then
            M.controls["/InGame/" .. state .. "/" .. control] = make_button(state, control, ctx)
        end
    end
end

-- DiplomacyActionView's conversation mode: the container visible, and one `SelectionButton`
-- instance per choice in `ConversationSelectionStack`, each with its `SelectionText` label inside
-- it -- the tree diplomacyactionview.xml declares and `ApplyStatement` fills.
local DIPLO = "/InGame/DiplomacyActionView"

local function contains(list, value)
    if list == nil then return false end
    for _, item in ipairs(list) do if item == value then return true end end
    return false
end

function M.set_diplomacy_conversation(texts, disabled_texts, hidden_texts)
    local container = make_control("ConversationContainer", false)
    M.controls[DIPLO .. "/ConversationContainer"] = container
    local stack = make_control("ConversationSelectionStack", false)
    local kids = {}
    for _, text in ipairs(texts) do
        local label = make_control("SelectionText", false)
        label.text = text
        local button = make_control("SelectionButton", contains(hidden_texts, text))
        button.disabled = contains(disabled_texts, text)
        button.children = { label }
        function button:CallCallback(eventType)
            if M.callback_errors then error("stubbed CallCallback failure") end
            M.clicked[#M.clicked + 1] = "DiplomacyActionView/" .. text
            if M.diplomacy_closes then container.hidden = true end
        end
        kids[#kids + 1] = button
    end
    stack.children = kids
    M.controls[DIPLO .. "/ConversationSelectionStack"] = stack
end

-- Overview mode: the same context open, its conversation container hidden.
function M.set_diplomacy_overview()
    M.controls[DIPLO .. "/ConversationContainer"] = make_control("ConversationContainer", true)
    local stack = make_control("ConversationSelectionStack", true)
    M.controls[DIPLO .. "/ConversationSelectionStack"] = stack
end

-- A build whose controls carry no CallCallback at all: every close control becomes uncallable.
function M.drop_call_callback()
    for _, c in pairs(M.controls) do
        if c.CallCallback ~= nil then c.CallCallback = nil end
    end
end

-- A popup whose close control cannot be resolved by name from InGame.
function M.drop_close_controls()
    for state, control in pairs(CLOSE_CONTROLS) do
        M.controls["/InGame/" .. state .. "/" .. control] = nil
    end
end

Mouse = { eLClick = 1 }

ContextPtr = {}
function ContextPtr:LookUpControl(path) return M.controls[path] end

UIManager = {}
function UIManager:DequeuePopup(ctx)
    if M.fail_dequeue then error("stubbed DequeuePopup failure") end
    M.dequeued[#M.dequeued + 1] = ctx.name
    ctx.hidden = true
end

return M
"""


@pytest.fixture
def lua() -> tuple[Any, Any]:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    stubs = runtime.execute(_STUBS)
    runtime.execute(SCREENS_LUA.read_text(encoding="utf-8"))
    return runtime, stubs


def _state(runtime: Any, stubs: Any, *, open: list[str], hidden: list[str] = ()) -> dict[str, Any]:
    stubs.reset(runtime.table(*open), runtime.table(*hidden))
    result = runtime.globals()["CivSim_Screens"]["probe"]()
    return {k: (list(v.values()) if k == "prompt_options" else v) for k, v in result.items()}


def test_the_plain_world_view_is_recognised_with_no_prompt(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    state = _state(runtime, stubs, open=[], hidden=["TechCivicCompletedPopup", "CityPanel"])
    assert state["screen"] == "world"
    assert state["recognized"] is True
    assert state["has_blocking_prompt"] is False
    assert state["prompt_options"] == []


@pytest.mark.parametrize(
    ("state_name", "screen_id"),
    [
        ("TechCivicCompletedPopup", "prompt.tech_civic_completed"),
        ("BoostUnlockedPopup", "prompt.boost_unlocked"),
    ],
)
def test_an_acknowledge_only_popup_is_a_recognised_blocking_prompt_offering_continue(
    lua: tuple[Any, Any], state_name: str, screen_id: str
) -> None:
    """MEASURED 2026-09-21 (attempt 5): this exact state was open and the probe answered
    `unknown` -> the run stalled. It now names the prompt and its one option."""
    runtime, stubs = lua
    state = _state(runtime, stubs, open=[state_name], hidden=["CityPanel"])
    assert state["screen"] == screen_id
    assert state["raw_screen_id"] == state_name
    assert state["recognized"] is True
    assert state["has_blocking_prompt"] is True
    assert state["prompt_options"] == ["continue"]


def test_a_recognised_prompt_outranks_an_open_panel_but_offers_no_options_yet(
    lua: tuple[Any, Any],
) -> None:
    """Every other prompt family still reports an empty option list (per-prompt enumeration is
    not implemented), so those actions stay unavailable rather than guessed at."""
    runtime, stubs = lua
    state = _state(runtime, stubs, open=["CityPanel", "EraCompletePopup"])
    assert state["screen"] == "prompt.era_transition"
    assert state["has_blocking_prompt"] is True
    assert state["prompt_options"] == []


def test_the_great_work_showcase_is_watched_and_names_its_prompt(lua: tuple[Any, Any]) -> None:
    """MEASURED 2026-09-21 (block 3, turn 27): a relic from a tribal village raised this showcase,
    the probe answered `world` because nothing watched the state, and every move issued underneath
    it failed verification. UNVERIFIED LIVE that the state reports hidden=false for it."""
    runtime, stubs = lua
    state = _state(runtime, stubs, open=["GreatWorkShowcase"], hidden=["CityPanel"])
    assert state["screen"] == "prompt.great_work_created"
    assert state["raw_screen_id"] == "GreatWorkShowcase"
    assert state["has_blocking_prompt"] is True
    assert state["prompt_options"] == ["continue"]


@pytest.mark.parametrize(
    ("state_name", "expected"),
    [
        # Deliberately NOT mapped: each of these contexts serves the browse/overview case too, so
        # `IsHidden()==false` on it cannot mean "a blocking prompt of this family is up". See
        # lua/ingame/screens.lua's CIVSIM_SCREEN_ID_BY_STATE comment and
        # specs/002-civ-playing-harness/spikes/screens-unmapped-2026-09-21.md.
        # `prompt.religion_selection` was retired outright on 2026-09-21 (catalogs/README.md §6):
        # opening the religion screen is not a blocking prompt, and its real interactions are
        # already claimed by catalogs/actions/religion.yaml. An open ReligionScreen is `unknown`.
        ("ReligionScreen", "unknown"),
        # DiplomacyActionView with no conversation controls resolvable: the ordinary screen.
        ("DiplomacyActionView", "diplomacy"),
        ("WorldCongressPopup", "congress"),  # not prompt.congress_vote
    ],
)
def test_a_dual_purpose_screen_is_never_reported_as_a_blocking_prompt(
    lua: tuple[Any, Any], state_name: str, expected: str
) -> None:
    runtime, stubs = lua
    state = _state(runtime, stubs, open=[state_name])
    assert state["screen"] == expected
    assert state["has_blocking_prompt"] is False


def test_the_strategic_view_context_is_not_watched_so_it_cannot_claim_a_screen(
    lua: tuple[Any, Any],
) -> None:
    """`StrategicView` has no Hidden attribute in any shipped ingame.xml and strategicview.lua has
    no show/hide logic, so it reads open at the ordinary world view. Watching it would make the
    probe answer `strategic` always; it is deliberately absent from the watchlist."""
    runtime, stubs = lua
    state = _state(runtime, stubs, open=["StrategicView"])
    assert state["screen"] == "world"
    assert state["has_blocking_prompt"] is False


@pytest.mark.parametrize(
    ("control_id", "lua_state_name"),
    [("Civilopedia", "CivilopediaScreen"), ("TopOptionsMenu", "InGameTopOptionsMenu")],
)
def test_the_two_contexts_whose_id_differs_from_their_state_name_are_watched_by_id(
    lua: tuple[Any, Any], control_id: str, lua_state_name: str
) -> None:
    """`/InGame/<name>` resolves a `<LuaContext>`'s ID, not the Lua state's name (its FileName).
    For these two they differ, so the old watchlist entries could never resolve."""
    runtime, stubs = lua
    assert _state(runtime, stubs, open=[control_id])["raw_screen_id"] == control_id
    assert _state(runtime, stubs, open=[lua_state_name])["screen"] == "world"


def _diplomacy(
    runtime: Any,
    stubs: Any,
    *,
    mode: str,
    texts: list[str] = (),
    disabled: list[str] = (),
    hidden: list[str] = (),
) -> dict[str, Any]:
    """Open DiplomacyActionView in `mode` and probe. `conversation` builds the choice stack."""
    stubs.reset(runtime.table("DiplomacyActionView"), runtime.table())
    if mode == "conversation":
        stubs.set_diplomacy_conversation(
            runtime.table(*texts), runtime.table(*disabled), runtime.table(*hidden)
        )
    else:
        stubs.set_diplomacy_overview()
    result = runtime.globals()["CivSim_Screens"]["probe"]()
    return {k: (list(v.values()) if k == "prompt_options" else v) for k, v in result.items()}


_GREETING = [
    "Perhaps you would like to visit our nearby city.",
    "I have no time for further pleasantries.",
]


def test_a_leader_statement_awaiting_a_choice_is_a_blocking_prompt(
    lua: tuple[Any, Any],
) -> None:
    """MEASURED 2026-09-21 (block 7, game turn 35): play stalled five harness turns on Australia's
    first-meeting leader scene -- nine send_delegation orders refused, four end turns never
    confirmed -- because the probe read the STATE (`diplomacy`, not blocking) and not the MODE.
    The options are the visible choice labels, which is what a human reads."""
    runtime, stubs = lua
    state = _diplomacy(runtime, stubs, mode="conversation", texts=_GREETING)
    assert state["screen"] == "prompt.diplomatic_approach"
    assert state["raw_screen_id"] == "DiplomacyActionView"
    assert state["recognized"] is True
    assert state["has_blocking_prompt"] is True
    assert state["prompt_options"] == _GREETING


def test_the_same_context_in_overview_mode_is_the_ordinary_diplomacy_screen(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    state = _diplomacy(runtime, stubs, mode="overview")
    assert state["screen"] == "diplomacy"
    assert state["has_blocking_prompt"] is False
    assert state["prompt_options"] == []


def test_choices_a_human_cannot_click_are_never_offered(lua: tuple[Any, Any]) -> None:
    """A disabled choice is visible to the human but not takeable, and a hidden one is a recycled
    instance manager slot. Offering either would be a superset of what the human can do."""
    runtime, stubs = lua
    state = _diplomacy(
        runtime,
        stubs,
        mode="conversation",
        texts=[*_GREETING, "Declare war (not enough gold)", "A recycled slot"],
        disabled=["Declare war (not enough gold)"],
        hidden=["A recycled slot"],
    )
    assert state["prompt_options"] == _GREETING


def test_conversation_mode_with_nothing_takeable_is_not_a_prompt(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    state = _diplomacy(
        runtime, stubs, mode="conversation", texts=["Only option"], disabled=["Only option"]
    )
    assert state["screen"] == "diplomacy"
    assert state["has_blocking_prompt"] is False


def test_answering_the_approach_clicks_the_matching_choice_button(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=_GREETING)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GREETING[1])
    )
    assert result["ok"] is True
    assert result["mechanism"] == "selection_button_callback"
    assert result["still_in_conversation"] is False
    assert list(stubs.clicked.values()) == [f"DiplomacyActionView/{_GREETING[1]}"]


def test_an_option_that_is_not_offered_is_refused_and_says_what_was(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=_GREETING)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", "Declare war")
    )
    assert result["ok"] is False
    assert result["reason"] == "option_not_offered"
    assert list(result["offered"].values()) == _GREETING
    assert list(stubs.clicked.values()) == []


def test_answering_outside_conversation_mode_is_refused(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="overview")
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GREETING[0])
    )
    assert result["ok"] is False
    assert result["reason"] == "not_in_conversation_mode"


def test_a_failing_choice_click_fails_rather_than_guessing_at_a_diplomacy_call(
    lua: tuple[Any, Any],
) -> None:
    """There is deliberately no fallback: reconstructing `DiplomacyManager.AddStatement` from a
    label would risk answering something other than what the agent chose."""
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=_GREETING)
    stubs.callback_errors = True
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GREETING[0])
    )
    assert result["ok"] is False
    assert "stubbed CallCallback failure" in result["error"]


def test_an_unmapped_watchlist_state_is_still_unknown_never_guessed(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    state = _state(runtime, stubs, open=["NaturalWonderPopup"])
    assert state["screen"] == "unknown"
    assert state["recognized"] is False
    assert state["has_blocking_prompt"] is False


@pytest.mark.parametrize(
    ("screen_id", "state_name", "control"),
    [
        ("prompt.tech_civic_completed", "TechCivicCompletedPopup", "CloseButton"),
        ("prompt.boost_unlocked", "BoostUnlockedPopup", "ContinueButton"),
        ("prompt.great_work_created", "GreatWorkShowcase", "ModalScreenClose"),
    ],
)
def test_acknowledging_drives_the_popups_own_close_control_first(
    lua: tuple[Any, Any], screen_id: str, state_name: str, control: str
) -> None:
    """MEASURED 2026-09-21 (block 4, turn 30): dequeuing without running the popup's own handler
    left its private queue holding the already-acknowledged card, which then reappeared. The
    acknowledge now clicks the control the human clicks, so that handler runs in the popup's own
    state, and neither fallback is reached."""
    runtime, stubs = lua
    stubs.reset(runtime.table(state_name), runtime.table())
    result = dict(runtime.globals()["CivSim_Screens"]["respond"](screen_id, "continue"))
    assert result["ok"] is True
    assert result["mechanism"] == "close_control_callback"
    assert result["close_control"] == control
    assert result["hidden_after"] is True
    assert list(stubs.clicked.values()) == [f"{state_name}/{control}"]
    assert list(stubs.dequeued.values()) == []
    assert list(stubs.set_hidden.values()) == []


@pytest.mark.parametrize(
    ("screen_id", "state_name", "mechanism"),
    [
        ("prompt.tech_civic_completed", "TechCivicCompletedPopup", "UIManager:DequeuePopup"),
        ("prompt.boost_unlocked", "BoostUnlockedPopup", "UIManager:DequeuePopup"),
        # GreatWorkShowcase is never queued in UIManager -- its own close handler is
        # `ContextPtr:SetHide(true)` (greatworkshowcase.lua HideScreen), so DequeuePopup would be
        # the wrong call for it and the fallback is per-popup, not one call for all three.
        ("prompt.great_work_created", "GreatWorkShowcase", "ContextPtr:SetHide"),
    ],
)
def test_a_build_without_callcallback_falls_back_to_that_popups_own_primitive(
    lua: tuple[Any, Any], screen_id: str, state_name: str, mechanism: str
) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table(state_name), runtime.table())
    stubs.drop_call_callback()
    result = dict(runtime.globals()["CivSim_Screens"]["respond"](screen_id, "continue"))
    assert result["ok"] is True
    assert result["mechanism"] == mechanism
    assert result["fallback_reason"] == "close_control_uncallable"
    assert result["hidden_after"] is True
    if mechanism == "UIManager:DequeuePopup":
        assert list(stubs.dequeued.values()) == [state_name]
    else:
        assert list(stubs.set_hidden.values()) == [state_name]


def test_a_close_control_that_cannot_be_resolved_records_why_before_falling_back(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table("TechCivicCompletedPopup"), runtime.table())
    stubs.drop_close_controls()
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.tech_civic_completed", "continue")
    )
    assert result["ok"] is True
    assert result["fallback_reason"] == "close_control_absent"
    assert result["mechanism"] == "UIManager:DequeuePopup"
    assert list(stubs.clicked.values()) == []


def test_a_close_control_that_leaves_the_popup_open_stops_rather_than_dequeuing(
    lua: tuple[Any, Any],
) -> None:
    """A queued popup whose own handler shows the NEXT card leaves the context visible. That is
    what a human sees after clicking Continue, so dequeuing on top of it would destroy a card the
    human would have been shown -- the acknowledge stops and says the popup is still up."""
    runtime, stubs = lua
    stubs.reset(runtime.table("TechCivicCompletedPopup"), runtime.table())
    stubs.callback_closes = False
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.tech_civic_completed", "continue")
    )
    assert list(stubs.clicked.values()) == ["TechCivicCompletedPopup/CloseButton"]
    assert result["ok"] is True
    assert result["mechanism"] == "close_control_callback"
    assert result["hidden_after"] is False
    assert result["note"] == "still_open_next_queued_card_likely_shown"
    assert list(stubs.dequeued.values()) == []


def test_a_failing_dequeue_is_returned_as_itself_with_the_error(lua: tuple[Any, Any]) -> None:
    """The live half is unverified, so a failure must come back as a fact the record can carry,
    not as `ok` -- the verification predicate then reports the popup still open."""
    runtime, stubs = lua
    stubs.reset(runtime.table("BoostUnlockedPopup"), runtime.table())
    stubs.drop_call_callback()
    stubs.fail_dequeue = True
    respond = runtime.globals()["CivSim_Screens"]["respond"]
    result = dict(respond("prompt.boost_unlocked", "continue"))
    assert result["ok"] is False
    assert result["hidden_after"] is False
    assert "stubbed DequeuePopup failure" in result["error"]
    assert list(stubs.dequeued.values()) == []


def test_a_failing_close_control_callback_is_recorded_and_does_not_stop_the_fallback(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table("BoostUnlockedPopup"), runtime.table())
    stubs.callback_errors = True
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.boost_unlocked", "continue")
    )
    assert result["fallback_reason"] == "close_control_uncallable"
    assert "stubbed CallCallback failure" in result["close_control_error"]
    assert result["ok"] is True
    assert list(stubs.dequeued.values()) == ["BoostUnlockedPopup"]


@pytest.mark.parametrize(
    ("open", "option", "reason"),
    [
        (["TechCivicCompletedPopup"], "change_government", "unknown_option"),
        ([], "continue", "popup_state_absent"),
    ],
)
def test_a_wrong_option_or_an_absent_popup_refuses_without_touching_the_ui(
    lua: tuple[Any, Any], open: list[str], option: str, reason: str
) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table(*open), runtime.table())
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.tech_civic_completed", option)
    )
    assert result["ok"] is False
    assert result["reason"] == reason
    assert list(stubs.dequeued.values()) == []


def test_a_popup_that_is_not_open_is_not_dequeued(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table(), runtime.table("TechCivicCompletedPopup"))
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.tech_civic_completed", "continue")
    )
    assert result["ok"] is False
    assert result["reason"] == "popup_not_open"
    assert list(stubs.dequeued.values()) == []


def test_an_unknown_prompt_family_is_refused_before_anything_is_called(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table(), runtime.table())
    result = dict(runtime.globals()["CivSim_Screens"]["respond"]("prompt.does_not_exist", "x"))
    assert result["ok"] is False
    assert result["reason"] == "unknown_prompt"
