-- lua/gamecore/religion.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: religion.state (catalogs/observations/religion.yaml),
-- capability_id: religion.read.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- This file's own accessors (Player:GetReligion(), Game.GetReligion(), etc.) were not covered by
-- the live-client sweep and remain unconfirmed guesses; see the per-call UNVERIFIED markers below,
-- left as-is because the sweep did not test them. The one correction made here (P4 spot-check) is
-- PlayerManager.GetAlive() replacing the unconfirmed PlayerManager.GetAliveMajors() guess.
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

-- VERIFIED (P3, per-context probe: spikes/sweep-raw/GameCore_Tuner__P3_congress_greatpeople_religion.txt):
-- `Game.GetReligion()` as a global religion-game singleton is confirmed to exist as a function in
-- both InGame and this file's own GameCore_Tuner context (one of only 4 of the 8 manager
-- accessors present in GameCore_Tuner — see lua/gamecore/congress.lua's header for the full
-- breakdown and the contrasting case where the accessor is GameCore_Tuner-absent). UNVERIFIED:
-- existence is not arity — every method called on it below, and on the separate
-- `Player:GetReligion()` call, remains an unconfirmed guess.
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
    -- VERIFIED (P3): Game.GetReligion() itself confirmed to exist. UNVERIFIED: :GetAvailableBeliefs(...).
    local ok3, beliefList = pcall(function() return Game.GetReligion():GetAvailableBeliefs(localPlayer) end) -- UNVERIFIED
    if ok3 and type(beliefList) == "table" then
        for _, beliefType in ipairs(beliefList) do
            availableBeliefs[#availableBeliefs + 1] = GameInfo.Beliefs[beliefType].BeliefType -- UNVERIFIED
        end
    end

    -- Majority religion of visible cities only (what a city banner shows).
    -- VERIFIED (P4 spot-check): PlayerManager.GetAlive() exists, replacing the unconfirmed
    -- GetAliveMajors() guess. UNVERIFIED: whether it returns majors only or all alive players;
    -- Player:IsMajor() is applied defensively (kept if unavailable) as in lua/gamecore/cities.lua.
    local cityReligions = {}
    for _, otherPlayer in ipairs(PlayerManager.GetAlive()) do
        if not otherPlayer.IsMajor or otherPlayer:IsMajor() then -- UNVERIFIED: Player:IsMajor()
            for _, city in otherPlayer:GetCities():Members() do
                local plot = Map.GetPlot(city:GetX(), city:GetY())
                local isOwn = (otherPlayer:GetID() == localPlayer)
                if isOwn or (plot ~= nil and PlayersVisibility[localPlayer]:IsVisible(plot:GetX(), plot:GetY())) then
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
