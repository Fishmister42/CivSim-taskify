-- lua/ingame/religion.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/religion.lua rather
-- than this file.
-- Backs declaration_ids: religion.select_pantheon, religion.found_religion,
-- religion.select_belief (catalogs/actions/religion.yaml), capability_id: religion.orders.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- UNVERIFIED (whole file): the set-side religion API is not confidently known; see the read-side
-- caveats in lua/gamecore/religion.lua. `Player:GetReligion()` itself was not covered by the P3
-- spot-check (which confirmed the *global* `Game.GetReligion()` manager accessor, a different
-- symbol) and every method called on it below (`ChoosePantheon`, `FoundReligion`, `AddBelief`)
-- remains an unconfirmed guess. Choices offered are restricted to what CivSim_Religion.state()'s
-- available_beliefs already reported.
--
-- Parity note: a pantheon/religion/belief may only be chosen from the options the standard
-- pantheon/religion-founding screen would offer for the local player right now.

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

-- ACCESSOR AUDIT (2026-09-21, specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md):
-- all three bodies in this file called set-side methods on `Players[p]:GetReligion()`.
--   * `:ChoosePantheon(index)` exists nowhere in Firaxis' shipped Lua and is not a registered
--     engine binding -- an invented name.
--   * `:FoundReligion(...)` and `:AddBelief(...)` are real, but on the GAME religion object
--     (`Game.GetReligion()`), with a leading playerID, and only in scenario gameplay scripts
--     (dlc/polandscenario/scripts/polandscenario.lua:567-568,
--     dlc/blackdeathscenario/scripts/blackdeathscenario.lua:143-144) that force a religion on a
--     player. Wrong object, wrong arity, and -- even spelled correctly -- a script-side setter
--     that bypasses the screen a human clicks, which Principle I does not allow.
-- Every one of them raised inside its pcall and reported `ok = false` forever.
--
-- SOURCE (this machine, 2026-09-21; steamassets/base/assets/ui/): all three are
-- `UI.RequestPlayerOperation` calls whose belief/religion parameter is a **hash**, not the index
-- these bodies used to pass (great-people operations take an index -- the asymmetry is Firaxis').
--   choosers/pantheonchooser.lua:125-139  ConfirmPantheon --
--     PlayerOperations.FOUND_PANTHEON, { PARAM_BELIEF_TYPE = GameInfo.Beliefs[i].Hash,
--                                        PARAM_INSERT_MODE = PlayerOperations.VALUE_EXCLUSIVE }
--   religionscreen.lua:903-911  the Confirm button's found-religion request --
--     PlayerOperations.FOUND_RELIGION, { PARAM_RELIGION_TYPE = <Religions row>.Hash,
--                                        PARAM_INSERT_MODE = VALUE_EXCLUSIVE }
--   religionscreen.lua:914-919  ONE SEPARATE REQUEST PER BELIEF, issued back to back after it --
--     PlayerOperations.ADD_BELIEF,   { PARAM_BELIEF_TYPE = <Beliefs row>.Hash,
--                                      PARAM_INSERT_MODE = VALUE_EXCLUSIVE }
--     Founding does not carry its belief in the founding request; that is why found_religion
--     below issues two requests, exactly as the screen does.
-- The requests answer nothing; religion.state's own re-read is what confirms them.
-- UNVERIFIED LIVE.

local function CivSim_Religion_Request(operation, parameters)
    local ok, err = pcall(function()
        UI.RequestPlayerOperation(Game.GetLocalPlayer(), operation, parameters)
    end)
    if not ok then
        return "UI.RequestPlayerOperation errored: " .. tostring(err)
    end
    return nil
end

local function CivSim_Religion_SelectPantheon(beliefType)
    local row = GameInfo.Beliefs[beliefType]
    if row == nil then
        return { ok = false, reason = "unknown_belief" }
    end
    local tParameters = {}
    tParameters[PlayerOperations.PARAM_BELIEF_TYPE] = row.Hash
    tParameters[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE
    local failure = CivSim_Religion_Request(PlayerOperations.FOUND_PANTHEON, tParameters)
    if failure ~= nil then
        return { ok = false, reason = failure, belief = beliefType }
    end
    return { ok = true, belief = beliefType, mechanism = "PlayerOperations.FOUND_PANTHEON" }
end

local function CivSim_Religion_FoundReligion(religionType, beliefType)
    local religionRow = GameInfo.Religions[religionType]
    local beliefRow = GameInfo.Beliefs[beliefType]
    if religionRow == nil or beliefRow == nil then
        return { ok = false, reason = "unknown_religion_or_belief" }
    end
    local tReligion = {}
    tReligion[PlayerOperations.PARAM_RELIGION_TYPE] = religionRow.Hash
    tReligion[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE
    local failure = CivSim_Religion_Request(PlayerOperations.FOUND_RELIGION, tReligion)
    if failure ~= nil then
        return { ok = false, reason = failure, religion = religionType, belief = beliefType }
    end
    -- religionscreen.lua:914-919: the founder belief is a second, separate request.
    local tBelief = {}
    tBelief[PlayerOperations.PARAM_BELIEF_TYPE] = beliefRow.Hash
    tBelief[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE
    local beliefFailure = CivSim_Religion_Request(PlayerOperations.ADD_BELIEF, tBelief)
    if beliefFailure ~= nil then
        return {
            ok = false,
            reason = "religion_founded_but_belief_request_failed: " .. beliefFailure,
            religion = religionType,
            belief = beliefType,
        }
    end
    return {
        ok = true,
        religion = religionType,
        belief = beliefType,
        mechanism = "PlayerOperations.FOUND_RELIGION + PlayerOperations.ADD_BELIEF",
    }
end

local function CivSim_Religion_SelectBelief(beliefType)
    local row = GameInfo.Beliefs[beliefType]
    if row == nil then
        return { ok = false, reason = "unknown_belief" }
    end
    local tParameters = {}
    tParameters[PlayerOperations.PARAM_BELIEF_TYPE] = row.Hash
    tParameters[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE
    local failure = CivSim_Religion_Request(PlayerOperations.ADD_BELIEF, tParameters)
    if failure ~= nil then
        return { ok = false, reason = failure, belief = beliefType }
    end
    return { ok = true, belief = beliefType, mechanism = "PlayerOperations.ADD_BELIEF" }
end

CivSim_ReligionOrders = {
    select_pantheon = CivSim_Religion_SelectPantheon,
    found_religion = CivSim_Religion_FoundReligion,
    select_belief = CivSim_Religion_SelectBelief,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_ReligionOrders.select_pantheon("BELIEF_GOD_OF_THE_SEA")))
