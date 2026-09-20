-- lua/gamecore/congress.lua
-- Context: GameCore_Tuner (read-only, best-effort).
-- Backs declaration_id: congress.state (catalogs/observations/congress.yaml),
-- capability_id: congress.read.
--
-- CAUTION (research.md R3), CONFIRMED TRUE by the live-client sweep (P3, per-context probe
-- output in spikes/sweep-raw/GameCore_Tuner__P3_congress_greatpeople_religion.txt): World Congress
-- was named as a "UI-bound surface" that might require the InGame context even to read, and the
-- sweep bears this out specifically for the accessor this file depends on.
--
-- `Game.GetWorldCongress()` is confirmed to exist (`function`) in InGame, but confirmed `nil` in
-- GameCore_Tuner — this file's own declared context. All 8 manager accessors
-- (`GetWorldCongress`, `GetReligion`, `GetGreatPeople`, `GetEmergencyManager`,
-- `GetHistoryManager`, `GetEras`, `GetQuestsManager`, `GetGossipManager`) exist in InGame; in
-- GameCore_Tuner only `GetReligion`, `GetGreatPeople`, `GetEras`, and `GetQuestsManager` are
-- present — `GetWorldCongress`, `GetEmergencyManager`, `GetHistoryManager`, and
-- `GetGossipManager` are all `nil` there. So every call below built on
-- `Game.GetWorldCongress()` will fail (caught by pcall, silently reporting "not in session")
-- under this file's current declared context, every single time, not just when the sweep's
-- P3-confirmed-unconfirmed methods happen to be wrong names.
--
-- REPORTED, NOT FIXED (Python/catalog-side, outside this agent's ownership): congress.state's
-- `context: GameCore_Tuner` in catalogs/observations/congress.yaml (not owned by this agent) is
-- now known-incompatible with the one call this read path needs — the caution this file's header
-- already carried ("this read must move to lua/ingame/congress.lua's read side rather than
-- staying here") is no longer speculative. This file is left as a best-effort, honestly-marked
-- placeholder rather than silently rewritten to run in a different context, since context
-- reassignment is a catalog-ownership decision (congress.yaml, capabilities.yaml) outside this
-- file.
--
-- SANDBOX CONSTRAINT (spike P5): neither tuner context exposes `require`, `io`, or `debug`, and no
-- JSON library exists in either. This file must stay entirely self-contained — no shared module
-- can ever be factored out and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- Beyond the context mismatch above: existence of `Game.GetWorldCongress()` (in InGame) is not
-- arity of its methods. `IsInSession`, `GetActiveResolutions`, and every field read off their
-- results below remain unconfirmed guesses, individually marked.
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
    -- CONFIRMED BROKEN in this file's own context (P3): Game.GetWorldCongress() is nil in
    -- GameCore_Tuner (confirmed a function only in InGame — see header). This pcall will always
    -- fail here, so isInSession will always read false under the current context assignment; kept
    -- as a best-effort, honestly-marked placeholder rather than silently rewritten to a different
    -- context (see header's REPORTED, NOT FIXED note). :IsInSession() itself remains an
    -- additionally unconfirmed method-name guess on top of that.
    local ok1, sessionActive = pcall(function() return Game.GetWorldCongress():IsInSession() end) -- UNVERIFIED
    if ok1 then isInSession = sessionActive end

    local resolutions = {}
    if isInSession then
        -- UNVERIFIED: :GetActiveResolutions() method, and every field read off its results below.
        local ok2, resolutionList = pcall(function() return Game.GetWorldCongress():GetActiveResolutions() end) -- UNVERIFIED
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
