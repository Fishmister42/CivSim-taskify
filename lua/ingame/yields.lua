-- lua/ingame/yields.lua
-- Context: InGame (read-only).
-- Backs declaration_id: player.yields (catalogs/observations/yields.yaml), capability_id: yields.read.
--
-- T258 (2026-09-21), Constitution III: every turn record must carry enough state to reconstruct
-- yields without replaying the game, and until this file existed `TurnCycle.yields` was `{}` on
-- every turn of every run -- the store's science / culture / gold / faith series were empty by
-- construction. This reads the local player's top bar: the seven numbers a human sees at the top
-- of the screen every turn, through the exact accessors Firaxis's own `toppanel.lua` uses (its
-- lines 106-162 on this build): `GetTechs():GetScienceYield()`, `GetCulture():GetCultureYield()`,
-- `GetReligion():GetFaithYield()` / `:GetFaithBalance()`, `GetTreasury():GetGoldYield() -
-- :GetTotalMaintenance()` (the top bar shows gold *net of maintenance*) / `:GetGoldBalance()`
-- (floored, as the top bar floors it), and `GetStats():GetTourism()`. Nothing else: no
-- per-source breakdown the tooltips compute, no other player's numbers, no hidden modifier.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- Parity note (Principle I): the top bar is the most-looked-at surface in the game; every value
-- here is displayed there, for the local player only, every turn. Each read is pcall'd
-- separately and a value the client does not answer is simply absent from the result -- the
-- record then carries a gap for that metric, never a fabricated zero (Constitution III).
-- UNVERIFIED LIVE from the InGame tuner state (the accessors are the top bar's own, which runs in
-- an InGame UI state; `Players[...]:GetTechs()` was measured in GameCore_Tuner by T213).

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

-- One pcall'd numeric read; nil (absent from the result) on any error or non-number.
local function CivSim_Yields_ReadNumber(fn)
    local ok, value = pcall(fn)
    if ok and type(value) == "number" and value == value then return value end
    return nil
end

local function CivSim_Yields_Read()
    local okP, player = pcall(function() return Players[Game.GetLocalPlayer()] end)
    if not okP or player == nil then
        return { reason = "local player is not resolvable from this state" }
    end
    local result = {}
    result.turn_number = CivSim_Yields_ReadNumber(function() return Game.GetCurrentGameTurn() end)
    result.science_per_turn = CivSim_Yields_ReadNumber(function()
        return player:GetTechs():GetScienceYield()
    end)
    result.culture_per_turn = CivSim_Yields_ReadNumber(function()
        return player:GetCulture():GetCultureYield()
    end)
    result.faith_per_turn = CivSim_Yields_ReadNumber(function()
        return player:GetReligion():GetFaithYield()
    end)
    result.faith_balance = CivSim_Yields_ReadNumber(function()
        return player:GetReligion():GetFaithBalance()
    end)
    result.gold_per_turn = CivSim_Yields_ReadNumber(function()
        local treasury = player:GetTreasury()
        return treasury:GetGoldYield() - treasury:GetTotalMaintenance()
    end)
    result.gold_balance = CivSim_Yields_ReadNumber(function()
        return math.floor(player:GetTreasury():GetGoldBalance())
    end)
    result.tourism_per_turn = CivSim_Yields_ReadNumber(function()
        return player:GetStats():GetTourism()
    end)
    return result
end

CivSim_Yields = {
    yields = CivSim_Yields_Read,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Yields.yields()))
