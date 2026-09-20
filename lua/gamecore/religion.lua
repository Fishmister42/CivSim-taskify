-- lua/gamecore/religion.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: religion.state (catalogs/observations/religion.yaml),
-- capability_id: religion.read.
--
-- Parity note: pantheon/religion state is reported only for the local player and only the
-- majority religion of a visible city (exactly what the standard religion overview and city
-- banners show) — never an opponent's undisclosed belief choices or founding intentions.

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

-- UNVERIFIED: Game.GetReligion() as a global religion-game singleton (analogous to
-- Game.GetGreatPeople()) is believed to exist based on community modding references to
-- religion-founding and belief-availability queries, but the exact method names below are not
-- confirmed against a live client.
local function CivSim_Religion_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local player = Players[localPlayer]
    local religionMgr = player:GetReligion() -- UNVERIFIED: Player:GetReligion()

    local pantheonSelected = false
    local ok1, pantheonType = pcall(function() return religionMgr:GetPantheon() end) -- UNVERIFIED
    if ok1 and pantheonType and pantheonType ~= -1 then
        pantheonSelected = true
    end

    local religionFounded = false
    local ownReligionType = nil
    local ok2, foundedType = pcall(function() return religionMgr:GetReligionTypeCreated() end) -- UNVERIFIED
    if ok2 and foundedType and foundedType ~= -1 then
        religionFounded = true
        ownReligionType = GameInfo.Religions[foundedType].ReligionType -- UNVERIFIED
    end

    local availableBeliefs = {}
    local ok3, beliefList = pcall(function() return Game.GetReligion():GetAvailableBeliefs(localPlayer) end) -- UNVERIFIED
    if ok3 and type(beliefList) == "table" then
        for _, beliefType in ipairs(beliefList) do
            availableBeliefs[#availableBeliefs + 1] = GameInfo.Beliefs[beliefType].BeliefType -- UNVERIFIED
        end
    end

    -- Majority religion of visible cities only (what a city banner shows).
    local cityReligions = {}
    for _, otherPlayer in ipairs(PlayerManager.GetAliveMajors()) do -- UNVERIFIED
        for _, city in otherPlayer:GetCities():Members() do
            local plot = Map.GetPlot(city:GetX(), city:GetY())
            local isOwn = (otherPlayer:GetID() == localPlayer)
            if isOwn or (plot ~= nil and plot:IsVisible(localPlayer)) then
                local ok4, majorityReligion = pcall(function()
                    return city:GetReligion():GetMajorityReligion() -- UNVERIFIED: City:GetReligion()
                end)
                if ok4 and majorityReligion and majorityReligion ~= -1 then
                    cityReligions[#cityReligions + 1] = {
                        city_id = city:GetID(),
                        majority_religion = GameInfo.Religions[majorityReligion].ReligionType, -- UNVERIFIED
                    }
                end
            end
        end
    end

    return {
        pantheon_selected = pantheonSelected,
        religion_founded = religionFounded,
        own_religion = ownReligionType,
        available_beliefs = availableBeliefs,
        city_majority_religions = cityReligions,
    }
end

CivSim_Religion = {
    state = CivSim_Religion_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Religion.state()))
