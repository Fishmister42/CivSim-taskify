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
-- ACCESSOR AUDIT (2026-09-21, specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md):
-- all three bodies called invented methods, each on an object that would not have carried them
-- anyway.
--   * `:DeclareWar(id)` and `:MakePeace(id)` were called on `Player:GetDiplomaticAI()`. That
--     object is read-only opinion data -- the only seven methods Firaxis ever calls on it are
--     GetDiplomaticStateIndex, GetDiplomaticScore, GetDiplomaticModifiers, GetThreatFrom,
--     GetThreatString, GetTrustFrom, GetTrustString. The real spellings on the *Diplomacy* object
--     are `DeclareWarOn` / `MakePeaceWith`, and those are scenario-script calls, not the UI path.
--   * `:SendDelegation(id)` exists nowhere at all, and there is no delegation PlayerOperation and
--     no delegation DealAgreementType either.
-- Every one raised inside its pcall and reported `ok = false`, which is why `send_delegation` sits
-- in the coverage report as attempted-and-never-applied.
-- UNVERIFIED LIVE: read out of Firaxis' callers, not yet exercised against a running client.
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

-- SOURCE (this machine, 2026-09-21; steamassets/base/assets/ui/popups/declarewarpopup.lua:77-81):
-- the war confirmation's Yes button issues
--   parameters[PlayerOperations.PARAM_PLAYER_ONE] = eAttackingPlayer
--   parameters[PlayerOperations.PARAM_PLAYER_TWO] = eDefendingPlayer
--   UI.RequestPlayerOperation(eAttackingPlayer, PlayerOperations.DIPLOMACY_DECLARE_WAR, parameters)
-- Identical at base/assets/ui/popups/leaderview.lua:18-23. Against a full civ the same popup can
-- instead open a leader session (`DiplomacyManager.RequestSession(..., "DECLARE_SURPRISE_WAR")`,
-- diplomacyactionview.lua:408), which is a conversation the harness cannot hold; the operation
-- above is the session-less path and is what this body uses.
local function CivSim_Diplomacy_DeclareWar(otherPlayerId)
    local localPlayer = Game.GetLocalPlayer()
    if type(otherPlayerId) ~= "number" then
        return { ok = false, reason = "unknown_player", target_player_id = otherPlayerId }
    end
    local ok, err = pcall(function()
        local tParameters = {}
        tParameters[PlayerOperations.PARAM_PLAYER_ONE] = localPlayer
        tParameters[PlayerOperations.PARAM_PLAYER_TWO] = otherPlayerId
        UI.RequestPlayerOperation(localPlayer, PlayerOperations.DIPLOMACY_DECLARE_WAR, tParameters)
    end)
    if not ok then
        return {
            ok = false,
            reason = "UI.RequestPlayerOperation errored: " .. tostring(err),
            target_player_id = otherPlayerId,
        }
    end
    return {
        ok = true,
        target_player_id = otherPlayerId,
        mechanism = "PlayerOperations.DIPLOMACY_DECLARE_WAR",
    }
end

-- SOURCE (base/assets/ui/partialscreens/citystates.lua:815-818): the peace button issues
--   parameters[PlayerOperations.PARAM_PLAYER_ONE] = localPlayerID
--   parameters[PlayerOperations.PARAM_PLAYER_TWO] = iPlayer
--   UI.RequestPlayerOperation(localPlayerID, PlayerOperations.DIPLOMACY_MAKE_PEACE, parameters)
-- Peace with a full civ goes through the deal system instead (diplomacyactionview.lua:431-447
-- builds a DealAgreementTypes.MAKE_PEACE item and opens a "MAKE_DEAL" session) -- a negotiation,
-- not an order, and out of this action's reach. `diplomacy.state`'s re-read is what confirms
-- whichever of the two the client honours; the `ok` here only says the request did not error.
local function CivSim_Diplomacy_MakePeace(otherPlayerId)
    local localPlayer = Game.GetLocalPlayer()
    if type(otherPlayerId) ~= "number" then
        return { ok = false, reason = "unknown_player", target_player_id = otherPlayerId }
    end
    local ok, err = pcall(function()
        local tParameters = {}
        tParameters[PlayerOperations.PARAM_PLAYER_ONE] = localPlayer
        tParameters[PlayerOperations.PARAM_PLAYER_TWO] = otherPlayerId
        UI.RequestPlayerOperation(localPlayer, PlayerOperations.DIPLOMACY_MAKE_PEACE, tParameters)
    end)
    if not ok then
        return {
            ok = false,
            reason = "UI.RequestPlayerOperation errored: " .. tostring(err),
            target_player_id = otherPlayerId,
        }
    end
    return {
        ok = true,
        target_player_id = otherPlayerId,
        mechanism = "PlayerOperations.DIPLOMACY_MAKE_PEACE",
    }
end

-- SOURCE (base/assets/ui/diplomacyactionview.lua:466-467): "Send Delegation" is
--   DiplomacyManager.RequestSession(ms_LocalPlayerID, ms_SelectedPlayerID, "DIPLOMATIC_DELEGATION")
-- and nothing else -- there is no delegation PlayerOperation and no delegation deal item. The gold
-- it costs is looked up as GetDiplomaticActionCost("DIPLOACTION_DIPLOMATIC_DELEGATION") (:381,
-- :387), and whether it is offered at all is gated on
-- `not localPlayerDiplomacy:HasDelegationAt(otherID)` (:951, :1416) -- which is exactly the
-- `other_player.has_delegation` this action's availability predicate already reads out of
-- diplomacy.state.
--
-- The session this opens is a real UI session, so it is closed the same way lua/ingame/screens.lua
-- closes the ones it opens; leaving it open would block the next decision step.
local function CivSim_Diplomacy_SendDelegation(otherPlayerId)
    local localPlayer = Game.GetLocalPlayer()
    if type(otherPlayerId) ~= "number" then
        return { ok = false, reason = "unknown_player", target_player_id = otherPlayerId }
    end
    local ok, err = pcall(function()
        DiplomacyManager.RequestSession(localPlayer, otherPlayerId, "DIPLOMATIC_DELEGATION")
    end)
    if not ok then
        return {
            ok = false,
            reason = "DiplomacyManager.RequestSession errored: " .. tostring(err),
            target_player_id = otherPlayerId,
        }
    end
    return {
        ok = true,
        target_player_id = otherPlayerId,
        mechanism = 'DiplomacyManager.RequestSession(..., "DIPLOMATIC_DELEGATION")',
    }
end

CivSim_DiplomacyOrders = {
    declare_war = CivSim_Diplomacy_DeclareWar,
    make_peace = CivSim_Diplomacy_MakePeace,
    send_delegation = CivSim_Diplomacy_SendDelegation,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_DiplomacyOrders.send_delegation(2)))
