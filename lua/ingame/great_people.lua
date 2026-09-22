-- lua/ingame/great_people.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/great_people.lua
-- rather than this file.
-- Backs declaration_id: great_people.recruit (catalogs/actions/great_people.yaml),
-- capability_id: great_people.orders.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- CORRECTED/CONFIRMED against a live client (spike P3): `Game.GetGreatPeople()` itself is
-- confirmed to exist in InGame (see lua/gamecore/great_people.lua's header). The recruit/patronize
-- method called on it below (`:Recruit(...)`) remains an unconfirmed guess — existence of the
-- accessor is not arity of its methods.
--
-- Parity note: only an individual already reported by CivSim_GreatPeople.state()'s
-- recruitable_individuals for the local player may be recruited — exactly the choice the standard
-- Great People screen would currently offer.

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

-- ACCESSOR AUDIT (2026-09-21): `Game.GetGreatPeople():Recruit(...)` exists in none of Firaxis'
-- shipped Lua files and is not a registered engine binding, so this order raised inside its pcall
-- and reported `ok = false` every time it was ever dispatched. (It was never dispatched: the read
-- side's own phantom kept `recruitable_individuals` empty, so the action was never available.)
--
-- SOURCE (this machine, 2026-09-21; steamassets/base/assets/ui/popups/greatpeoplepopup.lua):
--   :886-894  OnRecruitButtonClick -- the Recruit button issues
--     UI.RequestPlayerOperation(Game.GetLocalPlayer(), PlayerOperations.RECRUIT_GREAT_PERSON,
--       { [PlayerOperations.PARAM_GREAT_PERSON_INDIVIDUAL_TYPE] = individualID })
--     with exactly one parameter key, whose value is the GreatPersonIndividuals row INDEX
--     (:267 plumbs kPerson.IndividualID, which is entry.Individual from :726) -- an index, NOT a
--     hash, unlike the religion operations. The request answers nothing; great_people.state's own
--     re-read is what confirms it, never the `ok` here.
-- UNVERIFIED LIVE.
local function CivSim_GreatPeople_Recruit(individualId)
    if type(individualId) ~= "number" then
        return { ok = false, reason = "unknown_individual", individual_id = individualId }
    end
    local ok, err = pcall(function()
        local tParameters = {}
        tParameters[PlayerOperations.PARAM_GREAT_PERSON_INDIVIDUAL_TYPE] = individualId
        UI.RequestPlayerOperation(
            Game.GetLocalPlayer(), PlayerOperations.RECRUIT_GREAT_PERSON, tParameters)
    end)
    if not ok then
        return {
            ok = false,
            reason = "UI.RequestPlayerOperation errored: " .. tostring(err),
            individual_id = individualId,
        }
    end
    return {
        ok = true,
        individual_id = individualId,
        mechanism = "PlayerOperations.RECRUIT_GREAT_PERSON",
    }
end

CivSim_GreatPeopleOrders = {
    recruit = CivSim_GreatPeople_Recruit,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_GreatPeopleOrders.recruit(14)))
