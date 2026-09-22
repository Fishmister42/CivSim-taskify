-- lua/gamecore/research.lua
-- Context: InGame (read-only) -- see the block above the civics loop. The file's path is
-- historical; the context a body runs in is the `context:` key of its catalog declaration, not
-- its directory (the same split 882758e left behind for government/great_people/religion).
-- Backs declaration_id: research.state (catalogs/observations/research.yaml), capability_id: research.read.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- This file's own accessors (Player:GetTechs(), Player:GetCulture(), GameInfo.Technologies()
-- iteration, etc.) were not covered by the live-client sweep and remain unconfirmed guesses; the
-- per-call UNVERIFIED markers below are left as-is because the sweep did not test them.
--
-- Parity note: reports only the local player's own tech tree progress and choices, matching the
-- standard Research/Civics tree screen. No opponent research or civic progress — FR-019
-- explicitly names "other civilizations' undisclosed research or civic progress" as forbidden.

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

local function CivSim_Research_GetState()
    -- The check every shipped screen makes first (the 882758e guard): never touch a player object
    -- with no local player, and say so rather than answering with an empty list.
    local localPlayer = Game.GetLocalPlayer()
    if localPlayer == nil or localPlayer == -1 then
        return {
            researchable_techs = {},
            researchable_civics = {},
            researchable_techs_reason = "no_local_player",
            researchable_civics_reason = "no_local_player",
        }
    end
    local player = Players[localPlayer]
    local techs = player:GetTechs()   -- Player:GetTechs()
    local culture = player:GetCulture() -- Player:GetCulture()

    -- MEASURED (2026-09-22, static): `researchable_civics` was `[]` on every record in the store
    -- -- 914 decision steps and every block probe of 2026-09-21/22 -- while `researchable_techs`,
    -- built by the loop directly above it, was non-empty at every one, and `current_civic` (read
    -- off this same `culture` object, two lines below) was populated. That asymmetry is the whole
    -- finding, and it is a CONTEXT fault, not a missing name:
    --
    --   nm -DC libGameCore_XP2.so:
    --     GameCore::Lua::IPlayerTechs::lCanResearch              <- GameCore_Tuner side: PRESENT
    --     GameCore::Cache::Lua::IPlayerTechs::lCanResearch       <- InGame side:          present
    --     GameCore::Lua::IPlayerCulture::lGetProgressingCivic    <- GameCore_Tuner side: PRESENT
    --     GameCore::Cache::Lua::IPlayerCulture::lCanProgress     <- InGame side:          present
    --     (GameCore::Lua::IPlayerCulture::lCanProgress           <- GameCore_Tuner side: ABSENT)
    --
    -- `GameCore::Cache::Lua::` is the InGame binding and `GameCore::Lua::` the GameCore_Tuner one
    -- -- pinned by T213's own measurements: `Unit:GetUnitType()` is nil in GameCore_Tuner and
    -- answers InGame, and `lGetUnitType` exists only under `Cache`; `GetMovesRemaining` answers in
    -- both and is bound in both. So in GameCore_Tuner `culture.CanProgress` is nil, the
    -- `culture.CanProgress and` guard short-circuited on every row, and the field became an `[]`
    -- indistinguishable from "the game is offering no civic" -- which made
    -- `research.set_civic`'s availability predicate (`target in player.researchable_civics`)
    -- unsatisfiable for the whole history of the harness.
    --
    -- Firaxis calls it only from InGame screens, both of them, and calls CanResearch from the
    -- matching pair:
    --   base/assets/ui/screens/civicstree.lua:1279   playerCulture:CanProgress(civicID)
    --   base/assets/ui/choosers/civicschooser.lua:68 pPlayerCulture:CanProgress(iCivic)
    --   base/assets/ui/screens/techtree.lua:1107     playerTechs:CanResearch(techID)
    --   base/assets/ui/choosers/researchchooser.lua:71 pPlayerTechs:CanResearch(iTech)
    -- `CanResearch` only ever worked here because it happens to be bound on BOTH sides.
    --
    -- This declaration therefore moves to context InGame (catalogs/observations/research.yaml),
    -- the context both of its trees run in -- the cities.state precedent 1d0b372, and the same
    -- move 882758e made for government/great_people/religion. Every accessor this body names is
    -- bound on the Cache/InGame side, so nothing else in it changes meaning.
    -- UNVERIFIED LIVE: no client was launched for this change.
    local researchableTechs = {}
    local techsReason = nil
    if techs.CanResearch == nil then
        techsReason = "can_research_unavailable_in_context"
    else
        for row in GameInfo.Technologies() do -- UNVERIFIED: GameInfo table iteration idiom
            local techType = row.Index -- UNVERIFIED: index vs hash usage
            if type(techType) == "number" and techs:CanResearch(techType) then
                researchableTechs[#researchableTechs + 1] = row.TechnologyType
            end
        end
    end

    local researchableCivics = {}
    local civicsReason = nil
    if culture.CanProgress == nil then
        -- Never a silent `[]`: an accessor this context does not bind is "I cannot answer", not
        -- "the game is offering nothing".
        civicsReason = "can_progress_unavailable_in_context"
    else
        for row in GameInfo.Civics() do -- UNVERIFIED
            local civicType = row.Index
            if type(civicType) == "number" and culture:CanProgress(civicType) then
                researchableCivics[#researchableCivics + 1] = row.CivicType
            end
        end
    end

    local currentTechType = nil
    local currentTechHash = techs.GetResearchingTech and techs:GetResearchingTech() or -1 -- UNVERIFIED
    if currentTechHash and currentTechHash ~= -1 then
        currentTechType = GameInfo.Technologies[currentTechHash].TechnologyType -- UNVERIFIED
    end

    local currentCivicType = nil
    local currentCivicHash = culture.GetProgressingCivic and culture:GetProgressingCivic() or -1 -- UNVERIFIED
    if currentCivicHash and currentCivicHash ~= -1 then
        currentCivicType = GameInfo.Civics[currentCivicHash].CivicType -- UNVERIFIED
    end

    return {
        current_research = currentTechType,
        current_civic = currentCivicType,
        researchable_techs = researchableTechs,
        researchable_civics = researchableCivics,
        -- Present only when the list could not be asked at all. A Lua table cannot carry a nil
        -- key, so a nil reason simply does not appear in the encoded object.
        researchable_techs_reason = techsReason,
        researchable_civics_reason = civicsReason,
    }
end

CivSim_Research = {
    state = CivSim_Research_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Research.state()))
