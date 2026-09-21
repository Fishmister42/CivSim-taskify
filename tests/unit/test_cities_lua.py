"""`lua/gamecore/cities.lua` and `lua/ingame/city_orders.lua` executed for real, against fakes
shaped like Civilization VI's own production panel.

MEASURED 2026-09-21 (399 steps, 30 runs): `cities.state.available_productions` was `[]` in every
observation on record, so `cities.set_production`'s availability predicate could never hold. The
body asked the build queue for `GetAvailableProduction()` -- a method Civ VI does not have -- under
a `pcall` that swallowed its absence, so the list came back empty and *silent*. These tests pin
the panel-faithful replacement (`GameInfo.*` enumeration + `BuildQueue:CanProduce`, the two-call
idiom at steamassets/base/assets/ui/panels/productionpanel.lua:1911-1912/2026-2038/2124-2125/
2214-2215) and, above all, pin that a build queue which cannot be read reports a *reason* rather
than a silent `[]`.

Same discipline as `test_city_selection_lua.py`: the file's logic is asserted in an embedded Lua
5.4; that the real InGame tuner state answers these calls stays UNVERIFIED LIVE and is said so in
the Lua. Skipped, not failed, where `lupa` is absent (not a project dependency):
``uv run --with lupa pytest tests/unit/test_cities_lua.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

lupa = pytest.importorskip("lupa")

REPO_ROOT = Path(__file__).resolve().parents[2]
CITIES_LUA = REPO_ROOT / "lua" / "gamecore" / "cities.lua"
CITY_ORDERS_LUA = REPO_ROOT / "lua" / "ingame" / "city_orders.lua"

# A miniature of the shipped database and of the objects the production panel calls. Two units and
# one building are producible, one unit is `MustPurchase` (the panel leaves it out of the
# production list entirely) and one building is the item already in production (likewise). The
# district requires placement, which is the case the harness lists but never offers.
_STUBS = """
local M = {
    local_player = 0,
    build_queue_present = true,
    can_produce_errors = false,
    current_hash = 0,
    queue = {},
    queue_size = 1,
    purchasable_gold = {},
    visible = true,
    cities = {},
    requested = nil,
    request_accepted = true,
}

GameInfo = {
    Units = {
        [1] = { UnitType = "UNIT_BUILDER", Name = "LOC_UNIT_BUILDER_NAME", Hash = 101, Index = 1 },
        [2] = { UnitType = "UNIT_WARRIOR", Name = "LOC_UNIT_WARRIOR_NAME", Hash = 102, Index = 2 },
        [3] = { UnitType = "UNIT_MISSIONARY", Name = "LOC_UNIT_MISSIONARY_NAME", Hash = 103,
                Index = 3, MustPurchase = true },
    },
    Buildings = {
        [1] = { BuildingType = "BUILDING_MONUMENT", Name = "LOC_BUILDING_MONUMENT_NAME",
                Hash = 201, Index = 1 },
        [2] = { BuildingType = "BUILDING_GRANARY", Name = "LOC_BUILDING_GRANARY_NAME",
                Hash = 202, Index = 2 },
    },
    Districts = {
        [1] = { DistrictType = "DISTRICT_CAMPUS", Name = "LOC_DISTRICT_CAMPUS_NAME",
                Hash = 301, Index = 1, RequiresPlacement = true },
    },
    Projects = {},
    Yields = {
        YIELD_GOLD = { Index = 1 },
        YIELD_FAITH = { Index = 4 },
    },
}

-- `for row in GameInfo.Units() do` plus `GameInfo.Units[key]` in one object, exactly as the
-- shipped GameInfo tables behave (citysupport.lua:203-206 indexes them by hash).
local function make_table(rows)
    local by_key = {}
    for _, row in ipairs(rows) do
        by_key[row.Hash] = row
        for _, field in ipairs({"UnitType", "BuildingType", "DistrictType", "ProjectType"}) do
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
for _, name in ipairs({"Units", "Buildings", "Districts", "Projects"}) do
    GameInfo[name] = make_table(GameInfo[name])
end

MilitaryFormationTypes = { STANDARD_MILITARY_FORMATION = 0 }
CityOperationTypes = {
    BUILD = 7,
    PARAM_UNIT_TYPE = "unit", PARAM_BUILDING_TYPE = "building",
    PARAM_DISTRICT_TYPE = "district", PARAM_PROJECT_TYPE = "project",
    PARAM_INSERT_MODE = "insert", VALUE_REPLACE_AT = "replace",
    PARAM_QUEUE_DESTINATION_LOCATION = "slot",
}
CityCommandTypes = {
    PURCHASE = 3,
    PARAM_UNIT_TYPE = "unit", PARAM_BUILDING_TYPE = "building",
    PARAM_YIELD_TYPE = "yield",
}

-- Producible: the two non-purchase-only units and BUILDING_MONUMENT. BUILDING_GRANARY is the
-- item already in production when M.current_hash says so. DISTRICT_CAMPUS is on the panel but
-- needs a plot.
local PRODUCIBLE = { [101] = true, [102] = true, [201] = true, [301] = true }

local build_queue = {}
function build_queue:GetCurrentProductionTypeHash() return M.current_hash end
function build_queue:GetSize() return M.queue_size end
function build_queue:GetAt(i) return M.queue[i] end
function build_queue:HasBeenPlaced(hash) return false end
function build_queue:CanProduce(argument, exclusionTest, returnResults)
    if M.can_produce_errors then error("stubbed CanProduce failure") end
    local hash = argument
    if type(argument) == "table" then hash = argument.UnitType end
    local ok = PRODUCIBLE[hash] == true
    if returnResults then return ok, {} end
    return ok
end
function build_queue:GetUnitCost(index) return 50 + index end
function build_queue:GetBuildingCost(index) return 60 + index end
function build_queue:GetDistrictCost(index) return 54 end
function build_queue:GetProjectCost(index) return 0 end
function build_queue:GetTurnsLeft(key, formation) return 5 end

Locale = { Lookup = function(key) return "display:" .. tostring(key) end }

CityManager = {}
function CityManager.CanStartCommand(city, command, testOnly, parameters, returnResults)
    return M.purchasable_gold[parameters["unit"] or parameters["building"]] == parameters["yield"]
end
function CityManager.RequestOperation(city, operation, parameters)
    M.requested = { operation = operation, parameters = parameters }
    return M.request_accepted
end
function CityManager.RequestCommand(city, command, parameters)
    M.requested = { command = command, parameters = parameters }
    return M.request_accepted
end

local function make_city(id, owner)
    local city = { id = id, owner = owner }
    function city:GetID() return self.id end
    function city:GetName() return "Pasargadae" end
    function city:GetOwner() return self.owner end
    function city:GetX() return 10 end
    function city:GetY() return 12 end
    function city:GetPopulation() return 3 end
    function city:GetBuildQueue()
        if not M.build_queue_present then error("stubbed GetBuildQueue failure") end
        return build_queue
    end
    return city
end
M.make_city = make_city

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

local function make_player(id, cities)
    local player = { id = id }
    function player:GetID() return self.id end
    function player:IsMajor() return true end
    function player:GetCities() return { Members = members(cities) } end
    function player:GetTreasury() return { GetGoldBalance = function() return 182 end } end
    return player
end

PlayerManager = {}
function PlayerManager.GetAlive()
    local players = {}
    for _, spec in ipairs(M.cities) do
        players[#players + 1] = make_player(spec.owner, spec.cities)
    end
    return players
end

Players = setmetatable({}, { __index = function(_, id)
    for _, spec in ipairs(M.cities) do
        if spec.owner == id then return make_player(id, spec.cities) end
    end
    return make_player(id, {})
end })
Game = { GetLocalPlayer = function() return M.local_player end }
Map = { GetPlot = function(x, y) return { GetX = function() return x end,
                                          GetY = function() return y end } end }
PlayersVisibility = setmetatable({}, { __index = function()
    return { IsVisible = function(_, x, y) return M.visible end }
end })

function M.own_city(id)
    M.cities = { { owner = M.local_player, cities = { make_city(id, M.local_player) } } }
end
function M.foreign_city(id, owner)
    M.cities = { { owner = owner, cities = { make_city(id, owner) } } }
end
function M.allow_purchase(hash, yieldIndex)
    M.purchasable_gold[hash] = yieldIndex
end
return M
"""


def _runtime(source: Path) -> tuple[Any, Any]:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    stubs = runtime.execute(_STUBS)
    runtime.execute(source.read_text(encoding="utf-8"))
    return runtime, stubs


@pytest.fixture
def lua() -> tuple[Any, Any]:
    return _runtime(CITIES_LUA)


@pytest.fixture
def orders() -> tuple[Any, Any]:
    return _runtime(CITY_ORDERS_LUA)


def _state(runtime: Any) -> dict[str, Any]:
    value = runtime.globals()["CivSim_Cities"]["state"]()
    cities = [dict(city) for city in value["cities"].values()]
    return {"cities": cities}


def _first_city(runtime: Any) -> dict[str, Any]:
    cities = _state(runtime)["cities"]
    assert len(cities) == 1
    city: dict[str, Any] = cities[0]
    return city


def _listed(entry: dict[str, Any], field: str) -> list[Any]:
    value = entry[field]
    return list(value.values()) if hasattr(value, "values") else list(value)


def test_an_owned_city_with_two_producible_units_and_one_building_lists_all_three(
    lua: tuple[Any, Any],
) -> None:
    """The measured bug, inverted: the list is filled, and it is the production panel's own."""
    runtime, stubs = lua
    stubs.own_city(65538)

    city = _first_city(runtime)

    assert city["owner_is_local_player"] is True
    assert sorted(_listed(city, "available_productions")) == [
        "BUILDING_MONUMENT",
        "UNIT_BUILDER",
        "UNIT_WARRIOR",
    ]
    assert "available_productions_reason" not in city


def test_each_option_carries_what_the_panel_button_shows_and_nothing_else(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.own_city(65538)

    rows = _listed(_first_city(runtime), "production_options")
    options = {dict(o)["type"]: dict(o) for o in rows}

    assert set(options) == {
        "UNIT_BUILDER",
        "UNIT_WARRIOR",
        "BUILDING_MONUMENT",
        "DISTRICT_CAMPUS",
    }
    builder = options["UNIT_BUILDER"]
    assert builder["kind"] == "unit"
    assert builder["name"] == "display:LOC_UNIT_BUILDER_NAME"
    assert builder["production_required"] == 51
    assert builder["turns"] == 5
    assert builder["disabled"] is False
    assert set(builder) == {
        "type",
        "name",
        "kind",
        "production_required",
        "turns",
        "disabled",
        "requires_placement",
    }


def test_a_purchase_only_unit_is_not_on_the_production_list(lua: tuple[Any, Any]) -> None:
    """productionpanel.lua:2124 -- `if not row.MustPurchase and ...`."""
    runtime, stubs = lua
    stubs.own_city(65538)

    types = {dict(o)["type"] for o in _listed(_first_city(runtime), "production_options")}

    assert "UNIT_MISSIONARY" not in types


def test_a_district_that_needs_a_plot_is_listed_but_never_offered(lua: tuple[Any, Any]) -> None:
    """A human sees the Campus button; their next act is a click on a map plot, which the harness
    cannot make -- so it is reported and withheld, never silently dropped."""
    runtime, stubs = lua
    stubs.own_city(65538)

    city = _first_city(runtime)
    campus = next(
        dict(o) for o in _listed(city, "production_options") if dict(o)["type"] == "DISTRICT_CAMPUS"
    )

    assert campus["requires_placement"] is True
    assert "DISTRICT_CAMPUS" not in _listed(city, "available_productions")


def test_the_item_already_in_production_leaves_the_list_and_heads_the_queue(
    lua: tuple[Any, Any],
) -> None:
    """productionpanel.lua:2037 -- a building already in production is not offered again;
    :1894 -- it is what `GetCurrentProductionTypeHash` names."""
    runtime, stubs = lua
    stubs.own_city(65538)
    stubs.current_hash = 201  # BUILDING_MONUMENT

    city = _first_city(runtime)

    assert "BUILDING_MONUMENT" not in _listed(city, "available_productions")
    assert _listed(city, "production_queue") == ["BUILDING_MONUMENT"]


def test_a_city_the_player_does_not_own_yields_nothing_about_its_production(
    lua: tuple[Any, Any],
) -> None:
    runtime, stubs = lua
    stubs.foreign_city(70001, 3)

    city = _first_city(runtime)

    assert city["owner_is_local_player"] is False
    assert "available_productions" not in city
    assert "production_options" not in city
    assert "production_queue" not in city
    assert "gold_available" not in city


def test_an_absent_build_queue_yields_an_empty_list_with_a_reason_never_a_silent_empty(
    lua: tuple[Any, Any],
) -> None:
    """The exact shape of the measured failure: `[]` with nothing said about why."""
    runtime, stubs = lua
    stubs.own_city(65538)
    stubs.build_queue_present = False

    city = _first_city(runtime)

    assert _listed(city, "available_productions") == []
    assert "GetBuildQueue" in city["available_productions_reason"]


def test_a_build_queue_that_cannot_answer_canproduce_says_so(lua: tuple[Any, Any]) -> None:
    runtime, stubs = lua
    stubs.own_city(65538)
    stubs.can_produce_errors = True

    city = _first_city(runtime)

    assert _listed(city, "available_productions") == []
    assert "CanProduce" in city["available_productions_reason"]


def test_purchasability_is_the_panels_own_canstartcommand_gate(lua: tuple[Any, Any]) -> None:
    """productionpanel.lua:1637-1639 -- PARAM_UNIT_TYPE (the hash) + PARAM_YIELD_TYPE."""
    runtime, stubs = lua
    stubs.own_city(65538)
    stubs.allow_purchase(101, 1)  # UNIT_BUILDER, YIELD_GOLD

    city = _first_city(runtime)

    assert _listed(city, "purchasable_with_gold") == ["UNIT_BUILDER"]
    assert city["can_buy_with_gold"] is True
    assert _listed(city, "purchasable_with_faith") == []
    assert city["can_buy_with_faith"] is False


# --------------------------------------------------------------------------
# The order the panel's click issues.
# --------------------------------------------------------------------------


def test_set_production_issues_the_panels_own_build_operation(orders: tuple[Any, Any]) -> None:
    """productionpanel.lua:299-301 (PARAM_UNIT_TYPE = the item's *hash*) and :2896-2897 (an
    ordinary click REPLACES the head of the queue)."""
    runtime, stubs = orders
    stubs.own_city(65538)

    result = dict(runtime.globals()["CivSim_CityOrders"]["set_production"](65538, "UNIT_BUILDER"))

    assert result["ok"] is True
    assert result["production"] == "UNIT_BUILDER"
    assert result["kind"] == "unit"
    requested = dict(stubs.requested)
    assert requested["operation"] == 7  # CityOperationTypes.BUILD
    parameters = dict(requested["parameters"])
    assert parameters["unit"] == 101  # PARAM_UNIT_TYPE = UNIT_BUILDER's hash
    assert parameters["insert"] == "replace"
    assert parameters["slot"] == 0


def test_set_production_names_a_building_by_its_own_parameter_key(
    orders: tuple[Any, Any],
) -> None:
    runtime, stubs = orders
    stubs.own_city(65538)

    result = dict(
        runtime.globals()["CivSim_CityOrders"]["set_production"](65538, "BUILDING_MONUMENT")
    )

    assert result["ok"] is True
    parameters = dict(dict(stubs.requested)["parameters"])
    assert parameters["building"] == 201
    assert "unit" not in parameters


def test_set_production_refuses_an_item_that_would_open_plot_placement(
    orders: tuple[Any, Any],
) -> None:
    runtime, stubs = orders
    stubs.own_city(65538)

    set_production = runtime.globals()["CivSim_CityOrders"]["set_production"]
    result = dict(set_production(65538, "DISTRICT_CAMPUS"))

    assert result["ok"] is False
    assert result["reason"] == "requires_plot_placement"
    assert stubs.requested is None


def test_set_production_refuses_an_item_that_is_in_no_game_table(
    orders: tuple[Any, Any],
) -> None:
    runtime, stubs = orders
    stubs.own_city(65538)

    result = dict(runtime.globals()["CivSim_CityOrders"]["set_production"](65538, "UNIT_NONSENSE"))

    assert result["ok"] is False
    assert result["reason"] == "unknown_production_item"
    assert stubs.requested is None


def test_purchase_with_gold_names_the_item_by_hash_and_the_yield_by_index(
    orders: tuple[Any, Any],
) -> None:
    """productionpanel.lua:425-428; `PARAM_PRODUCTION_ITEM`, which this file used to write, is not
    a parameter key Civilization VI has."""
    runtime, stubs = orders
    stubs.own_city(65538)

    result = dict(
        runtime.globals()["CivSim_CityOrders"]["purchase_with_gold"](65538, "UNIT_BUILDER")
    )

    assert result["ok"] is True
    assert result["currency"] == "gold"
    parameters = dict(dict(stubs.requested)["parameters"])
    assert parameters["unit"] == 101
    assert parameters["yield"] == 1


def test_an_order_for_a_city_the_player_does_not_own_is_refused(orders: tuple[Any, Any]) -> None:
    runtime, stubs = orders
    stubs.own_city(65538)

    result = dict(runtime.globals()["CivSim_CityOrders"]["set_production"](99999, "UNIT_BUILDER"))

    assert result["ok"] is False
    assert result["reason"] == "city_not_found"
