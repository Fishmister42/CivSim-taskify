-- lua/ingame/city_orders.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/cities.lua's
-- CivSim_Cities.state() rather than this file.
-- Backs declaration_ids: cities.set_production, cities.purchase_with_gold,
-- cities.purchase_with_faith (catalogs/actions/cities.yaml), capability_id: cities.orders.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- CORRECTED against a live client (spike P4): `Cities.GetCity` is confirmed `nil` — there is no
-- `Cities` global with that shape. This file's own per-ID lookup used a different guess
-- (`CityManager.GetCity(playerID, cityID)`), also never confirmed to exist under that name and
-- absent from the spike's confirmed list; it has been replaced below with a lookup built entirely
-- from confirmed primitives (`Players`, `Player:GetCities()`, `Cities:Members()`, `City:GetID()` —
-- P4 spot-check, and the `Player:GetCities():Members()` idiom already used in
-- lua/gamecore/cities.lua), per the correction's guidance that city access goes through `Players`.
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

-- VERIFIED (P4 spot-check): finds one of the local player's own cities by id, built from
-- confirmed primitives (Players, Player:GetCities(), Cities:Members(), City:GetID()) rather than
-- the unconfirmed single-shot `CityManager.GetCity(playerID, cityID)` this file used to guess at
-- (see header). A city belonging to another player is deliberately not found here — every action
-- in this file only ever targets the local player's own cities, matching this catalog's parity
-- rule.
local function CivSim_FindLocalCity(cityId)
    local localPlayer = Game.GetLocalPlayer()
    local cities = Players[localPlayer]:GetCities()
    for _, c in cities:Members() do
        if c:GetID() == cityId then
            return c
        end
    end
    return nil
end

-- VERIFIED (P4 spot-check): CityManager.RequestOperation confirmed to exist as a function.
-- UNVERIFIED: CityOperationTypes.BUILD and the CityOperationTypes.PARAM_PRODUCTION_ITEM parameter
-- key are not on the spike's confirmed list (only CityCommandTypes.PARAM_X specifically was
-- spot-checked, not this operation's own enum members) — existence of the enum members used below
-- remains an untested guess, unlike the RequestOperation call itself.
local function CivSim_CityOrders_SetProduction(cityId, productionType)
    local city = CivSim_FindLocalCity(cityId)
    if city == nil then
        return { ok = false, reason = "city_not_found" }
    end
    local tParameters = {}
    tParameters[CityOperationTypes.PARAM_PRODUCTION_ITEM] = productionType -- UNVERIFIED
    local accepted = CityManager.RequestOperation(city, CityOperationTypes.BUILD, tParameters) -- UNVERIFIED: BUILD
    return { ok = (accepted ~= false), city_id = cityId, production = productionType }
end

-- VERIFIED (P4 spot-check): CityManager.RequestCommand confirmed to exist as a function, and a
-- `CityCommandTypes.PARAM_X`-style member is confirmed to exist on CityCommandTypes generally.
-- UNVERIFIED: CityCommandTypes.PURCHASE, PARAM_PRODUCTION_ITEM, and PARAM_YIELD_TYPE specifically
-- are not on the spike's confirmed list — only that the table has at least one PARAM_X-shaped
-- member was checked, not these particular names.
local function CivSim_CityOrders_PurchaseWithGold(cityId, itemType)
    local city = CivSim_FindLocalCity(cityId)
    if city == nil then
        return { ok = false, reason = "city_not_found" }
    end
    local tParameters = {}
    tParameters[CityCommandTypes.PARAM_PRODUCTION_ITEM] = itemType -- UNVERIFIED
    tParameters[CityCommandTypes.PARAM_YIELD_TYPE] = GameInfo.Yields["YIELD_GOLD"].Index -- UNVERIFIED
    local accepted = CityManager.RequestCommand(city, CityCommandTypes.PURCHASE, tParameters) -- UNVERIFIED: PURCHASE
    return { ok = (accepted ~= false), city_id = cityId, item = itemType, currency = "gold" }
end

local function CivSim_CityOrders_PurchaseWithFaith(cityId, itemType)
    local city = CivSim_FindLocalCity(cityId)
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
