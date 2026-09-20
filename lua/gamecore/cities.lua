-- lua/gamecore/cities.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: cities.state (catalogs/observations/cities.yaml), capability_id: cities.read.
-- Also supplies the `city.*` predicate symbols (catalogs/README.md §4).
--
-- Parity note: only the local player's own cities (full detail, matching the city screen) and
-- other civilizations' cities that are currently visible (name, owner, approximate population —
-- what a human sees on a visible city banner) are reported. No hidden production queues, no
-- undisclosed civ internals for cities the local player does not own.

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

-- UNVERIFIED: the exact accessor names for a city's buildable-item list. Civ VI's own production
-- panel builds this from the city's build queue manager; the closest known entry point is
-- CityManager-adjacent, but the precise call is not confirmed here.
local function CivSim_GetAvailableProductions(city)
    local items = {}
    local ok, queue = pcall(function() return city:GetBuildQueue() end) -- UNVERIFIED
    if ok and queue ~= nil and queue.GetAvailableProduction then
        for _, item in ipairs(queue:GetAvailableProduction()) do -- UNVERIFIED
            items[#items + 1] = item
        end
    end
    return items
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
        entry.available_productions = CivSim_GetAvailableProductions(city)
        local queueItems = {}
        local ok, queue = pcall(function() return city:GetBuildQueue() end) -- UNVERIFIED
        if ok and queue ~= nil and queue.GetQueue then
            for _, item in ipairs(queue:GetQueue()) do -- UNVERIFIED: queue introspection shape
                queueItems[#queueItems + 1] = item
            end
        end
        entry.production_queue = queueItems
        entry.gold_available = Players[localPlayer]:GetTreasury():GetGoldBalance() -- UNVERIFIED: player-scoped, not city-scoped, matching the top bar
        entry.can_buy_with_gold = city.CanPurchase and city:CanPurchase(true, false) or false -- UNVERIFIED
        entry.can_buy_with_faith = city.CanPurchase and city:CanPurchase(false, true) or false -- UNVERIFIED

        -- UNVERIFIED: per-item purchasability enumeration. The production panel filters the full
        -- available_productions list by what is currently affordable/purchasable; the exact
        -- accessor for that filtered view is not confirmed, so both lists are derived here by
        -- checking each available item individually rather than assumed to exist as a single call.
        local purchasableGold, purchasableFaith = {}, {}
        for _, item in ipairs(entry.available_productions) do
            local okG, canGold = pcall(function() return city:CanPurchase(true, false, item) end) -- UNVERIFIED
            if okG and canGold then purchasableGold[#purchasableGold + 1] = item end
            local okF, canFaith = pcall(function() return city:CanPurchase(false, true, item) end) -- UNVERIFIED
            if okF and canFaith then purchasableFaith[#purchasableFaith + 1] = item end
        end
        entry.purchasable_with_gold = purchasableGold
        entry.purchasable_with_faith = purchasableFaith
    end
    return entry
end

local function CivSim_Cities_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local cities = {}
    for _, player in ipairs(PlayerManager.GetAliveMajors()) do -- UNVERIFIED: exact enumerator name
        for _, city in player:GetCities():Members() do
            local plot = Map.GetPlot(city:GetX(), city:GetY())
            local isOwn = (player:GetID() == localPlayer)
            if isOwn or (plot ~= nil and plot:IsVisible(localPlayer)) then
                cities[#cities + 1] = CivSim_DescribeCity(city, localPlayer)
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
