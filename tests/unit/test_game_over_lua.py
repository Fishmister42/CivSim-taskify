"""`lua/ingame/game_over.lua` executed for real, with the end-game screen's accessors stubbed.

Same discipline as `test_screens_lua.py` and `test_yields_lua.py`: the file's logic runs in an
embedded Lua 5.4 against stubs shaped like the objects Firaxis's own
`base/assets/ui/endgame/endgamemenu.lua` calls (`Game.GetLocalPlayer`, `Players[id]:IsAlive()` /
`:GetTeam()`, `Game.GetWinningTeam()`'s two return values, `GameInfo.Victories`, `Teams`,
`PlayerConfigurations[id]:GetCivilizationDescription()`, `Locale.Lookup`, and
`ContextPtr:LookUpControl("/InGame/EndGameMenu")`). What is asserted is the file's own logic --
which outcome a board resolves to, that the victory type and winner are read **only** once the
game is over, and that an accessor which errors never fabricates an ending. That the InGame tuner
state answers these calls on a finished game stays UNVERIFIED LIVE and is said so in the Lua.

Skipped where `lupa` is absent: ``uv run --with lupa pytest tests/unit/test_game_over_lua.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

lupa = pytest.importorskip("lupa")

REPO_ROOT = Path(__file__).resolve().parents[2]
GAME_OVER_LUA = REPO_ROOT / "lua" / "ingame" / "game_over.lua"

# `M` scripts the board. `fail` turns any single accessor into an error, exactly as a build that
# renamed or removed it would behave; every read in the Lua is independently pcall'd, so one
# failure must never take the others with it.
_STUBS = """
local M = {
    fail = {},
    local_player = 0,
    alive = true,
    local_team = 0,
    winning_team = nil,
    victory_type = nil,
    end_screen_hidden = true,
    end_screen_missing = false,
}
local function guarded(name, fn)
    return function(...)
        if M.fail[name] then error("stubbed " .. name .. " failure") end
        return fn(...)
    end
end

local player = {}
player.IsAlive = guarded("alive", function() return M.alive end)
player.GetTeam = guarded("team", function() return M.local_team end)
Players = { [0] = player }

-- Two civilizations, so "the winner" is never trivially the local player.
PlayerConfigurations = {
    [0] = {
        GetCivilizationDescription = guarded("local_civ", function()
            return "LOC_CIVILIZATION_PERSIA_NAME"
        end),
    },
    [1] = {
        GetCivilizationDescription = guarded("winner_civ", function()
            return "LOC_CIVILIZATION_GEORGIA_NAME"
        end),
    },
}
Teams = { [0] = { 0 }, [1] = { 1 } }

Game = {
    GetLocalPlayer = guarded("local_player", function() return M.local_player end),
    -- endgamemenu.lua:1058 -- two return values, the second being the victory type.
    GetWinningTeam = guarded("winning_team", function()
        return M.winning_team, M.victory_type
    end),
}

GameInfo = {
    Victories = setmetatable({}, {
        __index = function(_, key)
            if M.fail["victories"] then error("stubbed victories failure") end
            if key == "VICTORY_CONQUEST" then
                return { VictoryType = "VICTORY_CONQUEST", Name = "LOC_VICTORY_DOMINATION_NAME" }
            end
            if key == "VICTORY_NAMELESS" then
                return { VictoryType = "VICTORY_NAMELESS" }
            end
            return nil
        end,
    }),
}

Locale = {
    Lookup = guarded("locale", function(key)
        local translations = {
            LOC_VICTORY_DOMINATION_NAME = "Domination Victory",
            LOC_CIVILIZATION_GEORGIA_NAME = "Georgia",
            LOC_CIVILIZATION_PERSIA_NAME = "Persia",
        }
        return translations[key] or key
    end),
}

local end_game_context = {}
function end_game_context:IsHidden()
    if M.fail["end_screen"] then error("stubbed IsHidden failure") end
    return M.end_screen_hidden
end

ContextPtr = {}
function ContextPtr:LookUpControl(path)
    if M.fail["lookup"] then error("stubbed LookUpControl failure") end
    if path == "/InGame/EndGameMenu" and not M.end_screen_missing then
        return end_game_context
    end
    return nil
end

return M
"""


@pytest.fixture
def lua() -> tuple[Any, Any]:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    stubs = runtime.execute(_STUBS)
    runtime.execute(GAME_OVER_LUA.read_text(encoding="utf-8"))
    return runtime, stubs


def _read(runtime: Any) -> dict[str, Any]:
    return dict(runtime.globals()["CivSim_GameOver"]["read"]())


def test_the_dispatch_table_exposes_exactly_one_read(lua: tuple[Any, Any]) -> None:
    runtime, _stubs = lua
    assert sorted(runtime.globals()["CivSim_GameOver"].keys()) == ["read"]


def test_an_alive_player_with_no_winner_is_not_a_game_over(lua: tuple[Any, Any]) -> None:
    runtime, _stubs = lua
    result = _read(runtime)
    assert result["game_over"] is False
    assert result["local_player_alive"] is True
    # Principle I: nothing about any other civilization is read while the game is still being
    # played -- the victory type and winner are not merely absent from the answer, the accessors
    # are never reached (see the file's structural gate).
    assert "outcome" not in result
    assert "victory_type" not in result
    assert "winner" not in result


def test_a_defeated_player_with_the_end_menu_up_is_a_defeat(lua: tuple[Any, Any]) -> None:
    """The measured 2026-09-21 case: eliminated at game turn 59, nobody had won yet, and the
    client was sitting on `EndGameMenu`. endgamemenu.lua:1057/1069 -- not alive means the human
    is shown the defeat screen, and that screen's winner panel is empty (:832)."""
    runtime, stubs = lua
    stubs.alive = False
    stubs.end_screen_hidden = False
    result = _read(runtime)
    assert result["game_over"] is True
    assert result["outcome"] == "defeat"
    assert result["local_player_alive"] is False
    assert result["basis"] == "local_player_not_alive"
    assert result["end_game_screen_shown"] is True
    assert "victory_type" not in result
    assert "winner" not in result


def test_another_teams_victory_is_a_defeat_that_names_the_winner(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.winning_team = 1
    stubs.victory_type = "VICTORY_CONQUEST"
    result = _read(runtime)
    assert result["game_over"] is True
    assert result["outcome"] == "defeat"
    assert result["basis"] == "winning_team_is_another_team"
    # Both are on the end-game screen the human is looking at, and only now (endgamemenu.lua:926,
    # :951) -- so both are reported, localized, never as a raw LOC key.
    assert result["victory_type"] == "Domination Victory"
    assert result["winner"] == "Georgia"


def test_the_local_teams_victory_is_a_victory_with_its_type(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.winning_team = 0
    stubs.victory_type = "VICTORY_CONQUEST"
    stubs.end_screen_hidden = False
    result = _read(runtime)
    assert result["game_over"] is True
    assert result["outcome"] == "victory"
    assert result["basis"] == "winning_team_is_local_team"
    assert result["victory_type"] == "Domination Victory"
    assert result["winner"] == "Persia"
    assert result["end_game_screen_shown"] is True


def test_a_winning_team_with_a_dead_local_player_still_reads_as_a_defeat(
    lua: tuple[Any, Any],
) -> None:
    """endgamemenu.lua:1057 checks `IsAlive()` first; a dead player is shown the defeat screen
    whatever else is true. The winner is still named, because that screen names it."""
    runtime, stubs = lua
    stubs.alive = False
    stubs.winning_team = 1
    stubs.victory_type = "VICTORY_CONQUEST"
    result = _read(runtime)
    assert result["outcome"] == "defeat"
    assert result["basis"] == "local_player_not_alive"
    assert result["winner"] == "Georgia"


def test_an_erroring_alive_accessor_is_not_a_game_over(lua: tuple[Any, Any]) -> None:
    """A read that errors is not a game over. `IsAlive()` failing leaves `local_player_alive`
    absent (Constitution III: a gap, never a fabricated value) and, with nobody having won, the
    game is still reported as running."""
    runtime, stubs = lua
    stubs.fail["alive"] = True
    result = _read(runtime)
    assert result["game_over"] is False
    assert "local_player_alive" not in result
    assert "outcome" not in result


def test_an_erroring_winning_team_accessor_is_not_a_game_over(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.fail["winning_team"] = True
    result = _read(runtime)
    assert result["game_over"] is False
    assert result["local_player_alive"] is True


def test_no_resolvable_local_player_reports_the_reason_and_nothing_else(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.local_player = -1
    result = _read(runtime)
    assert result == {
        "game_over": False,
        "reason": "local player is not resolvable from this state",
    }


def test_an_unnameable_victory_still_reports_the_outcome(lua: tuple[Any, Any]) -> None:
    """A `GameInfo.Victories` row with no `Name`, or a lookup this build cannot resolve, must not
    take the outcome down with it: the ending is still real, only its label is missing."""
    runtime, stubs = lua
    stubs.winning_team = 1
    stubs.victory_type = "VICTORY_NAMELESS"
    result = _read(runtime)
    assert result["game_over"] is True
    assert result["outcome"] == "defeat"
    assert "victory_type" not in result
    assert result["winner"] == "Georgia"


def test_an_erroring_locale_falls_back_to_the_key_rather_than_to_nothing(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.winning_team = 1
    stubs.victory_type = "VICTORY_CONQUEST"
    stubs.fail["locale"] = True
    result = _read(runtime)
    assert result["victory_type"] == "LOC_VICTORY_DOMINATION_NAME"
    assert result["winner"] == "LOC_CIVILIZATION_GEORGIA_NAME"


def test_an_unreachable_end_game_context_leaves_the_corroboration_absent(
    lua: tuple[Any, Any],
) -> None:
    """The screen check is corroboration, never a gate: a defeat whose popup never queued, or a
    build where the context cannot be looked up at all, is still a defeat."""
    runtime, stubs = lua
    stubs.end_screen_missing = True
    stubs.alive = False
    result = _read(runtime)
    assert result["game_over"] is True
    assert result["outcome"] == "defeat"
    assert "end_game_screen_shown" not in result


def test_an_erroring_lookup_control_does_not_stop_the_read(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.fail["lookup"] = True
    stubs.alive = False
    result = _read(runtime)
    assert result["game_over"] is True
    assert result["outcome"] == "defeat"
    assert "end_game_screen_shown" not in result
