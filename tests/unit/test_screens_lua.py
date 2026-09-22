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
    NaturalDisasterPopup = "Close",
    EraReviewPopup = "Continue",
    -- worldcongressintro.xml:13 -- the one button on the "Begin Voting" welcome card.
    WorldCongressIntro = "AcceptButton",
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
    local b = { name = controlName, hidden = false }
    function b:IsHidden() return self.hidden end
    -- The two Forge accessors the acknowledge reads to hand the harness the button's own
    -- on-screen rectangle; `M.rect_errors` scripts a build where they are unavailable.
    function b:GetScreenOffset()
        if M.rect_errors then error("stubbed GetScreenOffset failure") end
        return 900, 40
    end
    function b:GetSizeVal()
        if M.rect_errors then error("stubbed GetSizeVal failure") end
        return 32, 32
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
    M.rect_errors = false
    M.fail_close_session = false
    M.fail_add_response = false
    M.commemorations_allowed = 1
    M.session_by_player = {}
    M.closed_sessions = {}
    M.responses = {}
    M.selection_rows = {}
    M.drop_selection_table = false
    M.reply_choices = nil
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
        -- The two Forge accessors the answer reads to hand the harness the button's own
        -- on-screen rectangle (diplomacyactionview.lua:1876 uses GetScreenOffset the same way).
        -- Buttons are stacked 60 px apart under the leader; `M.rect_errors` scripts a build
        -- where the accessor is unavailable from InGame.
        local index = #kids
        function button:GetScreenOffset()
            if M.rect_errors then error("stubbed GetScreenOffset failure") end
            return 1200, 700 + index * 60
        end
        function button:GetSizeVal()
            if M.rect_errors then error("stubbed GetSizeVal failure") end
            return 420, 45
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

-- A build whose controls cannot report their own rectangle from InGame: every close control's
-- click request becomes unbuildable, so the acknowledge falls back to the popup's primitive.
function M.drop_call_callback()
    M.rect_errors = true
end

-- A popup whose close control cannot be resolved by name from InGame.
function M.drop_close_controls()
    for state, control in pairs(CLOSE_CONTROLS) do
        M.controls["/InGame/" .. state .. "/" .. control] = nil
    end
end

-- The two Forge accessors the real-click path reads, on an arbitrary control.
local function attach_rect(control, x, y, w, h)
    function control:GetScreenOffset()
        if M.rect_errors then error("stubbed GetScreenOffset failure") end
        return x, y
    end
    function control:GetSizeVal()
        if M.rect_errors then error("stubbed GetSizeVal failure") end
        return w, h
    end
    return control
end

-- The Gathering Storm dedication chooser's tree, exactly as dedicationpopup.xml:37-50 declares a
-- `Commemoration` instance: the instance's ROOT control is the `SelectCheck` GridButton (the
-- instance manager is declared over that control name, dedicationpopup.lua:25), the icon sits in
-- an image frame inside it, and the `MomentCategory` label a human reads as the commemoration's
-- name sits two levels down, inside an unnamed `Stack`.
local DEDICATION = "/InGame/DedicationPopup"

function M.set_dedication(labels, selected, disabled, hidden, confirm_disabled)
    local stack = make_control("CommemorationsStack", false)
    local kids = {}
    for _, label in ipairs(labels) do
        local category = make_control("MomentCategory", false)
        category.text = label
        local bonuses = make_control("MomentBonuses", false)
        bonuses.text = "+2 to something, for this age."
        local details = make_control("CommemorationDetails", false)
        details.children = { category, bonuses }
        local icon = make_control("CommemorationIcon", false)
        local iconFrame = make_control("CommemorationIconFrame", false)
        iconFrame.children = { icon }
        local check = make_control("SelectCheck", contains(hidden, label))
        check.disabled = contains(disabled, label)
        check.selected = contains(selected, label)
        function check:IsSelected() return self.selected == true end
        check.children = { iconFrame, details }
        attach_rect(check, 300, 200 + #kids * 140, 640, 136)
        kids[#kids + 1] = check
    end
    stack.children = kids
    M.controls[DEDICATION .. "/CommemorationsStack"] = stack

    local confirm = make_control("Confirm", false)
    confirm.disabled = (confirm_disabled == true)
    attach_rect(confirm, 412, 700, 200, 41)
    M.controls[DEDICATION .. "/Confirm"] = confirm
end

-- The congress's phase buttons (worldcongresspopup.xml:119-123). `UpdateNavButtons`
-- (worldcongresspopup.lua:380-425) is what shows, hides and greys each one; the probe reads
-- exactly that.
local CONGRESS = "/InGame/WorldCongressPopup"
local CONGRESS_BUTTONS = { "NextButton", "AcceptButton", "PassButton" }

function M.set_congress_phase(live, greyed)
    for index, name in ipairs(CONGRESS_BUTTONS) do
        local visible = contains(live, name) or contains(greyed, name)
        local control = make_control(name, not visible)
        control.disabled = contains(greyed, name)
        attach_rect(control, 300 + index * 210, 720, 200, 41)
        M.controls[CONGRESS .. "/" .. name] = control
    end
end

Mouse = { eLClick = 1 }

-- `Game.GetEras():GetPlayerNumAllowedCommemorations(localPlayer)` -- the same call
-- dedicationpopup.lua:65/:162/:201 makes to decide when Confirm may light up.
M.commemorations_allowed = 1
Game = {}
function Game.GetLocalPlayer() return 0 end
function Game.GetEras()
    return {
        GetPlayerNumAllowedCommemorations = function(self, playerID)
            if M.commemorations_allowed == nil then error("stubbed GetEras failure") end
            return M.commemorations_allowed
        end,
    }
end

-- The game's own text for the conversation's exit choice. Every shipped `CHOICE_EXIT` selection
-- carries this one tag (base/assets/gameplay/data/diplomacystatements_*.xml), rendered "Goodbye"
-- in en_US (base/assets/text/en_us/diplomacystatements_common_text.xml:107-109).
M.locale = { LOC_DIPLO_CHOICE_EXIT = "Goodbye" }
Locale = {}
function Locale.Lookup(tag)
    if M.locale[tag] ~= nil then return M.locale[tag] end
    return tag
end

-- The gameplay database's own `DiplomacySelections` rows, read exactly as Firaxis' shared
-- statement code reads them (diplomacystatementsupport.lua:77-84): `DB.Query` returns a table of
-- rows with `Text` and `Key`, and `ApplyStatement` puts `Locale.Lookup(Text)` on the button.
-- `M.selection_rows` is a list of {text_tag, key}; `M.drop_selection_table` scripts a build where
-- the query is unavailable at all.
M.selection_rows = {}
M.drop_selection_table = false
DB = {}
function DB.Query(sql)
    if M.drop_selection_table then error("stubbed DB.Query failure") end
    local rows = {}
    for _, row in ipairs(M.selection_rows) do
        rows[#rows + 1] = { Text = row[1], Key = row[2] }
    end
    return rows
end

function M.set_selections(rows)
    -- rows: list of {label, key} -- the label is registered as its own text tag so
    -- `Locale.Lookup(tag)` renders it back, which is what the real table does one step removed.
    M.selection_rows = {}
    for _, row in ipairs(rows) do
        M.locale[row[1]] = row[1]
        M.selection_rows[#M.selection_rows + 1] = { row[1], row[2] }
    end
end

M.responses = {}


-- Open diplomacy sessions, found the way base/assets/ui/civ6common.lua:688-698 finds them.
Players = {}
M.session_by_player = {}
M.closed_sessions = {}
function M.set_open_session(playerIndex, sessionID)
    M.session_by_player[playerIndex] = sessionID
    Players[playerIndex] = { GetID = function(self) return playerIndex end }
end

DiplomacyManager = {}
function DiplomacyManager.FindOpenSessionID(localID, otherID)
    return M.session_by_player[otherID]
end
function DiplomacyManager.CloseSession(sessionID)
    if M.fail_close_session then error("stubbed CloseSession failure") end
    M.closed_sessions[#M.closed_sessions + 1] = sessionID
    -- The scene fades out: the conversation container stops being visible.
    local container = M.controls[DIPLO .. "/ConversationContainer"]
    if container ~= nil then container.hidden = true end
end
-- MEASURED 2026-09-21, 12:35 EDT: AddResponse answered the greeting and the session STAYED OPEN
-- with the leader's reply, so the stub replaces the offered choices rather than closing anything.
function DiplomacyManager.AddResponse(sessionID, playerID, response)
    if M.fail_add_response then error("stubbed AddResponse failure") end
    M.responses[#M.responses + 1] = { session_id = sessionID, response = response }
    if M.reply_choices ~= nil then
        M.set_diplomacy_conversation(M.reply_choices, {}, {})
    end
end

ContextPtr = {}
function ContextPtr:LookUpControl(path) return M.controls[path] end

UIManager = {}
-- The UI's own screen space (measured 1024x768 on a 1920x1200 window, 2026-09-21).
function UIManager:GetScreenSizeVal() return 1024, 768 end
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
    """Every prompt family that is not acknowledge-only still reports an empty option list
    (per-prompt enumeration is not implemented), so those actions stay unavailable rather than
    guessed at. The era card left this group on 2026-09-21 (block 18): acknowledge-only now."""
    runtime, stubs = lua
    state = _state(runtime, stubs, open=["CityPanel", "UnitPromotionPopup"])
    assert state["screen"] == "prompt.unit_promotion"
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


def test_answering_the_approach_hands_the_harness_the_matching_buttons_own_rect(
    lua: tuple[Any, Any],
) -> None:
    """Measured 2026-09-21 (blocks 13-14): `CallCallback` is not an engine API and clicked nothing.
    The answer now returns the chosen button's own on-screen rectangle and asks the harness to
    click its centre through the host input port -- the same button a human clicks. It never
    reports itself applied: `ok` stays false until the executor's click, and the action's
    verification predicate decides the rest."""
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=_GREETING)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GREETING[1])
    )
    assert result["ok"] is False
    assert result["reason"] == "requires_host_click"
    assert result["mechanism"] == "host_click_at_control_rect"
    assert result["option"] == _GREETING[1]
    rect = dict(result["click"])
    assert rect == {"x": 1200, "y": 760, "w": 420, "h": 45}  # the second stacked button
    # The UI's own screen size rides along so the harness can scale UI units onto the window.
    assert dict(result["ui_screen"]) == {"w": 1024, "h": 768}


def test_the_first_choice_gets_the_first_buttons_rect(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=_GREETING)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GREETING[0])
    )
    assert result["reason"] == "requires_host_click"
    assert dict(result["click"]) == {"x": 1200, "y": 700, "w": 420, "h": 45}


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


def test_an_unreadable_button_rect_fails_rather_than_guessing_at_a_diplomacy_call(
    lua: tuple[Any, Any],
) -> None:
    """There is deliberately no fallback: reconstructing `DiplomacyManager.AddStatement` from a
    label would risk answering something other than what the agent chose. A build whose control
    accessors are unavailable from InGame reports exactly that."""
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=_GREETING)
    stubs.rect_errors = True
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GREETING[0])
    )
    assert result["ok"] is False
    assert result["reason"] == "control_rect_unreadable"
    assert "stubbed GetScreenOffset failure" in result["error"]


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
    left its private queue holding the already-acknowledged card, which then reappeared. And
    (blocks 13-15) `CallCallback` fires nothing, so the acknowledge now hands the harness the close
    control's own rectangle for a real click; that handler then runs in the popup's own state.
    Neither fallback is reached, and the Lua never reports the popup dismissed itself."""
    runtime, stubs = lua
    stubs.reset(runtime.table(state_name), runtime.table())
    result = dict(runtime.globals()["CivSim_Screens"]["respond"](screen_id, "continue"))
    assert result["ok"] is False
    assert result["reason"] == "requires_host_click"
    assert result["mechanism"] == "host_click_at_control_rect"
    assert result["close_control"] == control
    assert dict(result["click"]) == {"x": 900, "y": 40, "w": 32, "h": 32}
    assert dict(result["ui_screen"]) == {"w": 1024, "h": 768}
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
        # NaturalDisasterPopup's own Close() is a SetHide plus a LuaEvent (naturaldisasterpopup.lua
        # :87-122), not a UIManager popup, so its fallback is SetHide too.
        ("prompt.natural_disaster", "NaturalDisasterPopup", "ContextPtr:SetHide"),
        # EraReviewPopup's Continue and Close both run UIManager:DequeuePopup (erareviewpopup.lua
        # :241-246), so its fallback is the dequeue.
        ("prompt.era_transition", "EraReviewPopup", "UIManager:DequeuePopup"),
    ],
)
def test_a_build_whose_controls_report_no_rect_falls_back_to_that_popups_own_primitive(
    lua: tuple[Any, Any], screen_id: str, state_name: str, mechanism: str
) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table(state_name), runtime.table())
    stubs.drop_call_callback()
    result = dict(runtime.globals()["CivSim_Screens"]["respond"](screen_id, "continue"))
    assert result["ok"] is True
    assert result["mechanism"] == mechanism
    assert result["fallback_reason"] == "close_control_rect_unreadable"
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


def test_handing_back_a_click_request_never_dequeues_or_hides(lua: tuple[Any, Any]) -> None:
    """A queued popup whose own handler shows the NEXT card leaves the context visible after the
    human's click. Dequeuing on top of that would destroy a card the human would have been shown,
    so the Lua stops at the click request: the executor clicks, the verification predicate reports
    whether the popup is still current, and the agent acknowledges again -- one click per card."""
    runtime, stubs = lua
    stubs.reset(runtime.table("TechCivicCompletedPopup"), runtime.table())
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.tech_civic_completed", "continue")
    )
    assert result["reason"] == "requires_host_click"
    assert result["ok"] is False
    assert "hidden_after" not in result
    assert list(stubs.dequeued.values()) == []
    assert list(stubs.set_hidden.values()) == []


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


def test_a_hidden_close_control_is_recorded_and_does_not_stop_the_fallback(
    lua: tuple[Any, Any],
) -> None:
    """A close control that exists but is hidden cannot be clicked by a human either; the
    acknowledge says so and falls back to the popup's own primitive rather than clicking air."""
    runtime, stubs = lua
    stubs.reset(runtime.table("BoostUnlockedPopup"), runtime.table())
    stubs.controls["/InGame/BoostUnlockedPopup/ContinueButton"].hidden = True
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.boost_unlocked", "continue")
    )
    assert result["fallback_reason"] == "close_control_hidden"
    assert result["ok"] is True
    assert list(stubs.dequeued.values()) == ["BoostUnlockedPopup"]


def test_the_natural_disaster_cinematic_is_a_recognised_blocking_prompt(
    lua: tuple[Any, Any],
) -> None:
    """MEASURED 2026-09-21 (game turn 42): a Gathering Storm eruption cinematic blocked play and
    quicksaves while the probe said `world`; `/InGame/NaturalDisasterPopup` was hidden=false."""
    runtime, stubs = lua
    state = _state(runtime, stubs, open=["NaturalDisasterPopup"], hidden=["CityPanel"])
    assert state["screen"] == "prompt.natural_disaster"
    assert state["recognized"] is True
    assert state["has_blocking_prompt"] is True
    assert state["prompt_options"] == ["continue"]


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


# ---------------------------------------------------------------------------
# The Gathering Storm era dedication chooser (`prompt.era_dedication`)
# ---------------------------------------------------------------------------

_COMMEMORATIONS = ["FREE INQUIRY", "MONUMENTALITY", "PEN, BRUSH AND VOICE"]


def _dedication(
    runtime: Any,
    stubs: Any,
    *,
    labels: list[str] = _COMMEMORATIONS,
    selected: list[str] = (),
    disabled: list[str] = (),
    hidden: list[str] = (),
    confirm_disabled: bool = True,
    allowed: int = 1,
    also_open: list[str] = (),
) -> None:
    """Open `DedicationPopup` with a fake commemoration stack in the shipped shape."""
    stubs.reset(runtime.table("DedicationPopup", *also_open), runtime.table())
    stubs.commemorations_allowed = allowed
    stubs.set_dedication(
        runtime.table(*labels),
        runtime.table(*selected),
        runtime.table(*disabled),
        runtime.table(*hidden),
        confirm_disabled,
    )


def _probe(runtime: Any) -> dict[str, Any]:
    result = runtime.globals()["CivSim_Screens"]["probe"]()
    return {
        k: (list(v.values()) if k in ("prompt_options", "prompt_selected_options") else v)
        for k, v in result.items()
    }


def test_the_dedication_chooser_is_a_blocking_prompt_offering_the_commemoration_labels(
    lua: tuple[Any, Any],
) -> None:
    """MEASURED 2026-09-21 (gameplay block 20, Classical era): the chooser was on screen for 41
    harness steps while the probe answered `world`, and the model -- reading the delivered frame --
    asked to answer it at 40 of them, refused every time because no id covered it. The options are
    the cards' own `MomentCategory` labels, which is what a human reads."""
    runtime, stubs = lua
    _dedication(runtime, stubs)
    state = _probe(runtime)
    assert state["screen"] == "prompt.era_dedication"
    assert state["raw_screen_id"] == "DedicationPopup"
    assert state["recognized"] is True
    assert state["has_blocking_prompt"] is True
    assert state["prompt_options"] == _COMMEMORATIONS


def test_the_chooser_reports_how_many_dedications_it_wants_and_which_are_ticked(
    lua: tuple[Any, Any],
) -> None:
    """A Golden or Heroic age allows more than one, and the popup keeps Confirm greyed out until
    exactly that many are ticked (dedicationpopup.lua:200-203). Reporting the count is what lets
    the agent tell a half-made choice from a finished one."""
    runtime, stubs = lua
    _dedication(runtime, stubs, selected=["MONUMENTALITY"], allowed=2)
    state = _probe(runtime)
    assert state["prompt_selections_allowed"] == 2
    assert state["prompt_selections_made"] == 1
    assert state["prompt_selected_options"] == ["MONUMENTALITY"]


def test_a_client_that_cannot_answer_the_allowance_still_reports_the_chooser(
    lua: tuple[Any, Any],
) -> None:
    """The allowance is informational -- what actually governs is Confirm's own disabled state,
    which is what the human goes by. An unavailable call must not cost the prompt its id."""
    runtime, stubs = lua
    _dedication(runtime, stubs)
    stubs.commemorations_allowed = None
    state = _probe(runtime)
    assert state["screen"] == "prompt.era_dedication"
    assert "prompt_selections_allowed" not in state
    assert state["prompt_selections_made"] == 0


def test_cards_a_human_cannot_click_are_never_offered_as_commemorations(
    lua: tuple[Any, Any],
) -> None:
    """A hidden card is a recycled instance-manager slot and a greyed one cannot be clicked;
    offering either would be a superset of what the human can do."""
    runtime, stubs = lua
    _dedication(
        runtime,
        stubs,
        labels=[*_COMMEMORATIONS, "A RECYCLED SLOT", "A GREYED CARD"],
        hidden=["A RECYCLED SLOT"],
        disabled=["A GREYED CARD"],
    )
    assert _probe(runtime)["prompt_options"] == _COMMEMORATIONS


def test_answering_the_chooser_first_clicks_the_named_cards_own_rect(lua: tuple[Any, Any]) -> None:
    """One click per step, as a human does: the card first. Nothing is dequeued -- the popup is
    left standing so the next probe sees whether the tick took."""
    runtime, stubs = lua
    _dedication(runtime, stubs)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.era_dedication", "MONUMENTALITY")
    )
    assert result["ok"] is False
    assert result["reason"] == "requires_host_click"
    assert result["mechanism"] == "host_click_at_control_rect"
    assert result["step"] == "select_option"
    assert dict(result["click"]) == {"x": 300, "y": 340, "w": 640, "h": 136}
    assert dict(result["ui_screen"]) == {"w": 1024, "h": 768}
    assert list(stubs.dequeued.values()) == []


def test_answering_again_once_the_card_is_ticked_clicks_confirm(lua: tuple[Any, Any]) -> None:
    """`Confirm` runs `OnConfirm` (dedicationpopup.lua:206-217): close, then one
    `PlayerOperations.COMMEMORATE` per selection. It is only clicked when the popup itself says it
    is clickable."""
    runtime, stubs = lua
    _dedication(runtime, stubs, selected=["MONUMENTALITY"], confirm_disabled=False)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.era_dedication", "MONUMENTALITY")
    )
    assert result["reason"] == "requires_host_click"
    assert result["step"] == "confirm"
    assert result["control"] == "Confirm"
    assert dict(result["click"]) == {"x": 412, "y": 700, "w": 200, "h": 41}
    assert list(stubs.dequeued.values()) == []


def test_a_ticked_card_with_confirm_still_greyed_asks_for_the_rest_rather_than_forcing_it(
    lua: tuple[Any, Any],
) -> None:
    """Two allowed, one ticked: the human could not click Confirm either. The Lua says how many
    more the dedication wants and what is still on the table, and stops -- it never picks a second
    commemoration on the agent's behalf and never dequeues."""
    runtime, stubs = lua
    _dedication(
        runtime, stubs, selected=["MONUMENTALITY"], confirm_disabled=True, allowed=2
    )
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.era_dedication", "MONUMENTALITY")
    )
    assert result["ok"] is False
    assert result["reason"] == "more_selections_required"
    assert result["selections_allowed"] == 2
    assert result["selections_made"] == 1
    assert list(result["selected_options"].values()) == ["MONUMENTALITY"]
    assert list(result["remaining_options"].values()) == ["FREE INQUIRY", "PEN, BRUSH AND VOICE"]
    assert list(stubs.dequeued.values()) == []


def test_a_commemoration_that_is_not_offered_is_refused_and_says_what_was(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    _dedication(runtime, stubs)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.era_dedication", "EXODUS")
    )
    assert result["ok"] is False
    assert result["reason"] == "option_not_offered"
    assert list(result["offered"].values()) == _COMMEMORATIONS
    assert list(stubs.dequeued.values()) == []


def test_only_a_build_with_no_readable_rect_falls_back_to_the_choosers_own_x(
    lua: tuple[Any, Any],
) -> None:
    """The X dedicates NOTHING (dedicationpopup.lua:225-227). A human may do that, so it is the
    fallback of last resort -- and the result has to say that nothing was dedicated."""
    runtime, stubs = lua
    _dedication(runtime, stubs)
    stubs.rect_errors = True
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.era_dedication", "MONUMENTALITY")
    )
    assert result["ok"] is True
    assert result["mechanism"] == "UIManager:DequeuePopup"
    assert result["fallback_reason"] == "option_control_rect_unreadable"
    assert result["dedication_made"] is False
    assert result["hidden_after"] is True
    assert list(stubs.dequeued.values()) == ["DedicationPopup"]


def test_the_chooser_outranks_the_era_card_if_both_are_somehow_open(lua: tuple[Any, Any]) -> None:
    """`erareviewpopup.lua`'s Continue dequeues the card before raising
    `EraReviewPopup_MakeDedication` (:241-247), so in practice only one is up. The watchlist order
    makes the outcome deterministic rather than an accident of table iteration."""
    runtime, stubs = lua
    _dedication(runtime, stubs, also_open=["EraReviewPopup"])
    assert _probe(runtime)["screen"] == "prompt.era_dedication"


def test_a_dedication_answer_with_the_popup_closed_touches_nothing(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table(), runtime.table("DedicationPopup"))
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.era_dedication", "MONUMENTALITY")
    )
    assert result["ok"] is False
    assert result["reason"] == "popup_not_open"
    assert list(stubs.dequeued.values()) == []


# ---------------------------------------------------------------------------
# The World Congress: the welcome card, and the session's own phase controls
# ---------------------------------------------------------------------------


def test_the_congress_welcome_card_is_a_blocking_prompt_offering_accept(
    lua: tuple[Any, Any],
) -> None:
    """MEASURED 2026-09-21 (live stage, game turn 57): the "Begin Voting" card came up over the
    era review and stalled the run as `UnknownScreenEncountered` -- the state was watched but
    mapped to no id. Its option is `accept`, not `continue`: the card is the door into the
    session, not a report to acknowledge."""
    runtime, stubs = lua
    state = _state(runtime, stubs, open=["WorldCongressIntro"], hidden=["CityPanel"])
    assert state["screen"] == "prompt.congress_intro"
    assert state["raw_screen_id"] == "WorldCongressIntro"
    assert state["has_blocking_prompt"] is True
    assert state["prompt_options"] == ["accept"]


def test_accepting_the_welcome_card_clicks_its_own_begin_voting_button(
    lua: tuple[Any, Any],
) -> None:
    """`OnClose` (worldcongressintro.lua:26-29) dequeues the card AND raises
    `WorldCongressIntro_ShowWorldCongress`, which is what opens the congress. Only a real click
    runs both halves."""
    runtime, stubs = lua
    stubs.reset(runtime.table("WorldCongressIntro"), runtime.table())
    result = dict(runtime.globals()["CivSim_Screens"]["respond"]("prompt.congress_intro", "accept"))
    assert result["reason"] == "requires_host_click"
    assert result["close_control"] == "AcceptButton"
    assert list(stubs.dequeued.values()) == []


def test_the_welcome_card_refuses_the_acknowledge_only_continue_option(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table("WorldCongressIntro"), runtime.table())
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.congress_intro", "continue")
    )
    assert result["ok"] is False
    assert result["reason"] == "unknown_option"
    assert list(stubs.dequeued.values()) == []


def test_the_welcome_card_outranks_the_session_it_is_about_to_open(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table("WorldCongressIntro", "WorldCongressPopup"), runtime.table())
    stubs.set_congress_phase(runtime.table("NextButton"), runtime.table())
    assert _probe(runtime)["screen"] == "prompt.congress_intro"


def _congress(
    runtime: Any, stubs: Any, *, live: list[str] = (), greyed: list[str] = ()
) -> dict[str, Any]:
    stubs.reset(runtime.table("WorldCongressPopup"), runtime.table())
    stubs.set_congress_phase(runtime.table(*live), runtime.table(*greyed))
    return _probe(runtime)


def test_a_congress_page_with_a_live_navigation_button_is_a_blocking_prompt(
    lua: tuple[Any, Any],
) -> None:
    """The stage is not read from `m_CurrentStage` (private to that context's own Lua state) but
    from what `UpdateNavButtons` does with the buttons a human sees
    (worldcongresspopup.lua:380-425)."""
    runtime, stubs = lua
    state = _congress(runtime, stubs, live=["NextButton", "AcceptButton"])
    assert state["screen"] == "prompt.congress_vote"
    assert state["raw_screen_id"] == "WorldCongressPopup"
    assert state["has_blocking_prompt"] is True
    assert state["prompt_options"] == ["next", "accept"]


def test_a_congress_session_with_nothing_live_is_the_ordinary_screen_and_does_not_block(
    lua: tuple[Any, Any],
) -> None:
    """MEASURED 2026-09-21 (game turns 56-57): with the session on screen, its Next button
    visible but greyed out and Submit not yet shown, an end turn advanced 56 -> 57. An open
    session is not by itself a blocking prompt, and must not be reported as one."""
    runtime, stubs = lua
    state = _congress(runtime, stubs, greyed=["NextButton"])
    assert state["screen"] == "congress"
    assert state["has_blocking_prompt"] is False
    assert state["prompt_options"] == []


def test_the_special_session_pass_button_is_offered_only_when_it_is_shown(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    assert _congress(runtime, stubs, live=["PassButton", "AcceptButton"])["prompt_options"] == [
        "accept",
        "pass",
    ]


def test_answering_the_congress_clicks_the_named_buttons_own_rect(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    _congress(runtime, stubs, live=["NextButton", "AcceptButton"])
    result = dict(runtime.globals()["CivSim_Screens"]["respond"]("prompt.congress_vote", "accept"))
    assert result["reason"] == "requires_host_click"
    assert result["mechanism"] == "host_click_at_control_rect"
    assert result["state"] == "WorldCongressPopup"
    assert dict(result["click"]) == {"x": 720, "y": 720, "w": 200, "h": 41}


def test_a_congress_option_that_is_not_live_is_refused_and_says_what_was(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    _congress(runtime, stubs, live=["NextButton"], greyed=["AcceptButton"])
    result = dict(runtime.globals()["CivSim_Screens"]["respond"]("prompt.congress_vote", "accept"))
    assert result["ok"] is False
    assert result["reason"] == "option_not_offered"
    assert list(result["offered"].values()) == ["next"]


def test_the_congress_answer_has_no_close_fallback(lua: tuple[Any, Any]) -> None:
    """`OnAccept` submits the player's votes and `OnPass` dismisses a special-session notification
    (worldcongresspopup.lua:2222, :2523); closing the popup instead would abandon the session
    without answering it. A click that cannot be built fails, recorded."""
    runtime, stubs = lua
    _congress(runtime, stubs, live=["NextButton"])
    stubs.rect_errors = True
    result = dict(runtime.globals()["CivSim_Screens"]["respond"]("prompt.congress_vote", "next"))
    assert result["ok"] is False
    assert result["reason"] == "control_rect_unreadable"
    assert list(stubs.dequeued.values()) == []
    assert list(stubs.set_hidden.values()) == []


# ---------------------------------------------------------------------------
# The conversation's exit choice ("Goodbye")
# ---------------------------------------------------------------------------

_GOODBYE = "Goodbye"


def test_the_conversations_exit_choice_closes_the_session_rather_than_answering_it(
    lua: tuple[Any, Any],
) -> None:
    """MEASURED 2026-09-21 (live stage, game turn 58): a conversation offered exactly one choice,
    "Goodbye", the model chose it 16 times and the scene was still up after every one. It is the
    EXIT: `OnSelectConversationDiplomacyStatement` branches `CHOICE_EXIT` off before every
    statement case and runs `ExitConversationMode()` -> `DiplomacyManager.CloseSession`
    (diplomacyactionview.lua:488-493, :310-330)."""
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=[*_GREETING, _GOODBYE])
    stubs.set_open_session(3, 4242)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GOODBYE)
    )
    assert result["ok"] is True
    assert result["path"] == "close_session"
    assert result["mechanism"] == "DiplomacyManager.CloseSession"
    assert result["session_id"] == 4242
    assert result["still_in_conversation"] is False
    assert list(stubs.closed_sessions.values()) == [4242]


def test_a_label_that_resolves_to_no_key_falls_back_to_the_click_and_says_why(
    lua: tuple[Any, Any],
) -> None:
    """With no `DiplomacySelections` row matching the label there is nothing to resolve it to, so
    the button a human clicks is used and the reason is recorded rather than swallowed."""
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=[*_GREETING, _GOODBYE])
    stubs.set_open_session(3, 4242)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GREETING[0])
    )
    assert result["ok"] is False
    assert result["reason"] == "requires_host_click"
    assert result["path"] == "host_click"
    assert result["path_reason"] == "choice_key_unresolved"
    assert list(stubs.closed_sessions.values()) == []
    assert list(stubs.responses.values()) == []


def test_the_exit_choice_falls_back_to_the_click_when_no_session_can_be_proved(
    lua: tuple[Any, Any],
) -> None:
    """With no open session found, which one is on screen is not provable from InGame, so the
    button a human clicks is used instead and the reason is recorded rather than swallowed."""
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=[_GOODBYE])
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GOODBYE)
    )
    assert result["reason"] == "requires_host_click"
    assert result["path"] == "host_click"
    assert result["path_reason"] == "no_open_session_found"
    assert list(stubs.closed_sessions.values()) == []


def test_several_open_sessions_are_never_guessed_between(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=[_GOODBYE])
    stubs.set_open_session(3, 4242)
    stubs.set_open_session(5, 4343)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GOODBYE)
    )
    assert result["path_reason"] == "several_open_sessions"
    assert result["open_session_count"] == 2
    assert list(stubs.closed_sessions.values()) == []


def test_a_failing_close_session_is_returned_as_itself(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=[_GOODBYE])
    stubs.set_open_session(3, 4242)
    stubs.fail_close_session = True
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GOODBYE)
    )
    assert result["ok"] is False
    assert result["path"] == "close_session"
    assert "stubbed CloseSession failure" in result["error"]


# ---------------------------------------------------------------------------
# Answering a statement: the call its own button makes, chosen by its own key
# ---------------------------------------------------------------------------

#: The first-meeting greeting exactly as the store recorded it at game turn 42
#: (`run-d0933ca8...`), with the keys the shipped data gives those two rows
#: (diplomacystatements_firstmeet.xml:164-178: Sort 0 is CHOICE_POSITIVE, Sort 1 is CHOICE_EXIT).
_FIRST_MEET_ACCEPT = "Would you like to visit our nearby city and sample our hospitality?"
_FIRST_MEET_DECLINE = "Thanks for the introduction, but we have no time for further pleasantries."
_FIRST_MEET_ROWS = [
    [_FIRST_MEET_ACCEPT, "CHOICE_POSITIVE"],
    [_FIRST_MEET_DECLINE, "CHOICE_EXIT"],
]
_LEADER_REPLY = ["Goodbye"]


def _first_meeting(runtime: Any, stubs: Any, *, rows: list[list[str]] = None) -> None:
    """The turn-42 greeting: both choices offered, both resolvable to their own keys."""
    _diplomacy(
        runtime, stubs, mode="conversation", texts=[_FIRST_MEET_ACCEPT, _FIRST_MEET_DECLINE]
    )
    stubs.set_selections(
        runtime.table(*[runtime.table(*row) for row in (rows or _FIRST_MEET_ROWS)])
    )
    stubs.set_open_session(3, 4242)
    stubs.reply_choices = runtime.table(*_LEADER_REPLY)


def test_accepting_a_first_meeting_adds_the_response_its_own_button_adds(
    lua: tuple[Any, Any],
) -> None:
    """MEASURED 2026-09-21: three goal runs stuck 16 of 16 steps here, `prompt_options` identical
    at every step, so nothing the harness did reached the game. What DID answer this same greeting
    (12:35 EDT, operator scripting) was `DiplomacyManager.AddResponse`, which is exactly what the
    button's own handler calls for `CHOICE_POSITIVE` (diplomacyactionview.lua:524-526)."""
    runtime, stubs = lua
    _first_meeting(runtime, stubs)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"](
            "prompt.diplomatic_approach", _FIRST_MEET_ACCEPT
        )
    )
    assert result["ok"] is True
    assert result["path"] == "add_response"
    assert result["mechanism"] == "DiplomacyManager.AddResponse"
    assert result["choice_key"] == "CHOICE_POSITIVE"
    assert result["response"] == "POSITIVE"
    assert result["session_id"] == 4242
    sent = [dict(r) for r in stubs.responses.values()]
    assert sent == [{"session_id": 4242, "response": "POSITIVE"}]


def test_a_landed_statement_answer_leaves_the_conversation_open_with_new_choices(
    lua: tuple[Any, Any],
) -> None:
    """MEASURED 12:35 EDT: the response landed and the session STAYED OPEN -- the leader answered
    and the scene was still up. The result says so, and says what is offered now; that is what the
    verification predicate reads (`target not in prompt.options`)."""
    runtime, stubs = lua
    _first_meeting(runtime, stubs)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"](
            "prompt.diplomatic_approach", _FIRST_MEET_ACCEPT
        )
    )
    assert result["still_in_conversation"] is True
    assert list(result["offered_after"].values()) == _LEADER_REPLY
    assert _FIRST_MEET_ACCEPT not in list(result["offered_after"].values())


def test_a_first_meeting_decline_is_the_exit_even_though_it_never_says_goodbye(
    lua: tuple[Any, Any],
) -> None:
    """The second choice of a first meeting is `CHOICE_EXIT` carrying its OWN text
    (diplomacystatements_firstmeet.xml:172-178), not the generic `LOC_DIPLO_CHOICE_EXIT`. Matching
    the string "Goodbye" could never have caught it; resolving by key does."""
    runtime, stubs = lua
    _first_meeting(runtime, stubs)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"](
            "prompt.diplomatic_approach", _FIRST_MEET_DECLINE
        )
    )
    assert result["ok"] is True
    assert result["path"] == "close_session"
    assert result["choice_key"] == "CHOICE_EXIT"
    assert list(stubs.closed_sessions.values()) == [4242]
    assert list(stubs.responses.values()) == []


@pytest.mark.parametrize(
    ("key", "response"),
    [
        ("CHOICE_POSITIVE", "POSITIVE"),
        ("CHOICE_NEGATIVE", "NEGATIVE"),
        ("CHOICE_IGNORE", "RESPONSE_IGNORE"),
    ],
)
def test_each_response_key_sends_the_response_the_shipped_handler_sends(
    lua: tuple[Any, Any], key: str, response: str
) -> None:
    """diplomacyactionview.lua:524-532, reproduced and nothing added."""
    runtime, stubs = lua
    _first_meeting(runtime, stubs, rows=[[_FIRST_MEET_ACCEPT, key]])
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"](
            "prompt.diplomatic_approach", _FIRST_MEET_ACCEPT
        )
    )
    assert result["response"] == response
    assert [dict(r)["response"] for r in stubs.responses.values()] == [response]


def test_a_consequential_statement_keeps_the_click_path_with_its_key_recorded(
    lua: tuple[Any, Any],
) -> None:
    """The war / peace / deal / demand keys (diplomacyactionview.lua:493-521) each have their own
    catalog action. Firing one off a label lookup is not a risk worth taking for a convenience, so
    they stay on the button a human clicks and the record says exactly why."""
    runtime, stubs = lua
    _first_meeting(runtime, stubs, rows=[[_FIRST_MEET_ACCEPT, "CHOICE_DECLARE_SURPRISE_WAR"]])
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"](
            "prompt.diplomatic_approach", _FIRST_MEET_ACCEPT
        )
    )
    assert result["reason"] == "requires_host_click"
    assert result["path"] == "host_click"
    assert result["path_reason"] == "choice_key_not_directly_answerable"
    assert result["choice_key"] == "CHOICE_DECLARE_SURPRISE_WAR"
    assert list(stubs.responses.values()) == []


def test_a_label_two_keys_render_to_is_never_resolved_to_either(lua: tuple[Any, Any]) -> None:
    """An ambiguous label is dropped from the map rather than guessed at; the click still works."""
    runtime, stubs = lua
    _first_meeting(
        runtime,
        stubs,
        rows=[[_FIRST_MEET_ACCEPT, "CHOICE_POSITIVE"], [_FIRST_MEET_ACCEPT, "CHOICE_NEGATIVE"]],
    )
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"](
            "prompt.diplomatic_approach", _FIRST_MEET_ACCEPT
        )
    )
    assert result["path"] == "host_click"
    assert result["path_reason"] == "choice_key_unresolved"
    assert list(stubs.responses.values()) == []


def test_an_unreadable_selection_table_still_answers_a_plain_goodbye(
    lua: tuple[Any, Any],
) -> None:
    """Without the table there is one signal that still stands on its own -- the generic exit text
    -- and it must keep working, because that is the conversation a leader's reply ends with."""
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=[_GOODBYE])
    stubs.drop_selection_table = True
    stubs.set_open_session(3, 4242)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.diplomatic_approach", _GOODBYE)
    )
    assert result["path"] == "close_session"
    assert result["choice_key"] == "CHOICE_EXIT"
    assert list(stubs.closed_sessions.values()) == [4242]


def test_an_unreadable_selection_table_records_that_as_the_reason_for_a_click(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    _diplomacy(runtime, stubs, mode="conversation", texts=[_FIRST_MEET_ACCEPT])
    stubs.drop_selection_table = True
    stubs.set_open_session(3, 4242)
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"](
            "prompt.diplomatic_approach", _FIRST_MEET_ACCEPT
        )
    )
    assert result["path"] == "host_click"
    assert result["path_reason"] == "selection_table_unreadable"


def test_a_failing_add_response_is_returned_as_itself(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    _first_meeting(runtime, stubs)
    stubs.fail_add_response = True
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"](
            "prompt.diplomatic_approach", _FIRST_MEET_ACCEPT
        )
    )
    assert result["ok"] is False
    assert result["path"] == "add_response"
    assert "stubbed AddResponse failure" in result["error"]
    assert result["still_in_conversation"] is True
