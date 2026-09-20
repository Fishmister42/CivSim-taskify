-- lua/ingame/turn_control.lua
-- Context: InGame (write/act). Verification reads back through this file's own read_turn_number
-- function (turn number is available identically from either tuner context; kept here so
-- turn.end_turn's whole capability — request and readback — lives in one reviewable place).
-- Backs declaration_id: turn.end_turn (catalogs/actions/turn.yaml), capability_id: turn.control.
--
-- Parity note: this is the click on the end-turn button (or its hotkey) and nothing else. It does
-- not decide anything about whether ending the turn is currently wise or legal — that is what
-- turn.end_turn's availability_predicate is for, evaluated by the harness before this is ever
-- invoked.

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

-- UNVERIFIED: `UI.RequestAction(ActionTypeIndex["EndTurn"])` and `Game.EndTurn()` are both
-- recalled, from different eras of Civ modding lineage, as plausible ways to trigger the same
-- button the player clicks; which one (if either) exists unmodified in this Civ VI build is not
-- confirmed. `Game.EndTurn()` is tried first as the more direct call, falling back to the
-- UI-action route, so a client exposing only one of the two still works.
local function CivSim_TurnControl_EndTurn()
    local ok1, result1 = pcall(function() return Game.EndTurn() end) -- UNVERIFIED
    if ok1 then
        return { ok = (result1 ~= false), path = "Game.EndTurn" }
    end
    local ok2, result2 = pcall(function()
        return UI.RequestAction(ActionTypeIndex["EndTurn"]) -- UNVERIFIED
    end)
    return { ok = (ok2 and result2 ~= false), path = "UI.RequestAction" }
end

-- Read back the current turn number for the end_turn verification_predicate
-- (`game.turn_number == observed_turn_number + 1 or game.is_waiting_for_other_players`).
local function CivSim_TurnControl_ReadTurnNumber()
    local localPlayer = Game.GetLocalPlayer()
    local isLocalTurn = false
    local ok, result = pcall(function() return Players[localPlayer]:IsTurnActive() end) -- UNVERIFIED
    if ok then isLocalTurn = result end
    return {
        turn_number = Game.GetCurrentGameTurn(),
        is_local_player_turn = isLocalTurn,
        is_waiting_for_other_players = (not isLocalTurn),
    }
end

CivSim_TurnControl = {
    end_turn = CivSim_TurnControl_EndTurn,
    read_turn_number = CivSim_TurnControl_ReadTurnNumber,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_TurnControl.end_turn()))
--   print(CivSim_JsonEncode(CivSim_TurnControl.read_turn_number()))
