"""`cities.set_production`: the item name reaches the Lua as an item, not as a city id.

MEASURED LIVE 2026-09-21 (Stage 5, run-ba3ad80d, 24 of 24): with the list filled, the insert mode
fixed and a 4 s bounded re-read in place, every `cities.set_production` was still rejected with
`production_queue` `[]` on every read -- while a labelled probe issuing this file's own parameter
table through `CityManager.RequestOperation` filled the queue with UNIT_BUILDER inside a second,
and the harness's own `cities.state` then read `["UNIT_BUILDER"]` back. The game accepted the order
and the observation could see it; the harness never sent it.

`act/executor.py`'s `_build_arguments` passes a decision's `target` as the LAST positional
argument, and a city order's target is the ITEM, so every dispatch arrived as
`set_production("UNIT_BUILDER")`: the item name landed in the `cityId` parameter,
`CivSim_FindLocalCity` compared city ids against a string, and the order came back `city_not_found`
for a city that was selected with its panel open. Exactly the shape of the `units.promote` bug
(6606d4b) -- `lua/ingame/unit_orders.lua:107` carried the normalisation guard,
`lua/ingame/city_orders.lua` did not.

Like `test_promote_dispatch.py`, these run the production argument-marshalling
(`act/executor.ActionExecutor`) against the real shipped catalog and feed the arguments it produces
straight into the real Lua, executed in an embedded Lua 5.4. Skipped, not failed, where `lupa` is
absent: ``uv run --with lupa pytest tests/unit/test_set_production_dispatch.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from civsim_harness.act.executor import ActionExecutor
from civsim_harness.capability.executor import CapabilityExecutor
from civsim_harness.capability.loader import load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.models.common import DeclarationId, LuaContext
from civsim_harness.observe.assemble import CapabilityResult

lupa = pytest.importorskip("lupa")

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"
CITY_ORDERS_LUA = REPO_ROOT / "lua" / "ingame" / "city_orders.lua"

SET_PRODUCTION = DeclarationId("cities.set_production")
PURCHASE_WITH_GOLD = DeclarationId("cities.purchase_with_gold")
SELECTED_CITY_ID = 65538
BUILDER_HASH = 101

_STUBS = """
local M = {
    local_player = 0,
    cities = {},
    selected = nil,
    requested = nil,
    current_hash = 0,
}

CityOperationTypes = {
    BUILD = 7,
    PARAM_UNIT_TYPE = "unit", PARAM_BUILDING_TYPE = "building",
    PARAM_DISTRICT_TYPE = "district", PARAM_PROJECT_TYPE = "project",
    PARAM_INSERT_MODE = "insert", VALUE_EXCLUSIVE = "exclusive",
}
CityCommandTypes = {
    PURCHASE = 3,
    PARAM_UNIT_TYPE = "unit", PARAM_BUILDING_TYPE = "building", PARAM_YIELD_TYPE = "yield",
}
CityOperationResults = { FAILURE_REASONS = "failure_reasons" }
CityCommandResults = { FAILURE_REASONS = "failure_reasons" }

local function make_info_table(rows)
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

GameInfo = {}
GameInfo.Units = make_info_table({
    { UnitType = "UNIT_BUILDER", Name = "LOC_UNIT_BUILDER_NAME", Hash = 101, Index = 1 },
})
GameInfo.Buildings = make_info_table({
    { BuildingType = "BUILDING_MONUMENT", Name = "LOC_BUILDING_MONUMENT_NAME",
      Hash = 201, Index = 1 },
})
GameInfo.Districts = make_info_table({})
GameInfo.Projects = make_info_table({})
GameInfo.Yields = { YIELD_GOLD = { Index = 1 }, YIELD_FAITH = { Index = 4 } }

Locale = { Lookup = function(key) return key end }

local build_queue = {}
function build_queue:GetCurrentProductionTypeHash() return M.current_hash end
function build_queue:HasBeenPlaced(hash) return false end

CityManager = {}
function CityManager.CanStartOperation(city, operation, parameters, returnResults)
    return true, {}
end
function CityManager.RequestOperation(city, operation, parameters)
    M.requested = { operation = operation, parameters = parameters, city_id = city:GetID() }
    M.current_hash = parameters["unit"] or parameters["building"] or 0
end
function CityManager.CanStartCommand(city, command, testOnly, parameters, returnResults)
    return true
end
function CityManager.RequestCommand(city, command, parameters)
    M.requested = { command = command, parameters = parameters, city_id = city:GetID() }
end

local function make_city(id, owner)
    local city = { id = id, owner = owner }
    function city:GetID() return self.id end
    function city:GetOwner() return self.owner end
    function city:GetBuildQueue() return build_queue end
    return city
end

function M.city(id)
    -- The owner is fixed at creation, so a test can move the LOCAL player without the city
    -- following it -- which is how "the selected city belongs to someone else" is expressed.
    local city = make_city(id, M.local_player)
    M.cities = { city }
    M.selected = city
    return city
end

UI = {}
function UI.GetHeadSelectedCity() return M.selected end

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
    function player:GetCities() return { Members = members(M.cities) } end
    return player
end

Players = setmetatable({}, { __index = function(_, id) return make_player(id) end })
return M
"""


@pytest.fixture
def orders() -> tuple[Any, Any]:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    stubs = runtime.execute(_STUBS)
    runtime.execute(CITY_ORDERS_LUA.read_text(encoding="utf-8"))
    return runtime, stubs


def _call(runtime: Any, name: str, *args: Any) -> dict[str, Any]:
    return dict(runtime.globals()["CivSim_CityOrders"][name](*args))


# --------------------------------------------------------------------------
# What the production dispatcher actually hands the Lua, for the real declarations
# --------------------------------------------------------------------------


class _RecordingCapabilityExecutor:
    """The T206 seam: performs nothing, records the positional arguments it was handed."""

    def __init__(self) -> None:
        self.arguments: tuple[Any, ...] = ()

    async def execute(
        self,
        declaration_id: DeclarationId,
        *,
        context: LuaContext,
        arguments: Sequence[Any] = (),
    ) -> CapabilityResult:
        self.arguments = tuple(arguments)
        return CapabilityResult(declaration_id=declaration_id, value={"ok": True})


async def _dispatch_arguments(
    declaration_id: DeclarationId, target: Any
) -> tuple[Any, ...]:
    registry = CapabilityRegistry(catalog=load_catalog(CATALOG_ROOT))
    recorder = _RecordingCapabilityExecutor()
    executor = ActionExecutor(executor=cast(CapabilityExecutor, recorder), registry=registry)
    await executor(declaration_id, {"target": target}, target)
    return recorder.arguments


async def test_the_item_name_is_the_only_argument_the_dispatcher_sends() -> None:
    """The bug's root, pinned against the real catalog: one positional argument, a string."""
    assert await _dispatch_arguments(SET_PRODUCTION, "UNIT_BUILDER") == ("UNIT_BUILDER",)


async def test_that_argument_shape_sets_the_selected_citys_production(
    orders: tuple[Any, Any],
) -> None:
    """End to end: the production dispatcher's own arguments, fed to the real Lua. This is the
    assertion that was missing -- `test_cities_lua.py` called `set_production(cityId, type)`, the
    two-argument form nothing in the harness ever sends."""
    runtime, stubs = orders
    stubs.city(SELECTED_CITY_ID)

    arguments = await _dispatch_arguments(SET_PRODUCTION, "UNIT_BUILDER")
    result = _call(runtime, "set_production", *arguments)

    assert result["ok"] is True
    assert result["city_id"] == SELECTED_CITY_ID
    assert result["production"] == "UNIT_BUILDER"
    assert result["confirmed"] is True
    requested = dict(stubs.requested)
    assert requested["city_id"] == SELECTED_CITY_ID
    assert dict(requested["parameters"]) == {"unit": BUILDER_HASH, "insert": "exclusive"}


async def test_the_same_shape_reaches_purchase_with_gold(orders: tuple[Any, Any]) -> None:
    """The other two city orders take the identical argument shape and had the identical bug."""
    runtime, stubs = orders
    stubs.city(SELECTED_CITY_ID)

    arguments = await _dispatch_arguments(PURCHASE_WITH_GOLD, "UNIT_BUILDER")
    assert arguments == ("UNIT_BUILDER",)
    result = _call(runtime, "purchase_with_gold", *arguments)

    assert result["ok"] is True
    assert result["city_id"] == SELECTED_CITY_ID
    assert dict(dict(stubs.requested)["parameters"]) == {"unit": BUILDER_HASH, "yield": 1}


def test_the_city_may_still_be_named_explicitly(orders: tuple[Any, Any]) -> None:
    runtime, stubs = orders
    stubs.city(SELECTED_CITY_ID)

    result = _call(runtime, "set_production", SELECTED_CITY_ID, "UNIT_BUILDER")

    assert result["ok"] is True
    assert result["city_id"] == SELECTED_CITY_ID


def test_an_order_naming_only_a_city_says_so_rather_than_producing_something(
    orders: tuple[Any, Any],
) -> None:
    """The old shape inverted: a bare city id is not an item, and is refused as such."""
    runtime, stubs = orders
    stubs.city(SELECTED_CITY_ID)

    result = _call(runtime, "set_production", SELECTED_CITY_ID)

    assert result["ok"] is False
    assert result["reason"] == "no_production_named"
    assert result["city_id"] == SELECTED_CITY_ID
    assert stubs.requested is None


def test_with_no_city_selected_the_order_still_says_city_not_found(
    orders: tuple[Any, Any],
) -> None:
    """The old answer, now reserved for what it actually means."""
    runtime, stubs = orders
    stubs.city(SELECTED_CITY_ID)
    stubs.selected = None

    result = _call(runtime, "set_production", "UNIT_BUILDER")

    assert result["ok"] is False
    assert result["reason"] == "city_not_found"
    assert stubs.requested is None


def test_a_selected_city_another_player_owns_is_not_acted_on(orders: tuple[Any, Any]) -> None:
    """`UI.GetHeadSelectedCity()` can name a city the player does not own; a city order never
    acts on one (catalogs/actions/cities.yaml's parity rule)."""
    runtime, stubs = orders
    stubs.city(SELECTED_CITY_ID)
    stubs.local_player = 3  # the selected city's owner is still 0

    result = _call(runtime, "set_production", "UNIT_BUILDER")

    assert result["ok"] is False
    assert result["reason"] == "city_not_found"
    assert stubs.requested is None
