"""`units.promote`: the promotion name reaches the Lua as a promotion, not as a unit id.

MEASURED (2026-09-21, reading the dispatch path): `units.promote` could never have worked.
`act/executor.py`'s `_build_arguments` passes a decision's `target` as the LAST positional
argument, and this action's target is the promotion *name*, so every dispatch arrived at
`lua/ingame/unit_orders.lua` as `promote("PROMOTION_BATTLECRY")` -- the name landed in the
`unitId` parameter, `CivSim_FindLocalUnit` compared unit ids against a string, and the order came
back `unit_not_found` for a unit that was selected and standing right there. `move_to` already
carried the normalisation guard for exactly this shape; `promote` did not.

The body was a second, separate guess: `unit:SetPromotion(...)` appears nowhere in Firaxis'
shipped UI. The real path is UnitManager.RequestCommand with UnitCommandTypes.PROMOTE and
PARAM_PROMOTION_TYPE (unitpanel.lua:2708-2717), whose value is the promotion row's own Index
(unitpromotionpopup.lua:285-291), listed from CanStartCommand's UnitCommandResults.PROMOTIONS
(unitpanel.lua:435-446).

These tests run the production argument-marshalling (`act/executor.ActionExecutor`) against the
real shipped catalog and feed the arguments it produces straight into the real Lua, executed in an
embedded Lua 5.4. Skipped, not failed, where `lupa` is absent (not a project dependency):
``uv run --with lupa pytest tests/unit/test_promote_dispatch.py``.
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
UNITS_LUA = REPO_ROOT / "lua" / "gamecore" / "units.lua"
UNIT_ORDERS_LUA = REPO_ROOT / "lua" / "ingame" / "unit_orders.lua"

PROMOTE = DeclarationId("units.promote")
BATTLECRY_INDEX = 21
SELECTED_UNIT_ID = 65542

# The promotion tree as the game holds it, and the two calls its panel makes. `PROMOTIONS` carries
# promotion row *Indices* (unitpromotionpopup.lua:285-291), which is what makes "the name arrives
# where the id was expected" a silent failure rather than a loud one.
_STUBS = """
local M = {
    local_player = 0,
    units = {},
    selected = nil,
    offered = {},
    can_start = true,
    can_start_errors = false,
    requested = nil,
    request_accepted = true,
    visible = true,
}

UnitCommandTypes = { PROMOTE = 2001, PARAM_PROMOTION_TYPE = "param_promotion" }
UnitCommandResults = { PROMOTIONS = "promotions" }
UnitOperationTypes = {
    BUILD_IMPROVEMENT = 1001, FOUND_CITY = 1002, MOVE_TO = 1003,
    PARAM_X = "param_x", PARAM_Y = "param_y", PARAM_IMPROVEMENT_TYPE = "param_improvement",
}
UnitOperationResults = { IMPROVEMENTS = "improvements", BEST_IMPROVEMENT = "best_improvement" }

local function make_info_table(rows)
    local by_key = {}
    for _, row in ipairs(rows) do
        by_key[row.Index] = row
        if row.Hash ~= nil then by_key[row.Hash] = row end
        for _, field in ipairs({"UnitPromotionType", "UnitType"}) do
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
GameInfo.UnitPromotions = make_info_table({
    { UnitPromotionType = "PROMOTION_BATTLECRY", Name = "LOC_PROMOTION_BATTLECRY_NAME",
      Index = 21 },
    { UnitPromotionType = "PROMOTION_TORTOISE", Name = "LOC_PROMOTION_TORTOISE_NAME",
      Index = 22 },
})
GameInfo.Units = make_info_table({
    { UnitType = "UNIT_WARRIOR", Name = "LOC_UNIT_WARRIOR_NAME", Hash = 102, Index = 2 },
})
GameInfo.Improvements = make_info_table({})

Locale = { Lookup = function(key) return key end }

UnitManager = {}
function UnitManager.CanStartCommand(unit, command, testOnly, returnResults)
    if M.can_start_errors then error("stubbed CanStartCommand failure") end
    if command ~= UnitCommandTypes.PROMOTE then return false end
    if not M.can_start then return false end
    if returnResults then
        return true, { [UnitCommandResults.PROMOTIONS] = M.offered }
    end
    return true
end

function UnitManager.RequestCommand(unit, command, parameters)
    M.requested = {
        command = command,
        unit_id = unit:GetID(),
        promotion = parameters[UnitCommandTypes.PARAM_PROMOTION_TYPE],
    }
    return M.request_accepted
end

function UnitManager.CanStartOperation(unit, operation, plot, parameters, wantResults)
    return false
end
function UnitManager.GetReachableMovement(unit) return {} end

local function make_unit(id)
    local unit = { id = id }
    function unit:GetID() return self.id end
    function unit:GetOwner() return M.local_player end
    function unit:GetUnitType() return 2 end
    function unit:GetX() return 10 end
    function unit:GetY() return 12 end
    function unit:GetMovesRemaining() return 2 end
    function unit:GetMaxMoves() return 2 end
    function unit:GetFortifyTurns() return 0 end
    function unit:GetBuildCharges() return 0 end
    return unit
end

function M.warrior(id)
    local unit = make_unit(id)
    M.units = { unit }
    M.selected = unit
    return unit
end

function M.offer(...)
    M.offered = {}
    for _, index in ipairs({...}) do M.offered[#M.offered + 1] = index end
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
    GetPlot = function(x, y) return { GetImprovementType = function() return -1 end } end,
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
def orders() -> tuple[Any, Any]:
    return _runtime(UNIT_ORDERS_LUA)


@pytest.fixture
def state() -> tuple[Any, Any]:
    return _runtime(UNITS_LUA)


def _listed(value: Any) -> list[Any]:
    return list(value.values()) if hasattr(value, "values") else list(value)


def _promote(runtime: Any, *args: Any) -> dict[str, Any]:
    return dict(runtime.globals()["CivSim_UnitOrders"]["promote"](*args))


# --------------------------------------------------------------------------
# What the production dispatcher actually hands the Lua, for the real declaration
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


async def _dispatch_arguments(target: Any) -> tuple[Any, ...]:
    registry = CapabilityRegistry(catalog=load_catalog(CATALOG_ROOT))
    recorder = _RecordingCapabilityExecutor()
    executor = ActionExecutor(executor=cast(CapabilityExecutor, recorder), registry=registry)
    await executor(PROMOTE, {"target": target}, target)
    return recorder.arguments


async def test_the_promotion_name_is_the_only_argument_the_dispatcher_sends() -> None:
    """The bug's root, pinned against the real catalog: one positional argument, a string."""
    assert await _dispatch_arguments("PROMOTION_BATTLECRY") == ("PROMOTION_BATTLECRY",)


async def test_that_argument_shape_promotes_the_selected_unit(orders: tuple[Any, Any]) -> None:
    """End to end: the production dispatcher's own arguments, fed to the real Lua."""
    runtime, stubs = orders
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.offer(BATTLECRY_INDEX)

    arguments = await _dispatch_arguments("PROMOTION_BATTLECRY")
    result = _promote(runtime, *arguments)

    assert result["ok"] is True
    assert result["unit_id"] == SELECTED_UNIT_ID
    assert result["promotion"] == "PROMOTION_BATTLECRY"
    assert result.get("reason") is None


def test_the_command_is_the_one_the_promotion_slot_issues(orders: tuple[Any, Any]) -> None:
    """unitpanel.lua:2708-2717 -- PROMOTE with PARAM_PROMOTION_TYPE, whose value is the promotion
    row's own Index (unitpromotionpopup.lua:285-291), never its name."""
    runtime, stubs = orders
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.offer(BATTLECRY_INDEX)

    _promote(runtime, "PROMOTION_BATTLECRY")

    requested = dict(stubs.requested)
    assert requested["command"] == 2001  # UnitCommandTypes.PROMOTE
    assert requested["promotion"] == BATTLECRY_INDEX
    assert requested["unit_id"] == SELECTED_UNIT_ID


def test_the_unit_may_still_be_named_explicitly(orders: tuple[Any, Any]) -> None:
    runtime, stubs = orders
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.offer(BATTLECRY_INDEX)

    assert _promote(runtime, SELECTED_UNIT_ID, "PROMOTION_BATTLECRY")["ok"] is True


# --------------------------------------------------------------------------
# Wrong shapes are refused with their own reason, never a misleading one
# --------------------------------------------------------------------------


def test_an_order_naming_only_a_unit_says_so_rather_than_promoting_something(
    orders: tuple[Any, Any],
) -> None:
    """The old shape inverted: a bare unit id is not a promotion, and is refused as such."""
    runtime, stubs = orders
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.offer(BATTLECRY_INDEX)

    result = _promote(runtime, SELECTED_UNIT_ID)

    assert result["ok"] is False
    assert result["reason"] == "no_promotion_named"
    assert result["unit_id"] == SELECTED_UNIT_ID
    assert stubs.requested is None


def test_a_promotion_the_tree_does_not_have_is_refused_before_any_command(
    orders: tuple[Any, Any],
) -> None:
    runtime, stubs = orders
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.offer(BATTLECRY_INDEX)

    result = _promote(runtime, "PROMOTION_NOT_A_THING")

    assert result["ok"] is False
    assert result["reason"] == "unknown_promotion"
    assert stubs.requested is None


def test_a_promotion_the_unit_is_not_offered_is_refused_by_the_panels_own_gate(
    orders: tuple[Any, Any],
) -> None:
    runtime, stubs = orders
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.offer(BATTLECRY_INDEX)

    result = _promote(runtime, "PROMOTION_TORTOISE")

    assert result["ok"] is False
    assert result["reason"] == "promotion_not_available"
    assert stubs.requested is None


def test_a_unit_that_cannot_promote_at_all_is_refused(orders: tuple[Any, Any]) -> None:
    runtime, stubs = orders
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.can_start = False

    result = _promote(runtime, "PROMOTION_BATTLECRY")

    assert result["ok"] is False
    assert result["reason"] == "promotion_not_available"
    assert stubs.requested is None


def test_with_nothing_selected_the_order_still_says_unit_not_found(
    orders: tuple[Any, Any],
) -> None:
    """The old answer, now reserved for what it actually means."""
    runtime, stubs = orders
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.selected = None

    result = _promote(runtime, "PROMOTION_BATTLECRY")

    assert result["ok"] is False
    assert result["reason"] == "unit_not_found"


# --------------------------------------------------------------------------
# The list the predicate reads
# --------------------------------------------------------------------------


def test_available_promotions_is_the_panels_own_list_by_name(state: tuple[Any, Any]) -> None:
    """The second half of the same bug: the body asked `unit:GetAvailablePromotions()`, which Civ
    VI does not have, so this was `[]` on every record in the store."""
    runtime, stubs = state
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.offer(BATTLECRY_INDEX, 22)

    entry = dict(_listed(runtime.globals()["CivSim_Units"]["state"]()["units"])[0])

    assert _listed(entry["available_promotions"]) == ["PROMOTION_BATTLECRY", "PROMOTION_TORTOISE"]


def test_a_unit_with_nothing_to_promote_lists_nothing(state: tuple[Any, Any]) -> None:
    runtime, stubs = state
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.can_start = False

    entry = dict(_listed(runtime.globals()["CivSim_Units"]["state"]()["units"])[0])

    assert _listed(entry["available_promotions"]) == []
    assert "available_promotions_reason" not in entry


def test_a_promotion_query_that_cannot_be_asked_reports_a_reason(state: tuple[Any, Any]) -> None:
    runtime, stubs = state
    stubs.warrior(SELECTED_UNIT_ID)
    stubs.can_start_errors = True

    entry = dict(_listed(runtime.globals()["CivSim_Units"]["state"]()["units"])[0])

    assert _listed(entry["available_promotions"]) == []
    assert entry["available_promotions_reason"] == "can_start_command_unanswerable"
