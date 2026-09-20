-- lua/gamecore/congress.lua
-- Context: GameCore_Tuner (read-only, best-effort).
-- Backs declaration_id: congress.state (catalogs/observations/congress.yaml),
-- capability_id: congress.read.
--
-- CAUTION (research.md R3): World Congress is explicitly named as a "UI-bound surface" that may
-- require the InGame context even to read. Everything below is a best-effort read; if the actual
-- API only surfaces session/resolution data while the Congress screen is open in InGame, this
-- read must move to lua/ingame/congress.lua's read side rather than staying here. Flagged, not
-- resolved.
--
-- UNVERIFIED (whole file): the World Congress Lua surface in Civ VI (Gathering Storm) is not
-- confidently known. `Game.GetEmergencyManager()` is believed to exist for emergencies; a
-- dedicated Congress/League manager equivalent is assumed below by analogy with Civ V's
-- `Game.GetLeague()` but its Civ VI name is not confirmed. Treat every accessor in this file as
-- a placeholder pending a live-client spike, not a verified call.
--
-- Parity note: only currently proposed/active resolutions and their public vote tallies (what the
-- Congress screen shows any player) are reported — never another civilization's private voting
-- intentions.

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

local function CivSim_Congress_GetState()
    local isInSession = false
    local ok1, sessionActive = pcall(function() return Game.GetCongress():IsInSession() end) -- UNVERIFIED
    if ok1 then isInSession = sessionActive end

    local resolutions = {}
    if isInSession then
        local ok2, resolutionList = pcall(function() return Game.GetCongress():GetActiveResolutions() end) -- UNVERIFIED
        if ok2 and type(resolutionList) == "table" then
            for _, res in ipairs(resolutionList) do
                resolutions[#resolutions + 1] = {
                    resolution_id = res.ResolutionID, -- UNVERIFIED
                    resolution_type = res.ResolutionType, -- UNVERIFIED
                    proposer_player_id = res.ProposerID, -- UNVERIFIED
                }
            end
        end
    end

    local localPlayer = Game.GetLocalPlayer()
    local localPlayerFavor = 0
    local ok3, favor = pcall(function()
        return Players[localPlayer]:GetStats():GetDiplomaticFavor() -- UNVERIFIED
    end)
    if ok3 and favor then localPlayerFavor = favor end

    return {
        is_in_session = isInSession,
        local_player_favor = localPlayerFavor,
        active_resolutions = resolutions,
    }
end

CivSim_Congress = {
    state = CivSim_Congress_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Congress.state()))
