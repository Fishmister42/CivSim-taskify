-- lua/ingame/diplomacy.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/diplomacy.lua rather
-- than this file.
-- Backs declaration_ids: diplomacy.declare_war, diplomacy.make_peace,
-- diplomacy.send_delegation (catalogs/actions/diplomacy.yaml), capability_id: diplomacy.orders.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- UNVERIFIED (whole file): the InGame diplomatic-action Lua surface is not confidently known, and
-- this file's domain was not covered by the live-client sweep at all. A `DiplomacyManager` /
-- `Game.GetDiplomacyManager()` request pattern is assumed by analogy with CityManager/UnitManager's
-- RequestOperation shape, but the exact type and method names are not confirmed against a live
-- client.
--
-- Parity note: only the diplomatic actions the standard diplomacy screen offers against a met
-- civilization (declare war, make peace when eligible, send a delegation) are exposed — no
-- back-channel action a human could not also take from that screen.

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

local function CivSim_Diplomacy_DeclareWar(otherPlayerId)
    local localPlayer = Game.GetLocalPlayer()
    local ok, result = pcall(function()
        return Players[localPlayer]:GetDiplomaticAI():DeclareWar(otherPlayerId) -- UNVERIFIED
    end)
    return { ok = (ok and result ~= false), target_player_id = otherPlayerId }
end

local function CivSim_Diplomacy_MakePeace(otherPlayerId)
    local localPlayer = Game.GetLocalPlayer()
    local ok, result = pcall(function()
        return Players[localPlayer]:GetDiplomaticAI():MakePeace(otherPlayerId) -- UNVERIFIED
    end)
    return { ok = (ok and result ~= false), target_player_id = otherPlayerId }
end

local function CivSim_Diplomacy_SendDelegation(otherPlayerId)
    local localPlayer = Game.GetLocalPlayer()
    local ok, result = pcall(function()
        return Players[localPlayer]:GetDiplomacy():SendDelegation(otherPlayerId) -- UNVERIFIED
    end)
    return { ok = (ok and result ~= false), target_player_id = otherPlayerId }
end

CivSim_DiplomacyOrders = {
    declare_war = CivSim_Diplomacy_DeclareWar,
    make_peace = CivSim_Diplomacy_MakePeace,
    send_delegation = CivSim_Diplomacy_SendDelegation,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_DiplomacyOrders.send_delegation(2)))
