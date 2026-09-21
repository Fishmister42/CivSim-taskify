"""`lua/gamecore/units.lua`'s build list and `lua/ingame/unit_orders.lua`'s build order, executed
for real against fakes shaped like Civilization VI's own unit panel.

The gap these close: `catalogs/actions/units.yaml` declared no action that spends a builder charge,
so the directed goal `use_a_builder` (tests/live/goals/use_a_builder.yaml) was authored blocked --
"NO action in catalogs/actions/ spends a builder charge" -- and the harness had never built an
improvement. The fakes below reproduce the two calls Firaxis' own panel makes
(steamassets/base/assets/ui/panels/unitpanel.lua:554-573 to list the buttons, :2548-2557 to click
one), so "the panel would show this button" and "this is the order the button issues" are asserted
rather than assumed.

Same discipline as `test_cities_lua.py` / `test_city_selection_lua.py`: the file's logic is
asserted in an embedded Lua 5.4; that the real InGame tuner state answers these calls stays
UNVERIFIED LIVE and is said so in the Lua. Skipped, not failed, where `lupa` is absent (not a
project dependency): ``uv run --with lupa pytest tests/unit/test_unit_builds_lua.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

lupa = pytest.importorskip("lupa")

REPO_ROOT = Path(__file__).resolve().parents[2]
UNITS_LUA = REPO_ROOT / "lua" / "gamecore" / "units.lua"
UNIT_ORDERS_LUA = REPO_ROOT / "lua" / "ingame" / "unit_orders.lua"

FARM_HASH = 7001
MINE_HASH = 7002
PASTURE_HASH = 7003

# A miniature of the shipped database and of the objects the unit panel calls. The engine's two
# independent reasons for offering no build button at all are modelled separately, because they
# are separate on screen: a unit with no Build ability never has a build row, and a Builder that
# has spent its last charge is gone from the map with its row gone too.
_STUBS = """
local M = {
    local_player = 0,
    units = {},
    offered = {},
    best = -1,
    greyed = {},
    can_start_errors = false,
    requested = nil,
    request_accepted = true,
    plot_improvement = nil,
    can_start_calls = {},
    visible = true,
}

UnitOperationTypes = {
    BUILD_IMPROVEMENT = 1001,
    FOUND_CITY = 1002,
    MOVE_TO = 1003,
    PARAM_X = "param_x",
    PARAM_Y = "param_y",
    PARAM_IMPROVEMENT_TYPE = "param_improvement",
}
UnitOperationResults = { IMPROVEMENTS = "improvements", BEST_IMPROVEMENT = "best_improvement" }

local function make_info_table(rows)
    local by_key = {}
    for _, row in ipairs(rows) do
        by_key[row.Hash] = row
        by_key[row.Index] = row
        for _, field in ipairs({"ImprovementType", "UnitType"}) do
            if row[field] ~= nil then by_key[row[field]] = row end
        end
    end
    return setmetatable({}, {
        __call = function()
            local i = 0
            return function() i = i + 1 return rows[i] end
        end,
        __index = function(_, key) return by_key[key] end,
    })
end

GameInfo = {}
GameInfo.Improvements = make_info_table({
    { ImprovementType = "IMPROVEMENT_FARM", Name = "LOC_IMPROVEMENT_FARM_NAME",
      Hash = 7001, Index = 11 },
    { ImprovementType = "IMPROVEMENT_MINE", Name = "LOC_IMPROVEMENT_MINE_NAME",
      Hash = 7002, Index = 12 },
    { ImprovementType = "IMPROVEMENT_PASTURE", Name = "LOC_IMPROVEMENT_PASTURE_NAME",
      Hash = 7003, Index = 13 },
})
GameInfo.Units = make_info_table({
    { UnitType = "UNIT_BUILDER", Name = "LOC_UNIT_BUILDER_NAME", Hash = 101, Index = 1 },
    { UnitType = "UNIT_WARRIOR", Name = "LOC_UNIT_WARRIOR_NAME", Hash = 102, Index = 2 },
})

local NAMES = {
    LOC_IMPROVEMENT_FARM_NAME = "Farm",
    LOC_IMPROVEMENT_MINE_NAME = "Mine",
    LOC_IMPROVEMENT_PASTURE_NAME = "Pasture",
}
Locale = { Lookup = function(key) return NAMES[key] end }

UnitManager = {}
function UnitManager.CanStartOperation(unit, operation, plot, parameters, wantResults)
    if M.can_start_errors then error("stubbed CanStartOperation failure") end
    M.can_start_calls[#M.can_start_calls + 1] = {
        operation = operation,
        improvement = parameters and parameters[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] or nil,
        x = parameters and parameters[UnitOperationTypes.PARAM_X] or nil,
        y = parameters and parameters[UnitOperationTypes.PARAM_Y] or nil,
    }
    if operation ~= UnitOperationTypes.BUILD_IMPROVEMENT then return false end
    -- The two gates the engine itself applies, kept apart: no Build ability, no build row at all;
    -- and a builder out of charges offers nothing either.
    if unit.spec.is_builder ~= true then return false end
    if unit.spec.charges <= 0 then return false end
    local named = parameters and parameters[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE]
    if named ~= nil then
        return M.greyed[named] ~= true
    end
    if #M.offered == 0 then return false end
    return true, {
        [UnitOperationResults.IMPROVEMENTS] = M.offered,
        [UnitOperationResults.BEST_IMPROVEMENT] = M.best,
    }
end

function UnitManager.RequestOperation(unit, operation, parameters)
    M.requested = {
        operation = operation,
        unit_id = unit:GetID(),
        x = parameters[UnitOperationTypes.PARAM_X],
        y = parameters[UnitOperationTypes.PARAM_Y],
        improvement = parameters[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE],
    }
    if M.request_accepted then
        -- What a human watches happen: the charge counter drops and the tile gains the
        -- improvement.
        unit.spec.charges = unit.spec.charges - 1
        local row = GameInfo.Improvements[parameters[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE]]
        if row ~= nil then M.plot_improvement = row.Index end
    end
    return M.request_accepted
end

function UnitManager.GetReachableMovement(unit) return {} end

local function make_unit(spec)
    local unit = { spec = spec }
    function unit:GetID() return self.spec.id end
    function unit:GetOwner() return self.spec.owner end
    function unit:GetUnitType() return self.spec.unit_type_index end
    function unit:GetX() return self.spec.x end
    function unit:GetY() return self.spec.y end
    function unit:GetMovesRemaining() return self.spec.moves end
    function unit:GetMaxMoves() return 2 end
    function unit:GetFortifyTurns() return 0 end
    function unit:GetBuildCharges() return self.spec.charges end
    return unit
end

function M.builder(id, charges, moves)
    local unit = make_unit({ id = id, owner = M.local_player, unit_type_index = 1, x = 10, y = 12,
                             moves = moves or 2, charges = charges, is_builder = true })
    M.units = { unit }
    M.selected = unit
    return unit
end

function M.warrior(id)
    local unit = make_unit({ id = id, owner = M.local_player, unit_type_index = 2, x = 10, y = 12,
                             moves = 2, charges = 0, is_builder = false })
    M.units = { unit }
    M.selected = unit
    return unit
end

function M.offer(...)
    M.offered = {}
    for _, hash in ipairs({...}) do M.offered[#M.offered + 1] = hash end
end

UI = {}
function UI.GetHeadSelectedUnit() return M.selected end

Game = { GetLocalPlayer = function() return M.local_player end }

local function members(list)
    return function()
        local i = 0
        return function()
            i = i + 1
            if list[i] == nil then return nil end
            return i, list[i]
        end
    end
end

local function make_player(id)
    local player = { id = id }
    function player:GetID() return self.id end
    function player:IsMajor() return true end
    function player:GetUnits() return { Members = members(M.units) } end
    return player
end

PlayerManager = { GetAlive = function() return { make_player(M.local_player) } end }
Players = setmetatable({}, { __index = function(_, id) return make_player(id) end })
PlayersVisibility = setmetatable({}, { __index = function()
    return { IsVisible = function(_, x, y) return M.visible end }
end })
Map = {
    GetPlot = function(x, y)
        return { GetImprovementType = function() return M.plot_improvement or -1 end }
    end,
    GetPlotByIndex = function(index) return nil end,
}
return M
"""


def _runtime(source: Path) -> tuple[Any, Any]:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    stubs = runtime.execute(_STUBS)
    runtime.execute(source.read_text(encoding="utf-8"))
    return runtime, stubs


@pytest.fixture
def lua() -> tuple[Any, Any]:
    return _runtime(UNITS_LUA)


@pytest.fixture
def orders() -> tuple[Any, Any]:
    return _runtime(UNIT_ORDERS_LUA)


def _listed(value: Any) -> list[Any]:
    return list(value.values()) if hasattr(value, "values") else list(value)


def _first_unit(runtime: Any) -> dict[str, Any]:
    state = runtime.globals()["CivSim_Units"]["state"]()
    units = _listed(state["units"])
    assert len(units) == 1
    return dict(units[0])


def _builds(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(option) for option in _listed(entry["build_options"])]


# --------------------------------------------------------------------------
# units.state -- the panel's build row
# --------------------------------------------------------------------------


def test_a_selected_builder_lists_every_improvement_the_panel_offers(lua: tuple[Any, Any]) -> None:
    """unitpanel.lua:554-573: one button per improvement the plot offers, with its own label."""
    runtime, stubs = lua
    stubs.builder(65540, 3)
    stubs.offer(FARM_HASH, MINE_HASH)
    stubs.best = FARM_HASH

    entry = _first_unit(runtime)

    assert _listed(entry["available_builds"]) == ["IMPROVEMENT_FARM", "IMPROVEMENT_MINE"]
    assert _builds(entry) == [
        {
            "improvement_type": "IMPROVEMENT_FARM",
            "name": "Farm",
            "disabled": False,
            "is_recommended": True,
        },
        {
            "improvement_type": "IMPROVEMENT_MINE",
            "name": "Mine",
            "disabled": False,
            "is_recommended": False,
        },
    ]
    assert entry["charges_remaining"] == 3


def test_a_greyed_out_row_is_listed_but_is_not_offered_as_available(lua: tuple[Any, Any]) -> None:
    """unitpanel.lua:572-573 -- `isDisabled = not bCanStart` per row. The greyed button is still
    on screen, so it is still reported; it is simply not in the list the predicate reads."""
    runtime, stubs = lua
    stubs.builder(65540, 2)
    stubs.offer(FARM_HASH, MINE_HASH)
    stubs.greyed[MINE_HASH] = True

    entry = _first_unit(runtime)

    assert _listed(entry["available_builds"]) == ["IMPROVEMENT_FARM"]
    assert [option["disabled"] for option in _builds(entry)] == [False, True]


def test_a_builder_with_no_charges_left_offers_no_builds(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.builder(65540, 0)
    stubs.offer(FARM_HASH, MINE_HASH)

    entry = _first_unit(runtime)

    assert _listed(entry["available_builds"]) == []
    assert _builds(entry) == []
    assert entry["charges_remaining"] == 0


def test_a_unit_that_is_not_a_builder_offers_no_builds(lua: tuple[Any, Any]) -> None:
    """A warrior's panel has no build row at all -- and `unit.charges_remaining > 0` in the
    action's availability predicate is not the only thing standing between it and a build order."""
    runtime, stubs = lua
    stubs.warrior(65541)
    stubs.offer(FARM_HASH, MINE_HASH)

    entry = _first_unit(runtime)

    assert _listed(entry["available_builds"]) == []
    assert _builds(entry) == []


def test_a_builder_with_no_movement_left_offers_no_builds(lua: tuple[Any, Any]) -> None:
    """unitpanel.lua:545-546 -- the operation rows are listed only while the unit has moves."""
    runtime, stubs = lua
    stubs.builder(65540, 3, 0)
    stubs.offer(FARM_HASH, MINE_HASH)

    entry = _first_unit(runtime)

    assert _listed(entry["available_builds"]) == []
    assert "available_builds_reason" not in entry


def test_an_unselected_builder_is_not_asked_at_all(lua: tuple[Any, Any]) -> None:
    """The build buttons exist only on the panel of the unit a human has selected, so an
    unselected unit carries no build fields rather than a fabricated empty list."""
    runtime, stubs = lua
    stubs.builder(65540, 3)
    stubs.selected = None
    stubs.offer(FARM_HASH, MINE_HASH)

    entry = _first_unit(runtime)

    assert entry["is_selected"] is False
    assert "available_builds" not in entry
    assert "build_options" not in entry


def test_a_build_query_that_cannot_be_asked_reports_a_reason_never_a_silent_empty_list(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.builder(65540, 3)
    stubs.offer(FARM_HASH)
    stubs.can_start_errors = True

    entry = _first_unit(runtime)

    assert _listed(entry["available_builds"]) == []
    assert entry["available_builds_reason"] == "can_start_operation_unanswerable"


# --------------------------------------------------------------------------
# units.build_improvement -- the panel's build button, clicked
# --------------------------------------------------------------------------


def _build(runtime: Any, *args: Any) -> dict[str, Any]:
    return dict(runtime.globals()["CivSim_UnitOrders"]["build_improvement"](*args))


def test_the_order_is_the_operation_the_panels_button_issues(orders: tuple[Any, Any]) -> None:
    """unitpanel.lua:2548-2557 -- BUILD_IMPROVEMENT with the unit's own plot and the
    improvement's own Hash as PARAM_IMPROVEMENT_TYPE."""
    runtime, stubs = orders
    stubs.builder(65540, 3)
    stubs.offer(FARM_HASH)

    result = _build(runtime, "IMPROVEMENT_FARM")

    assert result["ok"] is True
    requested = dict(stubs.requested)
    assert requested["operation"] == 1001  # UnitOperationTypes.BUILD_IMPROVEMENT
    assert requested["improvement"] == FARM_HASH
    assert (requested["x"], requested["y"]) == (10, 12)
    assert requested["unit_id"] == 65540


def test_the_order_reads_back_the_charge_counter_and_the_tile(orders: tuple[Any, Any]) -> None:
    """The bounded read-back: the two things a human watches change. Evidence only -- the
    declaration's verification_predicate against a fresh observation is what decides `applied`."""
    runtime, stubs = orders
    stubs.builder(65540, 3)
    stubs.offer(FARM_HASH)

    result = _build(runtime, "IMPROVEMENT_FARM")

    assert result["charges_remaining"] == 2
    assert result["plot_improvement"] == "IMPROVEMENT_FARM"
    assert dict(result["plot"]) == {"x": 10, "y": 12}


def test_a_greyed_out_improvement_is_refused_and_no_operation_is_requested(
    orders: tuple[Any, Any],
) -> None:
    runtime, stubs = orders
    stubs.builder(65540, 3)
    stubs.offer(FARM_HASH, MINE_HASH)
    stubs.greyed[MINE_HASH] = True

    result = _build(runtime, "IMPROVEMENT_MINE")

    assert result["ok"] is False
    assert result["reason"] == "cannot_build_here"
    assert stubs.requested is None


def test_a_builder_with_no_charges_is_refused_by_the_panels_own_gate(
    orders: tuple[Any, Any],
) -> None:
    runtime, stubs = orders
    stubs.builder(65540, 0)
    stubs.offer(FARM_HASH)

    result = _build(runtime, "IMPROVEMENT_FARM")

    assert result["ok"] is False
    assert result["reason"] == "cannot_build_here"
    assert stubs.requested is None


def test_an_improvement_the_database_does_not_have_is_refused_before_any_order(
    orders: tuple[Any, Any],
) -> None:
    runtime, stubs = orders
    stubs.builder(65540, 3)
    stubs.offer(FARM_HASH)

    result = _build(runtime, "IMPROVEMENT_NOT_A_THING")

    assert result["ok"] is False
    assert result["reason"] == "unknown_improvement"
    assert stubs.requested is None


def test_the_order_acts_on_the_selected_unit_when_only_the_improvement_is_named(
    orders: tuple[Any, Any],
) -> None:
    """The dispatcher passes `target` as the last positional argument, so a lone string is the
    improvement and the unit is the one the game has selected (the measured found_city failure)."""
    runtime, stubs = orders
    stubs.builder(65540, 3)
    stubs.offer(FARM_HASH)

    assert _build(runtime, "IMPROVEMENT_FARM")["unit_id"] == 65540
    assert _build(runtime, 65540, "IMPROVEMENT_FARM")["unit_id"] == 65540


def test_an_order_with_no_improvement_named_is_refused(orders: tuple[Any, Any]) -> None:
    runtime, stubs = orders
    stubs.builder(65540, 3)
    stubs.offer(FARM_HASH)

    result = _build(runtime, 65540)

    assert result["ok"] is False
    assert result["reason"] == "no_improvement_named"
    assert stubs.requested is None


def test_the_dispatch_table_exposes_build_improvement(orders: tuple[Any, Any]) -> None:
    """`capability/executor.py` resolves a declaration_id's last dot-segment against this table."""
    runtime, _stubs = orders
    assert "build_improvement" in sorted(runtime.globals()["CivSim_UnitOrders"].keys())
