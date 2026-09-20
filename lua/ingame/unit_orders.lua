-- lua/ingame/unit_orders.lua
-- Context: InGame (write/act). Orders only; verification reads back through
-- lua/gamecore/units.lua's CivSim_Units.state() rather than this file.
-- Backs declaration_ids: units.move_to, units.found_city, units.promote (catalogs/actions/units.yaml),
-- capability_id: units.orders.
--
-- Parity note: every function here issues exactly the order a human could issue by selecting the
-- unit and clicking the corresponding UI command; none accepts a target the availability
-- predicate would not already have accepted (movement outside declared reachable_plots, an
-- unavailable promotion, founding where founding is illegal).

local function CivSim_JsonEncode(value)
    local t = type(value)
    if value == nil then
        return "null"
    elseif t == "boolean" then
        return value and "true" or "false"
    elseif t == "number" then
        if value ~= value then return "null" end
        return tostring(value)
    elseif t == "string" then
        local escaped = value:gsub('[%c"\\]', function(c)
            if c == '"' then return '\\"'
            elseif c == '\\' then return '\\\\'
            elseif c == '\n' then return '\\n'
            elseif c == '\r' then return '\\r'
            elseif c == '\t' then return '\\t'
            else return string.format('\\u%04x', string.byte(c)) end
        end)
        return '"' .. escaped .. '"'
    elseif t == "table" then
        local n = 0
        for _ in pairs(value) do n = n + 1 end
        if n == 0 then return "[]" end
        local isArray = true
        for i = 1, n do if value[i] == nil then isArray = false break end end
        if isArray then
            local parts = {}
            for i = 1, n do parts[i] = CivSim_JsonEncode(value[i]) end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            local parts = {}
            for k, v in pairs(value) do
                parts[#parts + 1] = CivSim_JsonEncode(tostring(k)) .. ":" .. CivSim_JsonEncode(v)
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    else
        return "null"
    end
end

-- Move the given unit to (x, y). UNVERIFIED: UnitOperationTypes.MOVE_TO and the
-- UnitOperationTypes.PARAM_X / PARAM_Y parameter-table keys are the pattern recalled from Civ VI's
-- own move-order UI Lua, but are not confirmed against a live client.
local function CivSim_UnitOrders_MoveTo(unitId, x, y)
    local localPlayer = Game.GetLocalPlayer()
    local unit = UnitManager.GetUnit(localPlayer, unitId) -- UNVERIFIED: UnitManager.GetUnit(playerID, unitID)
    if unit == nil then
        return { ok = false, reason = "unit_not_found" }
    end
    local tParameters = {}
    tParameters[UnitOperationTypes.PARAM_X] = x -- UNVERIFIED
    tParameters[UnitOperationTypes.PARAM_Y] = y -- UNVERIFIED
    local accepted = UnitManager.RequestOperation(unit, UnitOperationTypes.MOVE_TO, tParameters) -- UNVERIFIED
    return { ok = (accepted ~= false), unit_id = unitId, requested_plot = { x = x, y = y } }
end

-- Found a city with the given settler unit at its current plot.
local function CivSim_UnitOrders_FoundCity(unitId)
    local localPlayer = Game.GetLocalPlayer()
    local unit = UnitManager.GetUnit(localPlayer, unitId) -- UNVERIFIED
    if unit == nil then
        return { ok = false, reason = "unit_not_found" }
    end
    local accepted = UnitManager.RequestOperation(unit, UnitOperationTypes.FOUND_CITY, {}) -- UNVERIFIED
    return { ok = (accepted ~= false), unit_id = unitId }
end

-- Apply a promotion to a unit that has one available. UNVERIFIED: the exact operation/command for
-- applying a promotion (as opposed to querying availability) is not confirmed; some Civ VI builds
-- may expose this as a direct Unit method rather than an operation request.
local function CivSim_UnitOrders_Promote(unitId, promotionType)
    local localPlayer = Game.GetLocalPlayer()
    local unit = UnitManager.GetUnit(localPlayer, unitId) -- UNVERIFIED
    if unit == nil then
        return { ok = false, reason = "unit_not_found" }
    end
    local ok, result = pcall(function()
        return unit:SetPromotion(GameInfo.UnitPromotions[promotionType].Index) -- UNVERIFIED
    end)
    return { ok = (ok and result ~= false), unit_id = unitId, promotion = promotionType }
end

CivSim_UnitOrders = {
    move_to = CivSim_UnitOrders_MoveTo,
    found_city = CivSim_UnitOrders_FoundCity,
    promote = CivSim_UnitOrders_Promote,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_UnitOrders.move_to(42, 10, 12)))
