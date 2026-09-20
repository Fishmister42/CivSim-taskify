-- lua/gamecore/espionage.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: espionage.state (catalogs/observations/espionage.yaml),
-- capability_id: espionage.read.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- UNVERIFIED (whole file): the espionage Lua accessor surface is not confidently known, and this
-- file's domain was not covered by the live-client sweep at all. The calls below follow the
-- Player-scoped-manager pattern used elsewhere in this catalog (GetTreasury, GetCulture,
-- GetTechs) by analogy, but `Player:GetEspionage()` and its methods are not confirmed against a
-- live client and must be validated before first use.
--
-- Parity note: only the local player's own spies, their current missions, and *outcomes* of
-- resolved missions the human would see in the notification/reports log are reported. Never an
-- opponent's spy locations, counter-espionage posture, or in-progress opposing missions — those
-- are exactly the kind of hidden-opponent-state FR-019 forbids.

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

local function CivSim_Espionage_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local player = Players[localPlayer]

    local spies = {}
    local ok, espionageMgr = pcall(function() return player:GetEspionage() end) -- UNVERIFIED
    if ok and espionageMgr ~= nil and espionageMgr.GetSpies then
        for _, spy in ipairs(espionageMgr:GetSpies()) do -- UNVERIFIED
            spies[#spies + 1] = {
                unit_id = spy.GetUnitID and spy:GetUnitID() or nil, -- UNVERIFIED
                mission = spy.GetCurrentMission and spy:GetCurrentMission() or nil, -- UNVERIFIED
                target_city_id = spy.GetTargetCityID and spy:GetTargetCityID() or nil, -- UNVERIFIED
                is_available = spy.IsAvailable and spy:IsAvailable() or false, -- UNVERIFIED
            }
        end
    end

    return { spies = spies }
end

CivSim_Espionage = {
    state = CivSim_Espionage_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Espionage.state()))
