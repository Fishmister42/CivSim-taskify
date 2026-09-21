"""T258: `lua/ingame/yields.lua` executed for real, with the top bar's accessors stubbed.

Same discipline as `test_screens_lua.py`: the file's logic runs in an embedded Lua 5.4 against
stubs shaped like the objects Firaxis's own `toppanel.lua` calls. That the InGame tuner state
answers those calls stays UNVERIFIED LIVE and is said so in the Lua. Skipped where `lupa` is
absent: ``uv run --with lupa pytest tests/unit/test_yields_lua.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

lupa = pytest.importorskip("lupa")

REPO_ROOT = Path(__file__).resolve().parents[2]
YIELDS_LUA = REPO_ROOT / "lua" / "ingame" / "yields.lua"

_STUBS = """
local M = { fail = {}, science = 6.5, culture = 3, faith = 0, faith_balance = 12,
            gold_yield = 5.4, maintenance = 3, gold_balance = 41.9, tourism = 0, local_player = 0 }
local function guarded(name, fn)
    return function(...)
        if M.fail[name] then error("stubbed " .. name .. " failure") end
        return fn(...)
    end
end
local player = {}
function player:GetTechs()
    return { GetScienceYield = guarded("science", function() return M.science end) }
end
function player:GetCulture()
    return { GetCultureYield = guarded("culture", function() return M.culture end) }
end
function player:GetReligion()
    return {
        GetFaithYield = guarded("faith", function() return M.faith end),
        GetFaithBalance = guarded("faith_balance", function() return M.faith_balance end),
    }
end
function player:GetTreasury()
    return {
        GetGoldYield = guarded("gold", function() return M.gold_yield end),
        GetTotalMaintenance = function() return M.maintenance end,
        GetGoldBalance = guarded("gold_balance", function() return M.gold_balance end),
    }
end
function player:GetStats()
    return { GetTourism = guarded("tourism", function() return M.tourism end) }
end
Players = { [0] = player }
Game = {
    GetLocalPlayer = function() return M.local_player end,
    GetCurrentGameTurn = function() return 17 end,
}
return M
"""


@pytest.fixture
def lua() -> tuple[Any, Any]:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    stubs = runtime.execute(_STUBS)
    runtime.execute(YIELDS_LUA.read_text(encoding="utf-8"))
    return runtime, stubs


def _read(runtime: Any) -> dict[str, Any]:
    return dict(runtime.globals()["CivSim_Yields"]["yields"]())


def test_the_dispatch_table_exposes_the_declarations_last_segment(lua: tuple[Any, Any]) -> None:
    runtime, _stubs = lua
    assert sorted(runtime.globals()["CivSim_Yields"].keys()) == ["yields"]


def test_the_seven_top_bar_numbers_are_read_exactly_as_the_bar_shows_them(
    lua: tuple[Any, Any],
) -> None:
    runtime, _stubs = lua
    result = _read(runtime)
    assert result == {
        "turn_number": 17,
        "science_per_turn": 6.5,
        "culture_per_turn": 3,
        "faith_per_turn": 0,
        "faith_balance": 12,
        "gold_per_turn": 5.4 - 3,  # net of maintenance, as the top bar displays gold
        "gold_balance": 41,  # floored, as the top bar displays the balance
        "tourism_per_turn": 0,
    }


def test_a_read_that_errors_is_absent_from_the_result_and_the_others_still_answer(
    lua: tuple[Any, Any],
) -> None:
    """Constitution III: a missing measurement is a gap, never a fabricated zero -- and one
    failing accessor must not take the other six with it."""
    runtime, stubs = lua
    stubs.fail["gold"] = True
    stubs.fail["tourism"] = True
    result = _read(runtime)
    assert "gold_per_turn" not in result
    assert "tourism_per_turn" not in result
    assert result["science_per_turn"] == 6.5
    assert result["gold_balance"] == 41


def test_no_resolvable_local_player_reports_the_reason_and_no_numbers(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.local_player = 7  # no such Players entry
    result = _read(runtime)
    assert result == {"reason": "local player is not resolvable from this state"}
