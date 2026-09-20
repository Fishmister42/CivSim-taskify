-- lua/ingame/city_orders.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/cities.lua's
-- CivSim_Cities.state() rather than this file.
-- Backs declaration_ids: cities.set_production, cities.purchase_with_gold,
-- cities.purchase_with_faith (catalogs/actions/cities.yaml), capability_id: cities.orders.
--
-- Parity note: production and purchase choices are restricted to what CivSim_Cities.state()
-- already reported as available_productions / purchasable_with_gold / purchasable_with_faith for
-- that city — exactly the items the standard production/purchase panel would show as choosable.

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

-- UNVERIFIED: CityManager.RequestOperation with CityOperationTypes.BUILD and a
-- CityOperationTypes.PARAM_* parameter table is the pattern recalled from Civ VI's own production
-- panel Lua, but the exact enum members and parameter keys are not confirmed.
local function CivSim_CityOrders_SetProduction(cityId, productionType)
    local localPlayer = Game.GetLocalPlayer()
    local city = CityManager.GetCity(localPlayer, cityId) -- UNVERIFIED: CityManager.GetCity(playerID, cityID)
    if city == nil then
        return { ok = false, reason = "city_not_found" }
    end
    local tParameters = {}
    tParameters[CityOperationTypes.PARAM_PRODUCTION_ITEM] = productionType -- UNVERIFIED
    local accepted = CityManager.RequestOperation(city, CityOperationTypes.BUILD, tParameters) -- UNVERIFIED
    return { ok = (accepted ~= false), city_id = cityId, production = productionType }
end

-- UNVERIFIED: CityManager.RequestCommand with CityCommandTypes.PURCHASE is the pattern recalled
-- from Civ VI's own purchase panel Lua, but the exact command name and parameter keys are not
-- confirmed.
local function CivSim_CityOrders_PurchaseWithGold(cityId, itemType)
    local localPlayer = Game.GetLocalPlayer()
    local city = CityManager.GetCity(localPlayer, cityId) -- UNVERIFIED
    if city == nil then
        return { ok = false, reason = "city_not_found" }
    end
    local tParameters = {}
    tParameters[CityCommandTypes.PARAM_PRODUCTION_ITEM] = itemType -- UNVERIFIED
    tParameters[CityCommandTypes.PARAM_YIELD_TYPE] = GameInfo.Yields["YIELD_GOLD"].Index -- UNVERIFIED
    local accepted = CityManager.RequestCommand(city, CityCommandTypes.PURCHASE, tParameters) -- UNVERIFIED
    return { ok = (accepted ~= false), city_id = cityId, item = itemType, currency = "gold" }
end

local function CivSim_CityOrders_PurchaseWithFaith(cityId, itemType)
    local localPlayer = Game.GetLocalPlayer()
    local city = CityManager.GetCity(localPlayer, cityId) -- UNVERIFIED
    if city == nil then
        return { ok = false, reason = "city_not_found" }
    end
    local tParameters = {}
    tParameters[CityCommandTypes.PARAM_PRODUCTION_ITEM] = itemType -- UNVERIFIED
    tParameters[CityCommandTypes.PARAM_YIELD_TYPE] = GameInfo.Yields["YIELD_FAITH"].Index -- UNVERIFIED
    local accepted = CityManager.RequestCommand(city, CityCommandTypes.PURCHASE, tParameters) -- UNVERIFIED
    return { ok = (accepted ~= false), city_id = cityId, item = itemType, currency = "faith" }
end

CivSim_CityOrders = {
    set_production = CivSim_CityOrders_SetProduction,
    purchase_with_gold = CivSim_CityOrders_PurchaseWithGold,
    purchase_with_faith = CivSim_CityOrders_PurchaseWithFaith,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_CityOrders.set_production(7, "UNIT_WARRIOR")))
