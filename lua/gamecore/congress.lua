-- lua/gamecore/congress.lua
-- Context: InGame (read-only). Moved out of GameCore_Tuner on 2026-09-21 -- see below.
-- Backs declaration_id: congress.state (catalogs/observations/congress.yaml),
-- capability_id: congress.read.
--
-- ACCESSOR AUDIT (2026-09-21, specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md):
-- on top of the context defect this header already recorded, all three reads in this file named
-- accessors that do not exist:
--   * `:GetActiveResolutions()` -- appears in none of Firaxis' 645 shipped Lua files and is not a
--     registered engine binding. The real call is `:GetResolutions(playerID)`.
--   * `res.ResolutionID` / `.ResolutionType` / `.ProposerID` -- the entry carries none of those.
--     Its only identity is `.Type`, a GameInfo.Resolutions key; there is no proposer on it at all.
--   * `Players[p]:GetStats():GetDiplomaticFavor()` -- no such method on any object. Favor is read
--     straight off the player: `Players[p]:GetFavor()`.
-- `local_player_favor` was therefore 0 forever, so `congress.cast_vote`'s
-- `player.diplomatic_favor > 0` could never hold even during a session.
--
-- CONTEXT, now fixed: the sweep MEASURED `Game.GetWorldCongress()` as `nil` in GameCore_Tuner and
-- a `function` in InGame (spikes/sweep-raw/{GameCore_Tuner,InGame}__P3_*.txt), which made every
-- read here fail in its own declared context. `catalogs/observations/congress.yaml` now declares
-- `context: InGame`, the same correction units.yaml took under T213, so these accessors are asked
-- where they exist. UNVERIFIED LIVE: not yet exercised against a running client.
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
-- (That "REPORTED, NOT FIXED" note is now FIXED: congress.yaml declares `context: InGame`.)
--
-- SANDBOX CONSTRAINT (spike P5): neither tuner context exposes `require`, `io`, or `debug`, and no
-- JSON library exists in either. This file must stay entirely self-contained — no shared module
-- can ever be factored out and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- The complete set of methods Firaxis ever calls on the World Congress object is eight:
-- `IsInSession`, `GetMeetingStatus`, `GetResolutions`, `GetProposals`, `GetEmergencies`,
-- `GetReview`, `GetVotesandFavorCost`, `GetPreviousVotesOnResolution`. This file uses the first
-- and third. `IsInSession` is cited at dlc/expansion2/ui/replacements/actionpanel_expansion2.lua:45.
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

local function CivSim_Congress_Try(fn)
    local ok, value = pcall(fn)
    if ok then return value end
    return nil
end

-- --------------------------------------------------------------------------
-- The resolutions the World Congress screen is currently showing this player.
--
-- SOURCE (this machine, 2026-09-21; steamassets/dlc/expansion2/ui/additions/worldcongresspopup.lua):
--   :573-574  local pWorldCongress = Game.GetWorldCongress();
--             local kResolutions = pWorldCongress:GetResolutions(localPlayerID)
--   :580-581  the returned table mixes a non-numeric "Stage" key in with the numeric entries, so
--             Firaxis iterates `pairs` and skips anything whose key is not a number. `ipairs`
--             would be wrong here in a way that silently truncates.
--   :590      the entry's only identity is `.Type`, used as GameInfo.Resolutions[...]; there is no
--             resolution id and no proposer on it anywhere in the shipped code.
--   :606,:2241 the vote request keys on that row's `.Hash`, which is why `resolution_id` below is
--             the hash: `congress.cast_vote`'s `target` can then be handed straight to
--             PlayerOperations.PARAM_RESOLUTION_TYPE.
--
-- Parity: `.FavoredPlayerIDs`/`.DisfavoredPlayerIDs` and the vote tallies the screen shows are
-- deliberately NOT reported -- this body reports what is on the ballot, not who intends what.
-- --------------------------------------------------------------------------
local function CivSim_Congress_Resolutions(worldCongress, localPlayer)
    local resolutions = {}
    local list = CivSim_Congress_Try(function() return worldCongress:GetResolutions(localPlayer) end)
    if type(list) ~= "table" then
        return resolutions, "get_resolutions_unanswerable"
    end
    for key, entry in pairs(list) do
        if type(key) == "number" and type(entry) == "table" and entry.Type ~= nil then
            local row = CivSim_Congress_Try(function() return GameInfo.Resolutions[entry.Type] end)
            if type(row) == "table" and row.Hash ~= nil then
                local label = CivSim_Congress_Try(function() return Locale.Lookup(row.Name) end)
                resolutions[#resolutions + 1] = {
                    resolution_id = row.Hash,
                    resolution_type = row.ResolutionType,
                    name = (type(label) == "string" and label ~= "" and label) or nil,
                }
            end
        end
    end
    return resolutions, nil
end

local function CivSim_Congress_GetState()
    local localPlayer = Game.GetLocalPlayer()

    -- actionpanel_expansion2.lua:45 -- `m_CongressIsInSession = pWorldCongress:IsInSession()`.
    -- MEASURED: Game.GetWorldCongress() is nil in GameCore_Tuner and a function in InGame, which
    -- is why this declaration is now InGame. Rise & Fall and base-ruleset games have no World
    -- Congress at all, so an absent manager is reported as a reason, not as "not in session".
    local isInSession = false
    local sessionReason = nil
    local worldCongress = CivSim_Congress_Try(function() return Game.GetWorldCongress() end)
    if worldCongress == nil then
        sessionReason = "game_world_congress_unavailable"
    else
        local active = CivSim_Congress_Try(function() return worldCongress:IsInSession() end)
        if type(active) ~= "boolean" then
            sessionReason = "is_in_session_unanswerable"
        else
            isInSession = active
        end
    end

    local resolutions, resolutionsReason = {}, sessionReason
    if worldCongress ~= nil and isInSession then
        resolutions, resolutionsReason = CivSim_Congress_Resolutions(worldCongress, localPlayer)
    end

    -- toppanel_expansion2.lua:169 -- `local playerFavor = localPlayer:GetFavor()`, straight off the
    -- player object. There is no GetStats():GetDiplomaticFavor() and no treasury favor balance.
    local localPlayerFavor = 0
    local favorReason = nil
    local favor = CivSim_Congress_Try(function() return Players[localPlayer]:GetFavor() end)
    if type(favor) == "number" then
        localPlayerFavor = favor
    else
        favorReason = "get_favor_unanswerable"
    end

    local state = {
        is_in_session = isInSession,
        local_player_favor = localPlayerFavor,
        active_resolutions = resolutions,
    }
    if sessionReason ~= nil then state.is_in_session_reason = sessionReason end
    if resolutionsReason ~= nil then state.active_resolutions_reason = resolutionsReason end
    if favorReason ~= nil then state.local_player_favor_reason = favorReason end
    return state
end

CivSim_Congress = {
    state = CivSim_Congress_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Congress.state()))
