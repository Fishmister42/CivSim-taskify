-- lua/ingame/turn_control.lua
-- Context: InGame (write/act). Verification reads back through this file's own read_turn_number
-- function (turn number is available identically from either tuner context; kept here so
-- turn.end_turn's whole capability — request and readback — lives in one reviewable place).
-- Backs declaration_id: turn.end_turn (catalogs/actions/turn.yaml), capability_id: turn.control.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library (`json`/`JSON`/
-- `cjson`/`dkjson`/`Serialize`) exists in either. This file, like every file under lua/, must stay
-- entirely self-contained — no shared module can ever be factored out and `require`d elsewhere —
-- and carries its own hand-rolled JSON encoder rather than assuming a library is present.
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

-- VERIFIED (spikes/lua-api-verification-linux.md, P1): `Game.EndTurn` does not exist — it is
-- `nil` in both InGame and GameCore_Tuner. The previous primary path (tried here first, before
-- falling back to UI.RequestAction) never worked and has been removed rather than kept as a dead
-- first attempt. `UI.RequestAction(ActionTypes.ACTION_ENDTURN)` is the confirmed real mechanism,
-- live-proven to advance the turn counter (1 -> 2).
--
-- ACTION_ENDTURN is read from the `ActionTypes` table at call time and never hard-coded: the
-- spike observed it as a Civ VI type hash (751412917), not a small stable enum value, and the
-- same hash-not-string issue recurs across this API's configuration surface generally.
--
-- `UI.RequestAction` itself was observed to return `nil` unconditionally (ok=true ret=nil in the
-- live proof), so its return value carries zero information about whether the end-turn was
-- accepted or silently swallowed — `ok` below means only "the call did not error", never "the
-- turn advanced". The turn-number readback in CivSim_TurnControl_ReadTurnNumber, taken before and
-- after this call by the caller, is the only real signal and is mandatory under FR-011, not a
-- nicety (see turn.end_turn's verification_predicate in catalogs/actions/turn.yaml).
local function CivSim_TurnControl_EndTurn()
    local actionId = ActionTypes and ActionTypes.ACTION_ENDTURN
    if actionId == nil then
        return { ok = false, reason = "action_endturn_unavailable" }
    end
    local ok, result = pcall(function() return UI.RequestAction(actionId) end)
    return { ok = ok, result_is_informative = false, path = "UI.RequestAction" }
end

-- Read back the current turn number for the end_turn verification_predicate
-- (`game.turn_number == observed_turn_number + 1 or game.is_waiting_for_other_players`).
--
-- VERIFIED (P1): `Game.GetCurrentGameTurn()` and `UI.CanEndTurn()` are both real, confirmed
-- functions in InGame; UI.CanEndTurn() in particular is "a genuine boolean, readable before
-- acting" per the spike, and is used here as is_local_player_turn's source. This replaces an
-- earlier guess (`Players[localPlayer]:IsTurnActive()`) that the sweep never exercised and that
-- has been removed rather than kept as an untested stand-in now that a verified boolean exists.
-- Caveat: UI.CanEndTurn() answers "can the local player end their turn right now", which may also
-- go false while a blocking prompt is up, not only while waiting on other players — the same
-- underlying condition turn.end_turn's availability_predicate already checks via
-- `game.has_blocking_prompt` as a separate, ANDed clause, so this does not loosen that check.
local function CivSim_TurnControl_ReadTurnNumber()
    local canEndTurn = false
    local ok, result = pcall(function() return UI.CanEndTurn() end)
    if ok then canEndTurn = (result == true) end
    return {
        turn_number = Game.GetCurrentGameTurn(),
        is_local_player_turn = canEndTurn,
        is_waiting_for_other_players = (not canEndTurn),
    }
end

CivSim_TurnControl = {
    end_turn = CivSim_TurnControl_EndTurn,
    read_turn_number = CivSim_TurnControl_ReadTurnNumber,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_TurnControl.end_turn()))
--   print(CivSim_JsonEncode(CivSim_TurnControl.read_turn_number()))
