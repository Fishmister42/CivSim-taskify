-- lua/ingame/city_selection.lua
-- Context: InGame (read-only).
-- Backs declaration_id: cities.selection (catalogs/observations/cities.yaml),
-- capability_id: cities.selection.
--
-- T255 (2026-09-21). Every city order's availability predicate begins `city.is_selected and ...`
-- (catalogs/actions/cities.yaml, policies.assign_governor), and `cities.state` -- the body that
-- lists the cities -- runs in GameCore_Tuner, where `UI` does not exist, so no observation has
-- ever produced `city.is_selected` and no city order has ever been dispatchable (the unit half
-- of the same gap was measured and fixed in lua/gamecore/units.lua on 2026-09-21). This file is
-- the InGame half: it reports which city, if any, the game currently has selected --
-- `UI.GetHeadSelectedCity()`, the city whose panel the human has open, used by Firaxis's own
-- CityPanel/ProductionPanel scripts -- and act/predicates.py overlays that id onto the bound
-- `city` namespace. `cities.state` itself deliberately stays in GameCore_Tuner: its body is the
-- live-verified one (T213, 14/14), and its `GetAvailableProduction` call appears in no shipped
-- InGame UI file, so moving it would trade a measured pass for a guess.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- Parity note: "which city is selected" is the single most visible fact on a human's screen (the
-- city panel is open for it). Nothing here reads a city the human has not clicked, and nothing
-- here reads any city's contents -- that is cities.state's job, with its own parity basis.
-- UNVERIFIED LIVE that `UI.GetHeadSelectedCity()` answers from the InGame tuner state (it is
-- used by 12 shipped InGame UI files, and its unit twin `UI.GetHeadSelectedUnit()` was measured
-- live in units.state); every call is pcall'd and an unanswerable read reports `has_selection =
-- false` with the reason, never a guessed id.

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

-- The one read. `has_selection` is always present so the schema can require it; the id and owner
-- fields are present only when a city is selected (a JSON `null` would need the encoder to emit
-- absent table keys, which Lua tables cannot hold).
local function CivSim_CitySelection_Read()
    local okSel, selected = pcall(function() return UI.GetHeadSelectedCity() end)
    if not okSel then
        return { has_selection = false, reason = "UI.GetHeadSelectedCity errored: " .. tostring(selected) }
    end
    if selected == nil then
        return { has_selection = false }
    end
    local okId, cityId = pcall(function() return selected:GetID() end)
    local okOwner, owner = pcall(function() return selected:GetOwner() end)
    local okLocal, localPlayer = pcall(function() return Game.GetLocalPlayer() end)
    if not okId or type(cityId) ~= "number" then
        return { has_selection = false, reason = "selected city has no readable id" }
    end
    return {
        has_selection = true,
        selected_city_id = cityId,
        owner_player_id = (okOwner and type(owner) == "number") and owner or -1,
        owner_is_local_player = (okOwner and okLocal and owner == localPlayer) or false,
    }
end

CivSim_CitySelection = {
    selection = CivSim_CitySelection_Read,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_CitySelection.selection()))
