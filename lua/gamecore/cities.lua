-- lua/gamecore/cities.lua
-- Context: InGame (read-only).
-- Backs declaration_id: cities.state (catalogs/observations/cities.yaml), capability_id: cities.read.
-- Also supplies the `city.*` predicate symbols (catalogs/README.md §4).
--
-- CONTEXT MOVED GameCore_Tuner -> InGame (2026-09-21), the same move T213 made for `units.state`
-- for the same reason: this body now reads what the *human's own panel* reads, so it runs where the
-- panel runs. `City:GetBuildQueue()` and the build-queue methods themselves are available on the
-- city object in either context, but the panel's purchase gate is
-- `CityManager.CanStartCommand` (productionpanel.lua:1639), and every shipped caller of
-- `CityManager`'s operation/command API is a UI file -- 23 under `base/assets/ui` alone, none in a
-- registered gameplay script, whose `CityManager` exposes only the read/state subset. T213
-- separately measured the sibling gate `UnitManager.CanStartOperation` as **nil** in
-- GameCore_Tuner and present InGame
-- (specs/002-civ-playing-harness/spikes/t213-observation-bodies-linux.md:53), and `Locale.Lookup`,
-- which gives each option the name printed on its button, is UI-side too. The enumeration this
-- file does (`PlayerManager.GetAlive()`, `Player:GetCities():Members()`, `PlayersVisibility[pid]`,
-- `Map.GetPlot`) is the identical idiom `lua/gamecore/units.lua` runs InGame and was measured live
-- at 14/14.
--
-- MEASURED 2026-09-21 (399 steps, 30 runs): `available_productions` was `[]` in every observation
-- on record and `production_queue` with it, so `cities.set_production`'s availability predicate
-- (`... and target in city.available_productions`) could never hold and no city ever produced
-- anything under the harness. The cause was in this file: it asked the build queue for
-- `GetAvailableProduction()`, a method that appears nowhere in Civilization VI's shipped Lua. The
-- production panel does not have such a call to make -- it *enumerates* `GameInfo.Units()`,
-- `GameInfo.Buildings()`, `GameInfo.Districts()` and `GameInfo.Projects()` and asks the city's own
-- build queue `CanProduce` about each row. This file now does exactly that, row for row, filter for
-- filter (citations inline below).
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- CORRECTED against a live client (spike P4): `Cities.GetCity` is confirmed `nil` — there is no
-- `Cities` global with that shape. This file never used it directly (it already enumerated cities
-- via `Players`), so nothing here needed changing for that specific finding.
--
-- Parity note (Principle I): `production_options` is the standard production panel's list and
-- nothing else — the same four `GameInfo` tables the panel walks, behind the panel's own filters
-- (`MustPurchase`, `InternalOnly`, `OnePerCity`, "not the item already in production"), admitted
-- only when the city's own build queue answers `CanProduce`, carrying only what the panel's button
-- shows a human: the item's name, its kind, the Production it requires and its turns-left, and
-- whether the button is greyed out. The field is `production_required` rather than the panel's own
-- variable name `Cost`, because `cost` is a harness-telemetry token the parity guard bans outright
-- (src/civsim_harness/parity/forbidden.py:142, "call cost") -- the number is the same one the
-- panel prints beside the Production icon. Nothing here reads it, a turn count or a buildable item
-- for a city the
-- local player does not own, and nothing reports an item the panel would not list. A district or
-- wonder whose button opens plot placement is listed (a human sees it) but is withheld from
-- `available_productions`, because the harness cannot make the plot click that a human makes next.
--
-- UNVERIFIED LIVE: that the InGame tuner state answers `City:GetBuildQueue()`,
-- `BuildQueue:CanProduce/GetTurnsLeft/Get*Cost` and `CityManager.CanStartCommand`. Every one of
-- them is pcall'd, and a build queue that cannot be read reports an empty list *with*
-- `available_productions_reason` — never a silent `[]`, which is precisely the failure that hid
-- this bug for 399 steps. The live stage verifies it by producing a Builder.

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

-- --------------------------------------------------------------------------
-- Small pcall helpers. Every engine call in this file goes through one of them, so an accessor
-- this build does not have degrades to an absent field rather than killing the whole observation
-- (a Lua runtime error in a dispatched chunk aborts the sweep -- T213).
-- --------------------------------------------------------------------------

local function CivSim_Cities_Try(fn)
    local ok, value = pcall(fn)
    if ok then return true, value end
    return false, nil
end

local function CivSim_Cities_Number(fn)
    local ok, value = CivSim_Cities_Try(fn)
    if ok and type(value) == "number" and value == value then return value end
    return nil
end

-- The localized string a human reads on the panel button; the raw LOC key if `Locale` is not
-- answerable from this state (productionpanel.lua:2065 `Name = row.Name` is looked up the same way
-- one line later at its button).
local function CivSim_Cities_DisplayName(rawName)
    local ok, value = CivSim_Cities_Try(function() return Locale.Lookup(rawName) end)
    if ok and type(value) == "string" and value ~= "" then return value end
    return rawName
end

-- --------------------------------------------------------------------------
-- The production panel's list, item for item.
--
-- steamassets/base/assets/ui/panels/productionpanel.lua, GetData()/PopulateList():
--   :1867  local buildQueue = pSelectedCity:GetBuildQueue();
--   :1894  m_CurrentProductionHash = buildQueue:GetCurrentProductionTypeHash();
--   :1897  for row in GameInfo.Districts() do
--   :1909    isInPanelList = (row.Hash ~= m_CurrentProductionHash or not row.OnePerCity)
--                            and not row.InternalOnly;
--   :1911    if isInPanelList and ( buildQueue:CanProduce( row.Hash, true ) or ... )
--   :1912      local isCanProduceExclusion, results = buildQueue:CanProduce( row.Hash, false, true );
--   :1961      local iProductionCost = buildQueue:GetDistrictCost( row.Index );
--   :1994      TurnsLeft = buildQueue:GetTurnsLeft( row.DistrictType ),
--   :2019  for row in GameInfo.Buildings() do
--   :2026    local bCanProduce = buildQueue:CanProduce( row.Hash, true );
--   :2037    if row.Hash ~= m_CurrentProductionHash
--              and (not row.MustPurchase or cityBuildings:IsPillaged(row.Hash)) and bCanProduce then
--   :2052      local iProductionCost = buildQueue:GetBuildingCost( row.Index );
--   :2073      TurnsLeft = buildQueue:GetTurnsLeft( row.Hash ),
--   :2113  for row in GameInfo.Units() do
--   :2119    kBuildParameters.UnitType = row.Hash;
--   :2120    kBuildParameters.MilitaryFormationType = MilitaryFormationTypes.STANDARD_MILITARY_FORMATION;
--   :2124    if not row.MustPurchase and buildQueue:CanProduce( kBuildParameters, true ) then
--   :2126      local nProductionCost = buildQueue:GetUnitCost( row.Index );
--   :2138      TurnsLeft = buildQueue:GetTurnsLeft( row.Hash ),
--   :2211  for row in GameInfo.Projects() do
--   :2214    if buildQueue:CanProduce( row.Hash, true ) then
--   :2221      local iProductionCost = buildQueue:GetProjectCost( row.Index );
--   :2231      TurnsLeft = buildQueue:GetTurnsLeft( row.ProjectType ),
--
-- The two-call `CanProduce` idiom is the panel's own: the first call (`bExclusionTest = true`) asks
-- "does this row belong on the panel at all", the second (`false, true`) asks "is its button live
-- or greyed out". Both are reproduced, so `disabled` here means exactly what the greyed button
-- means on screen.
-- --------------------------------------------------------------------------

local CivSim_Cities_PRODUCTION_KINDS = {
    {
        -- `turns_by_type`: the panel asks GetTurnsLeft with the row's *type string* for districts
        -- (:1994) and projects (:2231) and with its *hash* for buildings (:2073) and units (:2138).
        -- Both forms are accepted; each kind is asked the way its own panel row asks.
        kind = "district",
        rows = function() return GameInfo.Districts() end,
        type_field = "DistrictType",
        cost_method = "GetDistrictCost",
        turns_by_type = true,
        placeable = true,
    },
    {
        kind = "building",
        rows = function() return GameInfo.Buildings() end,
        type_field = "BuildingType",
        cost_method = "GetBuildingCost",
        turns_by_type = false,
        placeable = true,
    },
    {
        kind = "unit",
        rows = function() return GameInfo.Units() end,
        type_field = "UnitType",
        cost_method = "GetUnitCost",
        turns_by_type = false,
        placeable = false,
    },
    {
        kind = "project",
        rows = function() return GameInfo.Projects() end,
        type_field = "ProjectType",
        cost_method = "GetProjectCost",
        turns_by_type = true,
        placeable = false,
    },
}

-- productionpanel.lua:2119-2120 -- a unit is tested through a parameter table, not a bare hash.
local function CivSim_Cities_UnitBuildParameters(hash)
    local parameters = { UnitType = hash }
    local ok, formation = CivSim_Cities_Try(function()
        return MilitaryFormationTypes.STANDARD_MILITARY_FORMATION
    end)
    if ok and formation ~= nil then
        parameters.MilitaryFormationType = formation
    end
    return parameters
end

-- productionpanel.lua:1909/2037/2124 -- the panel's own per-kind admission rules, and nothing else.
local function CivSim_Cities_IsInPanelList(kind, row, currentHash)
    if kind == "district" then
        return (row.Hash ~= currentHash or not row.OnePerCity) and not row.InternalOnly
    elseif kind == "building" then
        -- The panel's full condition is `(not row.MustPurchase or cityBuildings:IsPillaged(row.Hash))`
        -- (productionpanel.lua:2037): a purchase-only building reappears in the *production* list
        -- when it is pillaged and needs repairing. That repair case is withheld here rather than
        -- guessed at -- withholding a row a human can see is Principle-I safe; offering one they
        -- cannot click is not.
        return row.Hash ~= currentHash and not row.MustPurchase
    elseif kind == "unit" then
        return not row.MustPurchase
    end
    return true -- project
end

-- productionpanel.lua:341-404 (BuildBuilding/ZoneDistrict): a row whose definition requires
-- placement and has not been placed yet opens `UI.SetInterfaceMode(..._PLACEMENT)` -- a human's
-- next act is a click on a map plot, which the harness has no action for. Listed, never offered.
local function CivSim_Cities_RequiresPlacement(spec, row, queue)
    if not spec.placeable then return false end
    if row.RequiresPlacement ~= true then return false end
    local ok, placed = CivSim_Cities_Try(function() return queue:HasBeenPlaced(row.Hash) end)
    if ok and placed == true then return false end
    return true
end

-- Returns `options` (the panel's list), `hashes` (each option's item hash, parallel by index, kept
-- out of the reported entry: it is an engine identifier, not something the panel shows) and a
-- `reason` when the build queue answered nothing at all.
local function CivSim_Cities_ReadProductions(queue)
    local options, hashes = {}, {}
    local currentHash = CivSim_Cities_Number(function()
        return queue:GetCurrentProductionTypeHash()
    end) or 0
    local answered = false

    for _, spec in ipairs(CivSim_Cities_PRODUCTION_KINDS) do
        local okRows, rows = CivSim_Cities_Try(spec.rows)
        if okRows and rows ~= nil then
            for row in rows do
                if row ~= nil and row.Hash ~= nil and CivSim_Cities_IsInPanelList(spec.kind, row, currentHash) then
                    local argument = row.Hash
                    if spec.kind == "unit" then
                        argument = CivSim_Cities_UnitBuildParameters(row.Hash)
                    end
                    local okList, onPanel = CivSim_Cities_Try(function()
                        return queue:CanProduce(argument, true)
                    end)
                    if okList then
                        answered = true
                        if onPanel == true then
                            local _, startable = CivSim_Cities_Try(function()
                                return queue:CanProduce(argument, false, true)
                            end)
                            local requiresPlacement =
                                CivSim_Cities_RequiresPlacement(spec, row, queue)
                            options[#options + 1] = {
                                type = row[spec.type_field],
                                name = CivSim_Cities_DisplayName(row.Name),
                                kind = spec.kind,
                                production_required = CivSim_Cities_Number(function()
                                    return queue[spec.cost_method](queue, row.Index)
                                end),
                                turns = CivSim_Cities_Number(function()
                                    if spec.turns_by_type then
                                        return queue:GetTurnsLeft(row[spec.type_field])
                                    end
                                    return queue:GetTurnsLeft(row.Hash)
                                end),
                                disabled = (startable ~= true),
                                requires_placement = requiresPlacement,
                            }
                            hashes[#options] = row.Hash
                        end
                    end
                end
            end
        end
    end

    if not answered then
        return {}, {}, "the city's build queue did not answer CanProduce from this Lua state"
    end
    return options, hashes, nil
end

-- --------------------------------------------------------------------------
-- The queue the panel draws down the side of the production list.
-- productionhelper.lua:25-28 (`GetCurrentProductionTypeHash`), :178-190 (`GetAt(i)` per slot,
-- MAX_QUEUE_SIZE = 7 at :8), :95-143 (an entry names its item through one of
-- `entry.UnitType` / `entry.BuildingType` / `entry.DistrictType` / `entry.ProjectType`).
-- citysupport.lua:203-206 -- a production *hash* indexes the GameInfo tables directly.
-- --------------------------------------------------------------------------

local CivSim_Cities_QUEUE_LOOKUPS = {
    { field = "UnitType", table_name = "Units", type_field = "UnitType" },
    { field = "BuildingType", table_name = "Buildings", type_field = "BuildingType" },
    { field = "DistrictType", table_name = "Districts", type_field = "DistrictType" },
    { field = "ProjectType", table_name = "Projects", type_field = "ProjectType" },
}

local CivSim_Cities_MAX_QUEUE_SIZE = 7 -- productionhelper.lua:8

local function CivSim_Cities_TypeForKey(tableName, typeField, key)
    if key == nil then return nil end
    local ok, definition = CivSim_Cities_Try(function() return GameInfo[tableName][key] end)
    if ok and definition ~= nil then
        local okType, value = CivSim_Cities_Try(function() return definition[typeField] end)
        if okType and type(value) == "string" then return value end
    end
    return nil
end

local function CivSim_Cities_TypeForHash(hash)
    if hash == nil or hash == 0 then return nil end
    for _, lookup in ipairs(CivSim_Cities_QUEUE_LOOKUPS) do
        local value = CivSim_Cities_TypeForKey(lookup.table_name, lookup.type_field, hash)
        if value ~= nil then return value end
    end
    return nil
end

local function CivSim_Cities_TypeForQueueEntry(entry)
    if type(entry) ~= "table" then return nil end
    for _, lookup in ipairs(CivSim_Cities_QUEUE_LOOKUPS) do
        local value = CivSim_Cities_TypeForKey(lookup.table_name, lookup.type_field, entry[lookup.field])
        if value ~= nil then return value end
    end
    return nil
end

local function CivSim_Cities_ReadQueue(queue)
    local items = {}
    local head = CivSim_Cities_TypeForHash(CivSim_Cities_Number(function()
        return queue:GetCurrentProductionTypeHash()
    end))
    if head ~= nil then items[#items + 1] = head end

    local size = CivSim_Cities_Number(function() return queue:GetSize() end)
    local last = math.min(size or 0, CivSim_Cities_MAX_QUEUE_SIZE) - 1
    for i = 1, last do
        local ok, entry = CivSim_Cities_Try(function() return queue:GetAt(i) end)
        if ok then
            local value = CivSim_Cities_TypeForQueueEntry(entry)
            if value ~= nil then items[#items + 1] = value end
        end
    end
    return items
end

-- --------------------------------------------------------------------------
-- The purchase tabs.
-- productionpanel.lua:1632-1640 (ComposeUnitForPurchase) -- purchasability is
-- `CityManager.CanStartCommand( pCity, CityCommandTypes.PURCHASE, bTestOnly, tParameters,
-- bReturnResults )` with `PARAM_UNIT_TYPE`/`PARAM_BUILDING_TYPE` (the item's **hash**) and
-- `PARAM_YIELD_TYPE` (`GameInfo.Yields["YIELD_GOLD"].Index`). The panel gates which rows it even
-- offers on the row's own `PurchaseYield`, which is also what keeps this loop cheap.
-- --------------------------------------------------------------------------

-- Units (productionpanel.lua:425) and buildings (:470) only: a district is "purchased" by entering
-- plot placement (:496-517), not by a `CityCommandTypes.PURCHASE` command, and a project cannot be
-- bought at all.
local CivSim_Cities_PURCHASE_PARAM_BY_KIND = {
    unit = "PARAM_UNIT_TYPE",
    building = "PARAM_BUILDING_TYPE",
}

local function CivSim_Cities_YieldIndex(yieldType)
    return CivSim_Cities_Number(function() return GameInfo.Yields[yieldType].Index end)
end

local function CivSim_Cities_CanPurchase(city, kind, hash, yieldIndex)
    local paramName = CivSim_Cities_PURCHASE_PARAM_BY_KIND[kind]
    if paramName == nil or hash == nil or yieldIndex == nil then return false end
    local ok, allowed = CivSim_Cities_Try(function()
        local tParameters = {}
        tParameters[CityCommandTypes[paramName]] = hash
        tParameters[CityCommandTypes.PARAM_YIELD_TYPE] = yieldIndex
        return CityManager.CanStartCommand(city, CityCommandTypes.PURCHASE, false, tParameters, false)
    end)
    return ok and allowed == true
end

-- --------------------------------------------------------------------------

local function CivSim_Cities_DescribeOwnCity(entry, city, localPlayer)
    local okQueue, queue = CivSim_Cities_Try(function() return city:GetBuildQueue() end)
    if not okQueue or queue == nil then
        entry.available_productions = {}
        entry.production_options = {}
        entry.production_queue = {}
        entry.available_productions_reason =
            "city:GetBuildQueue() is not answerable from this Lua state"
        entry.purchasable_with_gold = {}
        entry.purchasable_with_faith = {}
        entry.can_buy_with_gold = false
        entry.can_buy_with_faith = false
        entry.gold_available = CivSim_Cities_Number(function()
            return Players[localPlayer]:GetTreasury():GetGoldBalance()
        end)
        return entry
    end

    local options, hashes, reason = CivSim_Cities_ReadProductions(queue)
    entry.production_options = options
    entry.production_queue = CivSim_Cities_ReadQueue(queue)
    if reason ~= nil then
        entry.available_productions_reason = reason
    end

    -- `available_productions` stays a list<string> -- the exact shape
    -- `catalogs/actions/cities.yaml`'s `target in city.available_productions` compares against
    -- (catalogs/README.md §4) -- and holds the items whose panel button a human could click and
    -- have take effect without a further plot click.
    local available = {}
    for _, option in ipairs(options) do
        if option.disabled ~= true and option.requires_placement ~= true and option.type ~= nil then
            available[#available + 1] = option.type
        end
    end
    entry.available_productions = available

    local goldIndex = CivSim_Cities_YieldIndex("YIELD_GOLD")
    local faithIndex = CivSim_Cities_YieldIndex("YIELD_FAITH")
    local purchasableGold, purchasableFaith = {}, {}
    for index, option in ipairs(options) do
        local hash = hashes[index]
        if CivSim_Cities_CanPurchase(city, option.kind, hash, goldIndex) then
            purchasableGold[#purchasableGold + 1] = option.type
        end
        if CivSim_Cities_CanPurchase(city, option.kind, hash, faithIndex) then
            purchasableFaith[#purchasableFaith + 1] = option.type
        end
    end
    entry.purchasable_with_gold = purchasableGold
    entry.purchasable_with_faith = purchasableFaith
    entry.can_buy_with_gold = #purchasableGold > 0
    entry.can_buy_with_faith = #purchasableFaith > 0

    entry.gold_available = CivSim_Cities_Number(function()
        return Players[localPlayer]:GetTreasury():GetGoldBalance()
    end)
    return entry
end

local function CivSim_DescribeCity(city, localPlayer)
    local entry = {
        city_id = city:GetID(),
        name = city:GetName(),
        owner_player_id = city:GetOwner(),
        owner_is_local_player = (city:GetOwner() == localPlayer),
        plot = { x = city:GetX(), y = city:GetY() },
        population = city:GetPopulation(),
    }
    if entry.owner_is_local_player then
        return CivSim_Cities_DescribeOwnCity(entry, city, localPlayer)
    end
    return entry
end

-- VERIFIED (P4 spot-check): PlayerManager.GetAlive() exists. The previous `GetAliveMajors` name
-- here was an untested guess and is not confirmed to exist under that name; it has been replaced.
-- UNVERIFIED: whether GetAlive() returns every alive player (including city-states) or majors
-- only was not checked. Player:IsMajor() is applied defensively (only if present) so this file
-- keeps reporting majors only, matching its parity note, even if GetAlive() turns out to include
-- minors; if IsMajor is unavailable every returned player is kept, preserving prior behavior.
local function CivSim_Cities_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local cities = {}
    for _, player in ipairs(PlayerManager.GetAlive()) do
        if not player.IsMajor or player:IsMajor() then -- UNVERIFIED: Player:IsMajor()
            for _, city in player:GetCities():Members() do
                local plot = Map.GetPlot(city:GetX(), city:GetY())
                local isOwn = (player:GetID() == localPlayer)
                if isOwn or (plot ~= nil and PlayersVisibility[localPlayer]:IsVisible(plot:GetX(), plot:GetY())) then
                    cities[#cities + 1] = CivSim_DescribeCity(city, localPlayer)
                end
            end
        end
    end
    return { cities = cities }
end

CivSim_Cities = {
    state = CivSim_Cities_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Cities.state()))
