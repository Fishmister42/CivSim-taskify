"""T260: `lua/ingame/camera.lua`'s look-at resolution executed for real, with `UI`,
`PlayersVisibility`, `Map` and `Game` stubbed.

Same discipline as `test_city_selection_lua.py` and `test_screens_lua.py`: the file's own logic is
asserted in an embedded Lua 5.4, while the question of whether the live InGame tuner answers
`UI.GetMapLookAtWorldTarget()` / `UI.GetPlotCoordFromWorld(...)` stays UNVERIFIED LIVE and is said
so in the Lua. Skipped, not failed, where `lupa` is absent (not a project dependency):
``uv run --with lupa pytest tests/unit/test_camera_lua.py``.

What matters here is the fail-closed shape (FR-026, Principle I): a plot only ever comes back when
both accessors answered with numbers, reveal is read for the LOCAL player only, and every absent or
erroring accessor yields *no plot*, ``target_is_revealed = false`` and a
``target_unavailable_reason`` naming what failed -- never a guessed plot and never a silent false.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

lupa = pytest.importorskip("lupa")

REPO_ROOT = Path(__file__).resolve().parents[2]
CAMERA_LUA = REPO_ROOT / "lua" / "ingame" / "camera.lua"

_STUBS = """
local M = {
    zoom = 0.70710706710815,
    render_view = 0,
    world = { 120.5, -30.25 },
    world_z = nil,
    world_error = false,
    legacy_plot = nil,
    plot = { 24, 30 },
    plot_error = false,
    plot_args = nil,
    plot_arg_count = 0,
    coord_form = "boolean",
    index_form = "nil",
    revealed = true,
    index_revealed = false,
    index_arg = nil,
    local_player = 0,
}

UI = {}
function UI.GetMapZoom() return M.zoom end
function UI.GetWorldRenderView() return M.render_view end
function UI.GetCameraTargetPlot()
    if M.legacy_plot == nil then error("stubbed GetCameraTargetPlot is absent") end
    return M.legacy_plot[1], M.legacy_plot[2]
end
function UI.GetMapLookAtWorldTarget()
    if M.world_error then error("stubbed GetMapLookAtWorldTarget failure") end
    if M.world_z ~= nil then return M.world[1], M.world[2], M.world_z end
    return M.world[1], M.world[2]
end
function UI.GetPlotCoordFromWorld(...)
    M.plot_arg_count = select("#", ...)
    M.plot_args = { ... }
    if M.plot_error then error("stubbed GetPlotCoordFromWorld failure") end
    return M.plot[1], M.plot[2]
end

Game = {}
function Game.GetLocalPlayer() return M.local_player end

Map = {}
function Map.GetPlotIndex(x, y)
    M.index_arg = { x, y }
    return (y * 1000) + x
end

local visibility = {}
function visibility:IsRevealed(a, b)
    if b ~= nil then
        if M.coord_form == "error" then error("stubbed (x, y) IsRevealed failure") end
        if M.coord_form == "nil" then return nil end
        return M.revealed
    end
    if M.index_form == "error" then error("stubbed plot-index IsRevealed failure") end
    if M.index_form == "nil" then return nil end
    return M.index_revealed
end

PlayersVisibility = { [0] = visibility }
return M
"""


@pytest.fixture
def lua() -> tuple[Any, Any]:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    stubs = runtime.execute(_STUBS)
    runtime.execute(CAMERA_LUA.read_text(encoding="utf-8"))
    return runtime, stubs


def _read(runtime: Any) -> dict[str, Any]:
    state = dict(runtime.globals()["CivSim_Camera"]["read_state"]())
    plot = state.get("target_plot")
    if plot is not None:
        state["target_plot"] = dict(plot)
    return state


# --------------------------------------------------------------------------
# The accessors are present and answer -- the case that unblocks every capture
# --------------------------------------------------------------------------


def test_the_look_at_world_target_is_mapped_to_a_plot_and_its_reveal_confirmed(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    state = _read(runtime)

    assert state["target_plot"] == {"x": 24, "y": 30}
    assert state["target_is_revealed"] is True
    assert state.get("target_unavailable_reason") is None
    # The world target reached GetPlotCoordFromWorld verbatim, as in Firaxis' own
    # automation_observercamera.lua:381-382 pairing.
    assert stubs.plot_arg_count == 2
    assert dict(stubs.plot_args) == {1: 120.5, 2: -30.25}


def test_a_world_target_carrying_a_z_passes_it_as_the_third_argument(
    lua: tuple[Any, Any],
) -> None:
    """unitflagmanager.lua:1172 passes the optional world Z; the two-argument form is used only
    when the world target gave no Z."""
    runtime, stubs = lua
    stubs.world_z = 7.5
    _read(runtime)

    assert stubs.plot_arg_count == 3
    assert dict(stubs.plot_args) == {1: 120.5, 2: -30.25, 3: 7.5}


def test_plot_coordinates_are_reported_as_integers(lua: tuple[Any, Any]) -> None:
    """`catalogs/observations/camera.yaml` declares x/y as integers, and the JSON encoder here
    writes a Lua float as `24.0`, which that schema check would reject."""
    runtime, stubs = lua
    stubs.plot = runtime.eval("{ 24.0, 30.9 }")
    state = _read(runtime)

    assert state["target_plot"] == {"x": 24, "y": 30}
    assert isinstance(state["target_plot"]["x"], int)
    assert isinstance(state["target_plot"]["y"], int)


def test_an_unrevealed_target_plot_is_reported_unrevealed_without_blaming_an_accessor(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.revealed = False
    state = _read(runtime)

    assert state["target_plot"] == {"x": 24, "y": 30}
    assert state["target_is_revealed"] is False
    # Nothing failed -- the plot simply is not revealed, and the record should not claim otherwise.
    assert state.get("target_unavailable_reason") is None


def test_the_reveal_read_falls_back_to_the_plot_index_form(lua: tuple[Any, Any]) -> None:
    """minimappanel.lua:893-898 calls the same object with `Map.GetPlotIndex(x, y)`; a build whose
    IsRevealed answers only that form must still be readable."""
    runtime, stubs = lua
    stubs.coord_form = "nil"
    stubs.index_form = "boolean"
    stubs.index_revealed = True
    state = _read(runtime)

    assert state["target_is_revealed"] is True
    assert dict(stubs.index_arg) == {1: 24, 2: 30}


def test_an_erroring_coordinate_form_does_not_hide_the_index_form(lua: tuple[Any, Any]) -> None:
    """Each overload is attempted under its own pcall -- a raise in the first must not abort the
    second."""
    runtime, stubs = lua
    stubs.coord_form = "error"
    stubs.index_form = "boolean"
    stubs.index_revealed = True

    assert _read(runtime)["target_is_revealed"] is True


# --------------------------------------------------------------------------
# Absent, erroring and nonsensical accessors -- every one fails closed, with a reason
# --------------------------------------------------------------------------


def test_an_absent_look_at_accessor_yields_no_plot_and_names_the_missing_accessor(
    lua: tuple[Any, Any],
) -> None:
    runtime, _stubs = lua
    runtime.globals()["UI"]["GetMapLookAtWorldTarget"] = None
    state = _read(runtime)

    assert state.get("target_plot") is None
    assert state["target_is_revealed"] is False
    assert "UI.GetMapLookAtWorldTarget is absent" in state["target_unavailable_reason"]


def test_there_is_no_legacy_look_at_getter_to_fall_back_to(lua: tuple[Any, Any]) -> None:
    """This used to assert the opposite: that `UI.GetCameraTargetPlot` would be used "on a build
    that carries it", so this file needed no per-build fork.

    The 2026-09-21 accessor audit
    (specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md, finding 35) found
    there is no such build. `UI.GetCameraTargetPlot` appears in none of Firaxis' 645 shipped Lua
    files and is not a registered UI binding in any shipped binary -- T213's MEASURED absence on
    1.0.12.9 was not a build quirk, it was the name being invented. Keeping the fallback cost
    nothing but it claimed something false, so the branch is gone and the absence of
    `UI.GetMapLookAtWorldTarget` is now simply the end of the road.

    Asserted here (rather than deleted) so the claim stays retired: a future edit that
    reintroduces the fallback fails this test, and `lua/ACCESSORS.txt` would reject the name too.
    """
    runtime, stubs = lua
    runtime.globals()["UI"]["GetMapLookAtWorldTarget"] = None
    stubs.legacy_plot = runtime.eval("{ 11, 12 }")
    state = _read(runtime)

    assert state.get("target_plot") is None
    assert state["target_is_revealed"] is False
    assert state["target_unavailable_reason"] == (
        "UI.GetMapLookAtWorldTarget is absent on this build"
    )


def test_an_erroring_look_at_accessor_reports_the_error_not_a_plot(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.world_error = True
    state = _read(runtime)

    assert state.get("target_plot") is None
    assert state["target_is_revealed"] is False
    assert "UI.GetMapLookAtWorldTarget errored" in state["target_unavailable_reason"]


def test_an_erroring_plot_coord_accessor_reports_the_error_not_a_plot(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.plot_error = True
    state = _read(runtime)

    assert state.get("target_plot") is None
    assert state["target_is_revealed"] is False
    assert "UI.GetPlotCoordFromWorld errored" in state["target_unavailable_reason"]


def test_a_non_numeric_world_target_is_not_treated_as_a_plot(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.world = runtime.eval('{ "nope", "nope" }')
    state = _read(runtime)

    assert state.get("target_plot") is None
    assert "no numeric world target" in state["target_unavailable_reason"]


def test_an_off_map_world_target_is_no_plot_at_all(lua: tuple[Any, Any]) -> None:
    """fullscreenmappopup.lua:111-116 reads -1 back from GetPlotCoordFromWorld as "off the map"."""
    runtime, stubs = lua
    stubs.plot = runtime.eval("{ -1, -1 }")
    state = _read(runtime)

    assert state.get("target_plot") is None
    assert state["target_is_revealed"] is False
    assert "off-map" in state["target_unavailable_reason"]


def test_an_unreadable_players_visibility_withholds_the_reveal_with_a_reason(
    lua: tuple[Any, Any],
) -> None:
    runtime, _stubs = lua
    runtime.globals()["PlayersVisibility"][0] = None
    state = _read(runtime)

    assert state["target_plot"] == {"x": 24, "y": 30}
    assert state["target_is_revealed"] is False
    assert "PlayersVisibility" in state["target_unavailable_reason"]


def test_a_reveal_read_answering_neither_form_is_false_with_a_reason(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.coord_form = "nil"
    stubs.index_form = "nil"
    state = _read(runtime)

    assert state["target_is_revealed"] is False
    assert "IsRevealed" in state["target_unavailable_reason"]


def test_the_reveal_is_read_for_the_local_player_only(lua: tuple[Any, Any]) -> None:
    """Principle I: another player's visibility is never consulted. With the local player id
    pointing at an entry that does not exist, the read fails closed rather than falling back to
    any other player's visibility."""
    runtime, stubs = lua
    stubs.local_player = 3
    state = _read(runtime)

    assert state["target_is_revealed"] is False
    assert "PlayersVisibility" in state["target_unavailable_reason"]


# --------------------------------------------------------------------------
# The rest of the camera state is unchanged by T260
# --------------------------------------------------------------------------


def test_zoom_and_mode_still_come_from_the_measured_accessors(lua: tuple[Any, Any]) -> None:
    runtime, _stubs = lua
    state = _read(runtime)

    assert state["zoom"] == pytest.approx(0.70710706710815)
    assert state["mode"] == "world"


def test_the_dispatch_table_still_exposes_the_four_camera_entry_points(
    lua: tuple[Any, Any],
) -> None:
    runtime, _stubs = lua
    assert sorted(runtime.globals()["CivSim_Camera"].keys()) == [
        "move",
        "read_state",
        "set_view_mode",
        "zoom",
    ]
