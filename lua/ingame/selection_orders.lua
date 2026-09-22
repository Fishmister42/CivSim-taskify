-- lua/ingame/selection_orders.lua
-- Context: InGame.
-- Backs declaration_ids: units.select, cities.select (catalogs/actions/units.yaml,
-- catalogs/actions/cities.yaml), capability_id: selection.orders.
--
-- Gameplay day, 2026-09-21. Every unit order acts on "the unit the game shows as selected" and
-- every city order's availability begins `city.is_selected and ...` -- and nothing in the catalog
-- ever *changed* the selection. A human does it by clicking the unit or the city: the game
-- selects it, the unit's orders (or the city panel) appear, and the next order goes to it. With
-- one unit the game's own auto-selection covered it; with a city it never did (block 2: the
-- model wrote "no city currently selected (so city actions cannot act on it)" and every city
-- order stayed structurally unavailable). These two functions are exactly that click:
-- `UI.SelectUnit` / `UI.SelectCity`, the calls Firaxis's own unit-flag and city-banner scripts
-- make on a click (unitflagmanager.lua, citybannermanager.lua). MEASURED live from the InGame
-- tuner state at game turn 25: `UI.SelectCity(city)` made `UI.GetHeadSelectedCity()` answer that
-- city and opened its CityPanel; `UI.SelectUnit(unit)` likewise; `UI.DeselectAll()` cleared both.
--
-- Parity note (Principle I): selecting is a click on something already on the human's screen;
-- it reveals nothing and changes no game state. Only the local player's own units and cities
-- are ever selected here -- the id is resolved against the local player, so another player's id
-- resolves to nil and the call reports `ok = false` rather than selecting anything.
--
-- SANDBOX CONSTRAINT (spikes/lua-api-verification-linux.md, P5): no `require`, no shared module;
-- this file carries its own JSON encoder like every other capability file.

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

-- Select one of the local player's units, as a click on it would. Reports what the game then
-- says is selected; the harness verifies through units.state's `is_selected`, never from here.
local function CivSim_SelectionOrders_SelectUnit(unitId)
    local localPlayer = Game.GetLocalPlayer()
    local okU, unit = pcall(function() return UnitManager.GetUnit(localPlayer, unitId) end)
    if not okU or unit == nil then
        return { ok = false, reason = "no such unit for the local player", unit_id = unitId }
    end
    local okS, err = pcall(function() UI.SelectUnit(unit) end)
    if not okS then
        return { ok = false, reason = "UI.SelectUnit errored: " .. tostring(err), unit_id = unitId }
    end
    local okH, head = pcall(function() return UI.GetHeadSelectedUnit() end)
    local headId = (okH and head ~= nil) and head:GetID() or -1
    return { ok = true, unit_id = unitId, selected_unit_id = headId }
end

-- Select one of the local player's cities, as a click on its banner would (the city panel opens).
local function CivSim_SelectionOrders_SelectCity(cityId)
    local localPlayer = Game.GetLocalPlayer()
    local okC, city = pcall(function() return CityManager.GetCity(localPlayer, cityId) end)
    if not okC or city == nil then
        return { ok = false, reason = "no such city for the local player", city_id = cityId }
    end
    local okS, err = pcall(function() UI.SelectCity(city) end)
    if not okS then
        return { ok = false, reason = "UI.SelectCity errored: " .. tostring(err), city_id = cityId }
    end
    local okH, head = pcall(function() return UI.GetHeadSelectedCity() end)
    local headId = (okH and head ~= nil) and head:GetID() or -1
    return { ok = true, city_id = cityId, selected_city_id = headId }
end

CivSim_SelectionOrders = {
    select_unit = CivSim_SelectionOrders_SelectUnit,
    select_city = CivSim_SelectionOrders_SelectCity,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_SelectionOrders.select_city(65536)))
