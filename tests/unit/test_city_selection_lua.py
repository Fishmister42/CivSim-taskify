"""T255: `lua/ingame/city_selection.lua` executed for real, with `UI` and `Game` stubbed.

Same discipline as `test_screens_lua.py`: the file's logic is asserted in an embedded Lua 5.4;
that the real InGame tuner state answers `UI.GetHeadSelectedCity()` stays UNVERIFIED LIVE and is
said so in the Lua. Skipped, not failed, where `lupa` is absent (not a project dependency):
``uv run --with lupa pytest tests/unit/test_city_selection_lua.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

lupa = pytest.importorskip("lupa")

REPO_ROOT = Path(__file__).resolve().parents[2]
CITY_SELECTION_LUA = REPO_ROOT / "lua" / "ingame" / "city_selection.lua"

_STUBS = """
local M = { selected = nil, local_player = 0, fail = false }
UI = {}
function UI.GetHeadSelectedCity()
    if M.fail then error("stubbed GetHeadSelectedCity failure") end
    return M.selected
end
Game = {}
function Game.GetLocalPlayer() return M.local_player end
function M.select(id, owner)
    M.selected = {
        GetID = function(self) return id end,
        GetOwner = function(self) return owner end,
    }
end
return M
"""


@pytest.fixture
def lua() -> tuple[Any, Any]:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    stubs = runtime.execute(_STUBS)
    runtime.execute(CITY_SELECTION_LUA.read_text(encoding="utf-8"))
    return runtime, stubs


def _read(runtime: Any) -> dict[str, Any]:
    return dict(runtime.globals()["CivSim_CitySelection"]["selection"]())


def test_the_dispatch_table_exposes_the_declarations_last_segment(lua: tuple[Any, Any]) -> None:
    runtime, _stubs = lua
    assert sorted(runtime.globals()["CivSim_CitySelection"].keys()) == ["selection"]


def test_no_selected_city_reports_has_selection_false_and_no_id(lua: tuple[Any, Any]) -> None:
    runtime, _stubs = lua
    result = _read(runtime)
    assert result == {"has_selection": False}


def test_the_local_players_selected_city_is_reported_by_id_and_owner(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.select(65538, 0)
    result = _read(runtime)
    assert result["has_selection"] is True
    assert result["selected_city_id"] == 65538
    assert result["owner_player_id"] == 0
    assert result["owner_is_local_player"] is True


def test_another_players_selected_city_is_reported_but_not_as_the_local_players(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.select(70001, 3)
    result = _read(runtime)
    assert result["has_selection"] is True
    assert result["selected_city_id"] == 70001
    assert result["owner_is_local_player"] is False


def test_an_erroring_read_reports_no_selection_with_the_reason_never_a_guess(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.select(1, 0)
    stubs.fail = True
    result = _read(runtime)
    assert result["has_selection"] is False
    assert "stubbed GetHeadSelectedCity failure" in result["reason"]
    assert "selected_city_id" not in result
