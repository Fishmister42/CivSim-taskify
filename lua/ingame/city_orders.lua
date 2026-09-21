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
-- An item whose panel button would put a human into plot-placement mode is refused with
-- `requires_plot_placement` rather than issued, because the click that finishes it is one the
-- harness cannot make.

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

-- --------------------------------------------------------------------------
-- Resolving a production item the way the panel's own button already holds it.
--
-- MEASURED 2026-09-21: `CityOperationTypes.PARAM_PRODUCTION_ITEM`, which this file used to write,
-- is not a parameter key Civilization VI has. The production panel's click handlers name the item
-- by **kind**, and the value they pass is the item's **hash**, not its type string:
--   steamassets/base/assets/ui/panels/productionpanel.lua
--     :299-301  BuildUnit       -- tParameters[CityOperationTypes.PARAM_UNIT_TYPE]     = unitEntry.Hash;
--                                  CityManager.RequestOperation(city, CityOperationTypes.BUILD, tParameters)
--     :368-370  BuildBuilding   -- tParameters[CityOperationTypes.PARAM_BUILDING_TYPE] = buildingEntry.Hash;
--     :401-403  ZoneDistrict    -- tParameters[CityOperationTypes.PARAM_DISTRICT_TYPE] = districtEntry.Hash;
--     :416-418  AdvanceProject  -- tParameters[CityOperationTypes.PARAM_PROJECT_TYPE]  = projectEntry.Hash;
--     :2884-2897 GetBuildInsertMode -- with the queue panel closed (the ordinary click), the item
--                                  REPLACES the head of the queue:
--                                    PARAM_INSERT_MODE = VALUE_REPLACE_AT, QUEUE_DESTINATION_LOCATION = 0
-- `cities.set_production` takes the type name the agent read out of
-- `cities.state.available_productions` (e.g. "UNIT_BUILDER") and issues exactly that call.
-- --------------------------------------------------------------------------

local CivSim_CityOrders_PRODUCTION_KINDS = {
    { table_name = "Units", param = "PARAM_UNIT_TYPE", kind = "unit" },
    { table_name = "Buildings", param = "PARAM_BUILDING_TYPE", kind = "building" },
    { table_name = "Districts", param = "PARAM_DISTRICT_TYPE", kind = "district" },
    { table_name = "Projects", param = "PARAM_PROJECT_TYPE", kind = "project" },
}

-- The item's definition row, found by the same type string `cities.state` reports. Returns the
-- kind spec, the hash, and whether the game would open plot placement for it
-- (productionpanel.lua:341-404: a `RequiresPlacement` row that has not been placed puts the human
-- into `BUILDING_PLACEMENT`/`DISTRICT_PLACEMENT` and waits for a click on a map plot).
local function CivSim_CityOrders_ResolveProduction(city, productionType)
    for _, spec in ipairs(CivSim_CityOrders_PRODUCTION_KINDS) do
        local ok, definition = pcall(function() return GameInfo[spec.table_name][productionType] end)
        if ok and definition ~= nil and definition.Hash ~= nil then
            local needsPlacement = definition.RequiresPlacement == true
            if needsPlacement then
                local okPlaced, placed = pcall(function()
                    return city:GetBuildQueue():HasBeenPlaced(definition.Hash)
                end)
                if okPlaced and placed == true then needsPlacement = false end
            end
            return spec, definition.Hash, needsPlacement
        end
    end
    return nil, nil, false
end

local function CivSim_CityOrders_SetProduction(cityId, productionType)
    local city = CivSim_FindLocalCity(cityId)
    if city == nil then
        return { ok = false, reason = "city_not_found" }
    end
    local spec, hash, needsPlacement = CivSim_CityOrders_ResolveProduction(city, productionType)
    if spec == nil then
        return { ok = false, reason = "unknown_production_item", city_id = cityId, production = productionType }
    end
    if needsPlacement then
        -- A human's next act here is a click on a map plot; the harness has no action for that, so
        -- it refuses rather than issuing an operation the game will not complete. `cities.state`
        -- keeps such an item out of `available_productions` for the same reason.
        return {
            ok = false,
            reason = "requires_plot_placement",
            city_id = cityId,
            production = productionType,
            kind = spec.kind,
        }
    end
    local tParameters = {}
    tParameters[CityOperationTypes[spec.param]] = hash
    tParameters[CityOperationTypes.PARAM_INSERT_MODE] = CityOperationTypes.VALUE_REPLACE_AT
    tParameters[CityOperationTypes.PARAM_QUEUE_DESTINATION_LOCATION] = 0
    local accepted = CityManager.RequestOperation(city, CityOperationTypes.BUILD, tParameters)
    return {
        ok = (accepted ~= false),
        city_id = cityId,
        production = productionType,
        kind = spec.kind,
    }
end

-- VERIFIED (P4 spot-check): CityManager.RequestCommand confirmed to exist as a function.
-- The purchase parameters are the panel's own (productionpanel.lua:1637-1640,
-- `ComposeUnitForPurchase`): the item named by kind as its **hash** under
-- `CityCommandTypes.PARAM_UNIT_TYPE` / `PARAM_BUILDING_TYPE` / `PARAM_DISTRICT_TYPE`, plus
-- `PARAM_YIELD_TYPE = GameInfo.Yields["YIELD_GOLD"].Index`. `PARAM_PRODUCTION_ITEM`, which this
-- file used to write, does not exist.
local function CivSim_CityOrders_Purchase(cityId, itemType, yieldType, currency)
    local city = CivSim_FindLocalCity(cityId)
    if city == nil then
        return { ok = false, reason = "city_not_found" }
    end
    -- Units (productionpanel.lua:425) and buildings (:470) only: a district is "purchased" through
    -- plot placement (:496-517), not through a PURCHASE command, and a project cannot be bought.
    local spec, hash = CivSim_CityOrders_ResolveProduction(city, itemType)
    if spec == nil or (spec.kind ~= "unit" and spec.kind ~= "building") then
        return { ok = false, reason = "unknown_purchase_item", city_id = cityId, item = itemType }
    end
    local okYield, yieldIndex = pcall(function() return GameInfo.Yields[yieldType].Index end)
    if not okYield or yieldIndex == nil then
        return { ok = false, reason = "unknown_yield_type", city_id = cityId, item = itemType }
    end
    local tParameters = {}
    tParameters[CityCommandTypes[spec.param]] = hash
    tParameters[CityCommandTypes.PARAM_YIELD_TYPE] = yieldIndex
    local accepted = CityManager.RequestCommand(city, CityCommandTypes.PURCHASE, tParameters)
    return { ok = (accepted ~= false), city_id = cityId, item = itemType, currency = currency }
end

local function CivSim_CityOrders_PurchaseWithGold(cityId, itemType)
    return CivSim_CityOrders_Purchase(cityId, itemType, "YIELD_GOLD", "gold")
end

local function CivSim_CityOrders_PurchaseWithFaith(cityId, itemType)
    return CivSim_CityOrders_Purchase(cityId, itemType, "YIELD_FAITH", "faith")
end

CivSim_CityOrders = {
    set_production = CivSim_CityOrders_SetProduction,
    purchase_with_gold = CivSim_CityOrders_PurchaseWithGold,
    purchase_with_faith = CivSim_CityOrders_PurchaseWithFaith,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_CityOrders.set_production(7, "UNIT_WARRIOR")))
