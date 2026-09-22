-- lua/gamecore/game_outcome.lua
-- Context: GameCore_Tuner (read-only). Backs declaration_id: game.outcome_state
-- (catalogs/observations/game.yaml), capability_id: game.outcome.
--
-- T216. Added because run/composition.py's stop evaluator could not resolve FR-005's
-- `game_outcome` stop condition at all: no declaration anywhere in catalogs/ read the game's own
-- outcome, so a run configured to play to a victory or defeat could never stop.
--
-- PARITY BOUNDARY -- read this before extending the file (Principle I, catalogs/README.md 3.5).
-- This file reports exactly one thing: whether the game has ended, and whether it ended in the
-- LOCAL player's own victory or defeat. It deliberately does NOT report, and must never be
-- extended to report:
--   * which other civilization won, or by which victory type;
--   * `Game.GetVictoryProgressForTeam(...)` for any team -- another team's progress toward a
--     victory is precisely the hidden AI/opponent state Principle I forbids, and it sits one
--     function call away from everything below, which is why it is named here as forbidden
--     rather than merely omitted;
--   * any other player's alive/dead state, score, or standing.
-- A human player sees their own victory or defeat screen at the moment the game ends. That, and
-- nothing adjacent to it, is what this file is declared against.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained and carries its own JSON encoder.

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
            -- `%c` is iscntrl() under the CLIENT's locale, which includes the C1 range
            -- 0x80-0x9F. UTF-8 continuation bytes are 0x80-0xBF, so the two overlap: escaping a
            -- matched high byte SEVERS the sequence and the whole frame stops decoding (three
            -- dead runs, 2026-09-22 -- "Kamal ud-Din Behzad" emitted a raw C4 followed by the
            -- literal text \\u0081). Lua patterns match bytes, not characters. The guard lives
            -- here rather than in the character class because narrowing the class needs \0,
            -- spelled `%z` in Lua 5.1 and `\0` in 5.2+, with no spelling valid in both -- and the
            -- client's Lua is not the version this repo's tests embed, so a wrong choice would
            -- pass every test and break every observation. Byte-identical in all 27 files and in
            -- nexus/sentinels.py's LUA_JSON_PRELUDE; tests/unit/test_lua_json_encoding.py
            -- enforces that identity, which is what stands in for the shared module the sandbox
            -- forbids.
            if string.byte(c) >= 0x80 then return c end
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

-- VERIFIED that the functions exist: the R5/T077 live namespace enumeration
-- (specs/002-civ-playing-harness/spikes/r5-raw/GameCore_Tuner__namespaces.txt) lists
-- `Game.GetWinningTeam`, `Game.GetLocalPlayer`, and `Game.GetCurrentGameTurn` as real functions in
-- THIS context (and the InGame listing carries all three too).
--
-- UNVERIFIED: `Game.GetWinningTeam()`'s exact "nobody has won yet" sentinel. Civ VI convention is
-- a negative team id (-1); anything nil or negative is treated as "no winner" below, so a
-- different non-negative sentinel would be the one way this misreports, and it would misreport as
-- a *defeat* rather than as a victory. If a live client shows a different sentinel, correct the
-- `winningTeam >= 0` guard here.
--
-- UNVERIFIED: `Players[id]:GetTeam()` and `Players[id]:IsAlive()`. `Players[localPlayer]` is the
-- established accessor across every other lua/gamecore/*.lua file in this catalog; the two
-- methods' exact names are not confirmed against a live client, so each is independently
-- pcall-guarded and an unreadable value degrades to "not resolved", never to a fabricated outcome.
local function CivSim_GameOutcome_Read()
    local localPlayer = nil
    local okLocal, lp = pcall(function() return Game.GetLocalPlayer() end)
    if okLocal then localPlayer = lp end

    if localPlayer == nil or localPlayer < 0 then
        -- No local player (an observer slot, or the game is not in a playable state): report
        -- honestly that nothing could be resolved rather than guessing at an outcome.
        return { is_game_over = false, outcome = "unresolved" }
    end

    local localTeam = nil
    local okTeam, team = pcall(function() return Players[localPlayer]:GetTeam() end) -- UNVERIFIED
    if okTeam then localTeam = team end

    local winningTeam = nil
    local okWin, winner = pcall(function() return Game.GetWinningTeam() end)
    if okWin then winningTeam = winner end

    if type(winningTeam) == "number" and winningTeam >= 0 then
        if localTeam ~= nil and winningTeam == localTeam then
            return { is_game_over = true, outcome = "victory" }
        end
        -- Someone has won and it is not this player's team. The local player's own game is over
        -- and they are shown that; which team won is NOT reported (see the parity note above).
        return { is_game_over = true, outcome = "defeat" }
    end

    -- No victory yet. The remaining way a local player's own game ends is their own elimination,
    -- which a human sees as the defeat screen.
    local alive = nil
    local okAlive, isAlive = pcall(function() return Players[localPlayer]:IsAlive() end) -- UNVERIFIED
    if okAlive then alive = isAlive end
    if alive == false then
        return { is_game_over = true, outcome = "defeat" }
    end

    return { is_game_over = false, outcome = "none" }
end

CivSim_GameOutcome = {
    outcome_state = CivSim_GameOutcome_Read,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_GameOutcome.outcome_state()))
