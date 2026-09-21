"""T253: `lua/ingame/screens.lua` executed for real, with the game's globals stubbed.

The harness ships Lua it cannot run headlessly, and until now the only check on a Lua edit short
of a live client was the executor's regex over its dispatch table. This module runs the file in an
embedded Lua 5.4 (`lupa`) with `ContextPtr` and `UIManager` stubbed to the shape Firaxis's own UI
scripts use, so what is asserted is the file's *logic* -- which screen id a set of open states
resolves to, which options it offers, which call it makes to dismiss a popup, and what it returns
when the call fails. What it cannot assert is that the real client's `UIManager:DequeuePopup`
accepts another state's context from InGame; that stays UNVERIFIED LIVE and is said so in the Lua.

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

# Stubs for the two Firaxis globals the file touches. `LookUpControl("/InGame/<State>")` answers
# a control for each state in `open_states`/`hidden_states` and nil otherwise, exactly like the
# live client did for `CivilopediaScreen` (absent on this build). `UIManager:DequeuePopup` records
# the call and, unless scripted to fail, hides the control -- what the real popup's hide event
# does after its own `Close()`.
_STUBS = """
local M = {}
M.dequeued = {}
M.fail_dequeue = false
M.controls = {}

local function make_control(name, hidden)
    local c = { name = name, hidden = hidden }
    function c:IsHidden() return self.hidden end
    return c
end

function M.reset(open_states, hidden_states)
    M.dequeued = {}
    M.fail_dequeue = false
    M.controls = {}
    for _, s in ipairs(open_states) do M.controls["/InGame/" .. s] = make_control(s, false) end
    for _, s in ipairs(hidden_states) do M.controls["/InGame/" .. s] = make_control(s, true) end
end

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
    assert state["screen"] == "world_view"
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


def test_an_unmapped_watchlist_state_is_still_unknown_never_guessed(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    state = _state(runtime, stubs, open=["NaturalWonderPopup"])
    assert state["screen"] == "unknown"
    assert state["recognized"] is False
    assert state["has_blocking_prompt"] is False


def test_acknowledging_the_popup_dequeues_its_own_context_and_reports_it_hidden(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.reset(runtime.table("TechCivicCompletedPopup"), runtime.table())
    result = dict(
        runtime.globals()["CivSim_Screens"]["respond"]("prompt.tech_civic_completed", "continue")
    )
    assert result["ok"] is True
    assert result["mechanism"] == "UIManager:DequeuePopup"
    assert result["hidden_after"] is True
    assert list(stubs.dequeued.values()) == ["TechCivicCompletedPopup"]


def test_a_failing_dequeue_is_returned_as_itself_with_the_error(lua: tuple[Any, Any]) -> None:
    """The live half is unverified, so a failure must come back as a fact the record can carry,
    not as `ok` -- the verification predicate then reports the popup still open."""
    runtime, stubs = lua
    stubs.reset(runtime.table("BoostUnlockedPopup"), runtime.table())
    stubs.fail_dequeue = True
    respond = runtime.globals()["CivSim_Screens"]["respond"]
    result = dict(respond("prompt.boost_unlocked", "continue"))
    assert result["ok"] is False
    assert result["hidden_after"] is False
    assert "stubbed DequeuePopup failure" in result["error"]
    assert list(stubs.dequeued.values()) == []


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
