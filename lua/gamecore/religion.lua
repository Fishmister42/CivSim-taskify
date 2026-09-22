-- lua/gamecore/religion.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: religion.state (catalogs/observations/religion.yaml),
-- capability_id: religion.read.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- ACCESSOR AUDIT (2026-09-21, specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md):
-- `Game.GetReligion():GetAvailableBeliefs(playerId)` exists in none of Firaxis' 645 shipped Lua
-- files and is not a registered engine binding. Under its `pcall` the list stayed `[]` on every
-- step, so all three religion actions (`religion.select_pantheon`, `religion.found_religion`,
-- `religion.select_belief`), whose availability predicates all read `target in
-- player.available_beliefs`, could never become available. There is no engine enumerator at all:
-- Firaxis iterates the static GameInfo.Beliefs() table and filters it with three real predicates,
-- which is what this file does now. Cited inline. UNVERIFIED LIVE.
--
-- Parity note: pantheon/religion state is reported only for the local player and only the
-- majority religion of a visible city (exactly what the standard religion overview and city
-- banners show) — never an opponent's undisclosed belief choices or founding intentions.

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

local function CivSim_Religion_Try(fn)
    local ok, value = pcall(fn)
    if ok then return value end
    return nil
end

-- --------------------------------------------------------------------------
-- The beliefs the game's own chooser would offer right now.
--
-- SOURCE (this machine, 2026-09-21; steamassets/base/assets/ui/):
--   choosers/pantheonchooser.lua:29-40,:69-77 -- before a pantheon, the chooser walks
--     `for row in GameInfo.Beliefs()` and keeps a row when
--       not Game.GetReligion():IsInSomePantheon(row.Index)
--       and not Game.GetReligion():IsInSomeReligion(row.Index)
--       and row.BeliefClassType == "BELIEF_CLASS_PANTHEON"
--   religionscreen.lua:446-469 -- for a religion, the same two predicates plus
--     not Game.GetReligion():IsTooManyForReligion(row.Index, <religion type>), and the class
--     filter inverts: everything EXCEPT "BELIEF_CLASS_PANTHEON" (:465-466)
--
-- There is no "give me the available beliefs" call anywhere in the shipped corpus; this is how
-- the game itself answers the question. The list this body reports is the one the chooser a human
-- would have open right now would show: pantheon-class beliefs before a pantheon is chosen,
-- religion beliefs after.
-- --------------------------------------------------------------------------
local function CivSim_Religion_AvailableBeliefs(pantheonSelected, ownReligionIndex)
    local beliefs = {}
    local gameReligion = CivSim_Religion_Try(function() return Game.GetReligion() end)
    if gameReligion == nil then
        return beliefs, "game_religion_unavailable"
    end
    local answered = false
    for row in GameInfo.Beliefs() do
        -- Each predicate resolves the belief definition from this index (religionscreen.lua:462-464
        -- passes row.Index of InGame rows, always numeric); a non-number is never handed to the
        -- engine -- a native fault is not catchable (2026-09-21 20:46 segfault, government.lua).
        local beliefIndex = row.Index
        local inPantheon, inReligion = nil, nil
        if type(beliefIndex) == "number" then
            inPantheon = CivSim_Religion_Try(function()
                return gameReligion:IsInSomePantheon(beliefIndex)
            end)
            inReligion = CivSim_Religion_Try(function()
                return gameReligion:IsInSomeReligion(beliefIndex)
            end)
        end
        if type(inPantheon) == "boolean" or type(inReligion) == "boolean" then answered = true end
        local taken = (inPantheon == true) or (inReligion == true)
        local classMatches
        if not pantheonSelected then
            classMatches = (row.BeliefClassType == "BELIEF_CLASS_PANTHEON")
        else
            classMatches = (row.BeliefClassType ~= "BELIEF_CLASS_PANTHEON")
            if classMatches and type(ownReligionIndex) == "number" and ownReligionIndex ~= -1
                and type(beliefIndex) == "number" then
                local tooMany = CivSim_Religion_Try(function()
                    return gameReligion:IsTooManyForReligion(beliefIndex, ownReligionIndex)
                end)
                if tooMany == true then classMatches = false end
            end
        end
        if classMatches and not taken and row.BeliefType ~= nil then
            beliefs[#beliefs + 1] = row.BeliefType
        end
    end
    if not answered then
        -- Never a silent `[]`: without the two predicates this body cannot tell a taken belief
        -- from an offered one, and an unfiltered GameInfo dump is not what the chooser shows.
        return {}, "belief_availability_predicates_unanswerable"
    end
    return beliefs, nil
end

-- VERIFIED (P3, per-context probe: spikes/sweep-raw/GameCore_Tuner__P3_congress_greatpeople_religion.txt):
-- `Game.GetReligion()` as a global religion-game singleton is confirmed to exist as a function in
-- both InGame and this file's own GameCore_Tuner context (one of only 4 of the 8 manager
-- accessors present in GameCore_Tuner — see lua/gamecore/congress.lua's header for the full
-- breakdown and the contrasting case where the accessor is GameCore_Tuner-absent).
-- `Player:GetReligion():GetPantheon()` returns a GameInfo.Beliefs row INDEX, or < 0 for none
-- (base/assets/ui/religionscreen.lua:121, :311; base/assets/ui/launchbar.lua:138) -- both real,
-- both left as they were.
local function CivSim_Religion_GetState()
    local localPlayer = Game.GetLocalPlayer()
    -- Shipped screens return before touching a player object when the local player is -1
    -- (greatpeoplepopup.lua:690-692); nothing below may be asked about player -1.
    if type(localPlayer) ~= "number" or localPlayer < 0 then
        return {
            pantheon_selected = false,
            religion_founded = false,
            own_religion = nil,
            available_beliefs = {},
            city_majority_religions = {},
            available_beliefs_reason = "no_local_player",
        }
    end
    local player = Players[localPlayer]
    local religionMgr = nil
    if player ~= nil then
        religionMgr = CivSim_Religion_Try(function() return player:GetReligion() end)
    end

    local pantheonSelected = false
    local ok1, pantheonType = pcall(function() return religionMgr:GetPantheon() end) -- UNVERIFIED
    if ok1 and pantheonType and pantheonType ~= -1 then
        pantheonSelected = true
    end

    local religionFounded = false
    local ownReligionType = nil
    local ok2, foundedType = pcall(function() return religionMgr:GetReligionTypeCreated() end) -- UNVERIFIED
    if ok2 and foundedType and foundedType ~= -1 then
        religionFounded = true
        ownReligionType = GameInfo.Religions[foundedType].ReligionType -- UNVERIFIED
    end

    local availableBeliefs, beliefsReason =
        CivSim_Religion_AvailableBeliefs(pantheonSelected, foundedType)

    -- Majority religion of visible cities only (what a city banner shows).
    -- VERIFIED (P4 spot-check): PlayerManager.GetAlive() exists, replacing the unconfirmed
    -- GetAliveMajors() guess. UNVERIFIED: whether it returns majors only or all alive players;
    -- Player:IsMajor() is applied defensively (kept if unavailable) as in lua/gamecore/cities.lua.
    local cityReligions = {}
    for _, otherPlayer in ipairs(PlayerManager.GetAlive()) do
        if not otherPlayer.IsMajor or otherPlayer:IsMajor() then -- UNVERIFIED: Player:IsMajor()
            for _, city in otherPlayer:GetCities():Members() do
                local plot = Map.GetPlot(city:GetX(), city:GetY())
                local isOwn = (otherPlayer:GetID() == localPlayer)
                if isOwn or (plot ~= nil and PlayersVisibility[localPlayer]:IsVisible(plot:GetX(), plot:GetY())) then
                    local ok4, majorityReligion = pcall(function()
                        return city:GetReligion():GetMajorityReligion() -- UNVERIFIED: City:GetReligion()
                    end)
                    if ok4 and majorityReligion and majorityReligion ~= -1 then
                        cityReligions[#cityReligions + 1] = {
                            city_id = city:GetID(),
                            majority_religion = GameInfo.Religions[majorityReligion].ReligionType, -- UNVERIFIED
                        }
                    end
                end
            end
        end
    end

    local state = {
        pantheon_selected = pantheonSelected,
        religion_founded = religionFounded,
        own_religion = ownReligionType,
        available_beliefs = availableBeliefs,
        city_majority_religions = cityReligions,
    }
    if beliefsReason ~= nil then state.available_beliefs_reason = beliefsReason end
    return state
end

CivSim_Religion = {
    state = CivSim_Religion_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Religion.state()))
