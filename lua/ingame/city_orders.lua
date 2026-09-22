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
            -- `%c` is iscntrl() under the CLIENT's locale, which includes the C1 range
            -- 0x80-0x9F. UTF-8 continuation bytes are 0x80-0xBF, so the two overlap: escaping a
            -- matched high byte SEVERS the sequence and the whole frame stops decoding (three
            -- dead runs, 2026-09-22 -- "Kamal ud-Din Behzad" emitted a raw C4 followed by the
            -- literal text \\u0081). Lua patterns match bytes, not characters. The guard lives
            -- here rather than in the character class because narrowing the class needs \0,
            -- spelled `%z` in Lua 5.1 and `\0` in 5.2+, with no spelling valid in both -- and the
            -- client's Lua is not the version this repo's tests embed, so a wrong choice would
            -- pass every test and break every observation. Byte-identical in all 27 files and in
            -- nexus/sentinels.py's LUA_JSON_PRELUDE; tests/unit/test_lua_json_encoding.py
            -- enforces that identity, which is what stands in for the shared module the sandbox
            -- forbids.
            if string.byte(c) >= 0x80 then return c end
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
    -- MEASURED LIVE 2026-09-21 (Stage 5, run-ba3ad80d, 24 of 24): a nil id must mean "the city the
    -- human has selected", exactly as it does for a unit in lua/ingame/unit_orders.lua:80-86. A
    -- city order's own parity basis is the panel of the city whose banner was clicked
    -- (`cities.select` is the click), and `UI.GetHeadSelectedCity()` is the same call
    -- `cities.selection` already reports from. A wrong id is still not found, and nothing here
    -- ever picks a city on its own.
    if cityId == nil then
        local ok, selected = pcall(function() return UI.GetHeadSelectedCity() end)
        if ok and selected ~= nil then
            local okOwner, owner = pcall(function() return selected:GetOwner() end)
            if okOwner and owner == localPlayer then
                return selected
            end
        end
        return nil
    end
    local cities = Players[localPlayer]:GetCities()
    for _, c in cities:Members() do
        if c:GetID() == cityId then
            return c
        end
    end
    return nil
end

-- MEASURED LIVE 2026-09-21 (Stage 5, run-ba3ad80d): `cities.set_production` was rejected 24 of 24
-- with `production_queue` empty on every re-read, while a labelled probe issuing this file's own
-- parameter table through `CityManager.RequestOperation` filled the queue with UNIT_BUILDER inside
-- a second and `cities.state` read it back. The game accepted the order; the harness never sent
-- it. `act/executor.py`'s `_build_arguments` passes a decision's `target` as the LAST positional
-- argument, and a city order's target is the ITEM -- so every dispatch arrived here as
-- `set_production("UNIT_BUILDER")`, the item name landed in the `cityId` parameter,
-- `CivSim_FindLocalCity` compared city ids against a string, and the order came back
-- `city_not_found` for a city that was selected with its panel open. Identical in shape to the
-- `units.promote` bug (6606d4b): `unit_orders.lua` carried this normalisation, this file did not.
--
-- A lone string argument is therefore the production/purchase item, and the city is the selected
-- one -- which is what every one of these declarations' availability predicates already require
-- (`city.is_selected and ...`, catalogs/actions/cities.yaml). The explicit `(cityId, itemType)`
-- form still works for a caller that names the city.
local function CivSim_CityOrders_NormaliseArguments(cityId, itemType)
    if type(cityId) == "string" and itemType == nil then
        return nil, cityId
    end
    return cityId, itemType
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
--
-- MEASURED LIVE 2026-09-21 (run-26d265f6, 9 of 9): with the item correctly named by hash under
-- PARAM_UNIT_TYPE, the BUILD operation was issued and never took -- `production_queue` stayed `[]`
-- after every issue. The missing piece was the **insert mode**. This file copied the panel's
-- `GetBuildInsertMode` (productionpanel.lua:2884-2897), whose queue-panel-closed branch is
--   PARAM_INSERT_MODE = VALUE_REPLACE_AT, PARAM_QUEUE_DESTINATION_LOCATION = 0
-- -- "replace what is at slot 0". The harness's cities had *nothing* in production (their queue
-- really was empty, which is why `production_queue` read `[]` before the order as well), so there
-- was no slot 0 to replace and the operation was refused.
--
-- The shipped, NON-panel way to set a city's production -- the exact analogue of what this file
-- does, issued from script with no queue UI open -- is `ResetProduction()` in
-- steamassets/base/assets/ui/tutorialuiroot.lua:878-900:
--     :879-891  local tParameters = {};
--               local productionCategory = GameInfo.Types[m_LockedProductionHash].Kind;
--               if     productionCategory == "KIND_UNIT"     then tParameters[CityOperationTypes.PARAM_UNIT_TYPE]     = hash;
--               elseif productionCategory == "KIND_BUILDING" then tParameters[CityOperationTypes.PARAM_BUILDING_TYPE] = hash;
--               elseif productionCategory == "KIND_DISTRICT" then tParameters[CityOperationTypes.PARAM_DISTRICT_TYPE] = hash;
--               end
--               tParameters[CityOperationTypes.PARAM_INSERT_MODE] = CityOperationTypes.VALUE_EXCLUSIVE;
--     :898     CityManager.RequestOperation(capitalCity, CityOperationTypes.BUILD, tParameters);
-- No queue location at all: `VALUE_EXCLUSIVE` means "this item IS the queue now", which is exactly
-- what `cities.set_production` means and is correct whether the queue is empty or not. It is the
-- only insert mode any shipped file uses outside the panel's own queue-reordering UI.
--
-- `cities.set_production` takes the type name the agent read out of
-- `cities.state.available_productions` (e.g. "UNIT_BUILDER") and issues exactly that call. It asks
-- `CityManager.CanStartOperation` first (the same gate strategicview_mapplacement.lua:56 uses) so a
-- refusal names itself instead of arriving three seconds later as an unexplained false
-- verification predicate.
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

-- The game's own words for a refusal. productionpanel.lua:1577-1582 reads the reason list out of
-- `results[CityCommandResults.FAILURE_REASONS]` and `Locale.Lookup`s each entry for the tooltip a
-- human reads on the greyed-out button; CityOperationResults carries the same key on this build for
-- operations. Both are tried, defensively, and an unreadable results table yields no reasons rather
-- than a guess.
local function CivSim_CityOrders_ResultStrings(results)
    local reasons = {}
    if type(results) ~= "table" then return reasons end
    -- The two enums name the same slot on this build, so the keys are collected distinctly first:
    -- reading both blind would report every reason twice.
    local seenKey, keys = {}, {}
    for _, holder in ipairs({ "CityCommandResults", "CityOperationResults" }) do
        local ok, key = pcall(function() return _G[holder].FAILURE_REASONS end)
        if ok and key ~= nil and not seenKey[key] then
            seenKey[key] = true
            keys[#keys + 1] = key
        end
    end
    for _, key in ipairs(keys) do
        local ok, list = pcall(function() return results[key] end)
        if ok and type(list) == "table" then
            for _, entry in ipairs(list) do
                local okText, text = pcall(function() return Locale.Lookup(entry) end)
                reasons[#reasons + 1] = (okText and type(text) == "string" and text) or tostring(entry)
            end
        end
    end
    return reasons
end

-- What the city is producing right now, as the type name `cities.state` reports. The build queue's
-- head hash indexes the GameInfo tables directly (citysupport.lua:203-206).
local CivSim_CityOrders_CurrentProduction
do
    local lookups = {
        { table_name = "Units", type_field = "UnitType" },
        { table_name = "Buildings", type_field = "BuildingType" },
        { table_name = "Districts", type_field = "DistrictType" },
        { table_name = "Projects", type_field = "ProjectType" },
    }
    CivSim_CityOrders_CurrentProduction = function(city)
        local okHash, hash = pcall(function()
            return city:GetBuildQueue():GetCurrentProductionTypeHash()
        end)
        if not okHash or type(hash) ~= "number" or hash == 0 then return nil end
        for _, lookup in ipairs(lookups) do
            local ok, definition = pcall(function() return GameInfo[lookup.table_name][hash] end)
            if ok and definition ~= nil then
                local okType, value = pcall(function() return definition[lookup.type_field] end)
                if okType and type(value) == "string" then return value end
            end
        end
        return nil
    end
end

local function CivSim_CityOrders_SetProduction(cityId, productionType)
    cityId, productionType = CivSim_CityOrders_NormaliseArguments(cityId, productionType)
    local city = CivSim_FindLocalCity(cityId)
    if city == nil then
        return { ok = false, reason = "city_not_found" }
    end
    if type(productionType) ~= "string" then
        -- The inverse shape (a bare city id and no item) says what it is instead of being read as
        -- an item name, so no order is ever issued for something nobody named.
        return { ok = false, reason = "no_production_named", city_id = city:GetID() }
    end
    local spec, hash, needsPlacement = CivSim_CityOrders_ResolveProduction(city, productionType)
    if spec == nil then
        return { ok = false, reason = "unknown_production_item", city_id = city:GetID(),
                 production = productionType }
    end
    if needsPlacement then
        -- A human's next act here is a click on a map plot; the harness has no action for that, so
        -- it refuses rather than issuing an operation the game will not complete. `cities.state`
        -- keeps such an item out of `available_productions` for the same reason.
        return {
            ok = false,
            reason = "requires_plot_placement",
            city_id = city:GetID(),
            production = productionType,
            kind = spec.kind,
        }
    end
    -- tutorialuiroot.lua:879-891: the item by kind (its hash), and VALUE_EXCLUSIVE. Nothing else.
    local tParameters = {}
    tParameters[CityOperationTypes[spec.param]] = hash
    tParameters[CityOperationTypes.PARAM_INSERT_MODE] = CityOperationTypes.VALUE_EXCLUSIVE

    -- strategicview_mapplacement.lua:56 -- CanStartOperation(city, op, tParameters, bReturnResults)
    -- -> bCanStart, tResults. Asked before the request so a refusal carries the game's own reason.
    local okCheck, canStart, results = pcall(function()
        return CityManager.CanStartOperation(city, CityOperationTypes.BUILD, tParameters, true)
    end)
    if okCheck and canStart == false then
        return {
            ok = false,
            reason = "operation_refused",
            city_id = city:GetID(),
            production = productionType,
            kind = spec.kind,
            refusal_reasons = CivSim_CityOrders_ResultStrings(results),
        }
    end

    CityManager.RequestOperation(city, CityOperationTypes.BUILD, tParameters)

    -- `CityManager.RequestOperation` returns nothing in every shipped call site (none of them
    -- check it), so "it returned non-false" was never evidence of anything. The build queue's own
    -- head is. It is read back immediately; if the engine has not applied the operation by the
    -- time this chunk finishes, `confirmed` is false and `cities.set_production`'s
    -- verification_predicate (`target in city.production_queue`, re-read on the next observation)
    -- remains the authority -- this never reports a refusal it did not see.
    local current = CivSim_CityOrders_CurrentProduction(city)
    local result = {
        ok = true,
        city_id = city:GetID(),
        production = productionType,
        kind = spec.kind,
        insert_mode = "exclusive",
        confirmed = (current ~= nil and current == productionType),
    }
    if current ~= nil then
        result.current_production = current
    end
    if not result.confirmed then
        result.reason = "issued_not_yet_confirmed"
    end
    return result
end

-- VERIFIED (P4 spot-check): CityManager.RequestCommand confirmed to exist as a function.
-- The purchase parameters are the panel's own (productionpanel.lua:1637-1640,
-- `ComposeUnitForPurchase`): the item named by kind as its **hash** under
-- `CityCommandTypes.PARAM_UNIT_TYPE` / `PARAM_BUILDING_TYPE` / `PARAM_DISTRICT_TYPE`, plus
-- `PARAM_YIELD_TYPE = GameInfo.Yields["YIELD_GOLD"].Index`. `PARAM_PRODUCTION_ITEM`, which this
-- file used to write, does not exist.
local function CivSim_CityOrders_Purchase(cityId, itemType, yieldType, currency)
    cityId, itemType = CivSim_CityOrders_NormaliseArguments(cityId, itemType)
    local city = CivSim_FindLocalCity(cityId)
    if city == nil then
        return { ok = false, reason = "city_not_found" }
    end
    if type(itemType) ~= "string" then
        return { ok = false, reason = "no_purchase_item_named", city_id = city:GetID() }
    end
    -- Units (productionpanel.lua:425) and buildings (:470) only: a district is "purchased" through
    -- plot placement (:496-517), not through a PURCHASE command, and a project cannot be bought.
    local spec, hash = CivSim_CityOrders_ResolveProduction(city, itemType)
    if spec == nil or (spec.kind ~= "unit" and spec.kind ~= "building") then
        return { ok = false, reason = "unknown_purchase_item", city_id = city:GetID(),
                 item = itemType }
    end
    local okYield, yieldIndex = pcall(function() return GameInfo.Yields[yieldType].Index end)
    if not okYield or yieldIndex == nil then
        return { ok = false, reason = "unknown_yield_type", city_id = city:GetID(),
                 item = itemType }
    end
    local tParameters = {}
    tParameters[CityCommandTypes[spec.param]] = hash
    tParameters[CityCommandTypes.PARAM_YIELD_TYPE] = yieldIndex
    local accepted = CityManager.RequestCommand(city, CityCommandTypes.PURCHASE, tParameters)
    return { ok = (accepted ~= false), city_id = city:GetID(), item = itemType, currency = currency }
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
