-- lua/gamecore/diplomacy.lua
-- Context: GameCore_Tuner (read-only, best-effort).
-- Backs declaration_id: diplomacy.state (catalogs/observations/diplomacy.yaml),
-- capability_id: diplomacy.read.
--
-- CAUTION (research.md R3): diplomacy is named as a "UI-bound surface" that may only be fully
-- reachable from the InGame context, not GameCore_Tuner. The fields below are the subset believed
-- reachable as plain game-state reads (diplomatic state/visibility, which the standard diplomacy
-- overview shows regardless of which screen is open); anything that turns out to require the
-- InGame UI state at runtime must be re-declared against lua/ingame/diplomacy.lua's read side
-- instead of this file. This tension is flagged, not resolved, here. Not independently tested by
-- the live-client sweep (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md).
--
-- SANDBOX CONSTRAINT (spike P5): neither tuner context exposes `require`, `io`, or `debug`, and
-- no JSON library exists in either. This file must stay entirely self-contained — no shared
-- module can ever be factored out and `require`d elsewhere — and carries its own hand-rolled JSON
-- encoder.
--
-- Parity note: only diplomatic *state* visible to the human player (met/not met, current
-- diplomatic state, public agreements) is reported — never hidden AI intent, undisclosed grievances
-- weighting, or an opponent's private diplomatic calculations.

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

local function CivSim_Diplomacy_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local player = Players[localPlayer]
    -- MEASURED (2026-09-20, Linux 1.0.12.9, live run): `Player:GetDiplomaticAI()` does NOT exist
    -- in GameCore_Tuner ("function expected instead of nil"), and the unguarded call killed the
    -- whole diplomacy read -- and with it turn 1 of the first real run. It is the UI-side
    -- accessor (InGame). Acquired under pcall; when absent, `diplomatic_state` is reported as
    -- null (unobservable from this context) rather than the read failing.
    local diploAI = nil
    local okAI, ai = pcall(function() return player:GetDiplomaticAI() end)
    if okAI then diploAI = ai end

    -- VERIFIED (P4 spot-check): PlayerManager.GetAlive() exists, replacing the unconfirmed
    -- GetAliveMajors() guess. UNVERIFIED: whether it returns majors only or all alive players;
    -- Player:IsMajor() is applied defensively (kept if unavailable) as in lua/gamecore/cities.lua.
    local relations = {}
    for _, otherPlayer in ipairs(PlayerManager.GetAlive()) do
        if not otherPlayer.IsMajor or otherPlayer:IsMajor() then -- UNVERIFIED: Player:IsMajor()
            local otherID = otherPlayer:GetID()
            if otherID ~= localPlayer then
                local hasMet = false
                local ok1, met = pcall(function() return player:GetDiplomacy():HasMet(otherID) end) -- UNVERIFIED
                if ok1 then hasMet = met end
                if hasMet then
                    local stateName = nil
                    local ok2, stateIndex = pcall(function()
                        if diploAI ~= nil then
                            return diploAI:GetDiplomaticStateIndex(otherID) -- UNVERIFIED
                        end
                        -- GameCore-side fallback: the PlayerDiplomacy object. UNVERIFIED.
                        return player:GetDiplomacy():GetDiplomaticStateIndex(otherID)
                    end)
                    if ok2 and stateIndex then
                        local okName, name = pcall(function()
                            return GameInfo.DiplomaticStates[stateIndex].StateType -- UNVERIFIED
                        end)
                        if okName then stateName = name end
                    end
                    local hasDelegation = false
                    local ok3, delegation = pcall(function()
                        return player:GetDiplomacy():HasDelegationAt(otherID) -- UNVERIFIED
                    end)
                    if ok3 then hasDelegation = delegation end
                    relations[#relations + 1] = {
                        player_id = otherID,
                        has_met = true,
                        diplomatic_state = stateName,
                        has_delegation = hasDelegation,
                        civilization = PlayerConfigurations[otherID]:GetCivilizationTypeName(), -- UNVERIFIED accessor name
                    }
                else
                    relations[#relations + 1] = { player_id = otherID, has_met = false }
                end
            end
        end
    end

    return { relations = relations }
end

CivSim_Diplomacy = {
    state = CivSim_Diplomacy_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Diplomacy.state()))
