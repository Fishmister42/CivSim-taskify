-- lua/ingame/espionage.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/espionage.lua rather
-- than this file.
-- Backs declaration_id: espionage.assign_mission (catalogs/actions/espionage.yaml),
-- capability_id: espionage.orders.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- UNVERIFIED (whole file): the InGame spy-mission-assignment Lua surface is not confidently known,
-- and this file's domain (espionage) was not covered by the live-client sweep at all. Assumed by
-- analogy with the Unit/City operation-request pattern used elsewhere in this catalog. The spy
-- lookup below was corrected from a `UnitManager.GetUnit(playerID, unitID)` guess (never
-- confirmed to exist, and the sweep separately confirmed `Units.GetUnit` — a related but distinct
-- guess — is `nil`) to the `Players`-based lookup pattern lua/ingame/unit_orders.lua now uses,
-- since a spy is itself a unit; this substitution is a defensive correction by analogy, not
-- itself independently spike-verified for the espionage domain.
--
-- Parity note: a mission may only be assigned to one of the local player's own spies, against a
-- target city currently offered on the standard espionage panel for that spy — never a mission
-- against a target the human could not select there.

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

local function CivSim_FindLocalSpyUnit(spyUnitId)
    local localPlayer = Game.GetLocalPlayer()
    local units = Players[localPlayer]:GetUnits()
    for _, u in units:Members() do
        if u:GetID() == spyUnitId then
            return u
        end
    end
    return nil
end

-- ACCESSOR AUDIT (2026-09-21, specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md):
-- `UnitOperationTypes.SPY_MISSION` and `UnitOperationTypes.PARAM_SPY_MISSION` exist nowhere -- not
-- in Firaxis' shipped Lua, not as engine strings. Both read `nil`, so `tParameters[nil] = ...`
-- raised and the operation type matched nothing. There is no generic "spy mission" operation.
--
-- SOURCE (this machine, 2026-09-21; steamassets/base/assets/ui/):
--   choosers/espionagechooser.lua:196-236 -- the legal missions for a city are enumerated from the
--     database: `for operation in GameInfo.UnitOperations()` where
--     `operation.CategoryInUI == "OFFENSIVESPY"`, each tested with
--     `UnitManager.CanStartOperation(spy, operation.Hash, cityPlot, false, true)`; the failures
--     come back as results[UnitOperationResults.FAILURE_REASONS] (:234).
--   popups/espionagepopup.lua:472-477 -- OnAccept, the button that actually starts the mission,
--     issues `UnitManager.RequestOperation(spy, operation.Hash)` with NO parameter table: the spy
--     is already standing in the target city, so there is nothing to address.
--   choosers/espionagechooser.lua:612-616 -- travelling to a different city is a different
--     operation, SPY_TRAVEL_NEW_CITY, and that one does take PARAM_X/PARAM_Y.
--   popups/espionagepopup.lua:328-333 -- renewing passes PARAM_X/PARAM_Y as well.
-- The real constants are SPY_COUNTERSPY, SPY_GAIN_SOURCES, SPY_GREAT_WORK_HEIST,
-- SPY_LISTENING_POST, SPY_SIPHON_FUNDS, SPY_STEAL_TECH_BOOST, SPY_TRAVEL_NEW_CITY; the only
-- UnitOperationTypes.PARAM_* keys that exist are PARAM_X, PARAM_Y, PARAM_FLAGS,
-- PARAM_IMPROVEMENT_TYPE, PARAM_MODIFIERS, PARAM_OPERATION_TYPE and PARAM_WMD_TYPE.
--
-- `missionType` is a UnitOperations OperationType name, matching what espionage.state reports as a
-- spy's `mission`. The operation is refused unless the game itself says it can start, so the
-- harness never issues a mission the chooser would have greyed out.
-- UNVERIFIED LIVE.
local function CivSim_Espionage_AssignMission(spyUnitId, missionType, targetCityId)
    local unit = CivSim_FindLocalSpyUnit(spyUnitId)
    if unit == nil then
        return { ok = false, reason = "spy_not_found", spy_unit_id = spyUnitId }
    end
    local okRow, row = pcall(function() return GameInfo.UnitOperations[missionType] end)
    if not okRow or type(row) ~= "table" or row.Hash == nil then
        return { ok = false, reason = "unknown_mission", spy_unit_id = spyUnitId, mission = missionType }
    end
    if row.CategoryInUI ~= "OFFENSIVESPY" then
        -- espionagechooser.lua:211 -- only this category is on the mission list a human sees.
        return {
            ok = false,
            reason = "mission_not_offered_by_the_espionage_panel",
            spy_unit_id = spyUnitId,
            mission = missionType,
        }
    end
    local okPlot, plot = pcall(function() return Map.GetPlot(unit:GetX(), unit:GetY()) end)
    if not okPlot or plot == nil then
        return { ok = false, reason = "spy_plot_unreadable", spy_unit_id = spyUnitId }
    end
    local okCan, canStart = pcall(function()
        return UnitManager.CanStartOperation(unit, row.Hash, plot, false, true)
    end)
    if not okCan then
        return { ok = false, reason = "can_start_operation_unanswerable", spy_unit_id = spyUnitId }
    end
    if canStart ~= true then
        return {
            ok = false,
            reason = "mission_button_is_greyed_out",
            spy_unit_id = spyUnitId,
            mission = missionType,
        }
    end
    local okRequest, err = pcall(function()
        -- espionagepopup.lua:473 -- no parameter table; the spy acts where it stands.
        UnitManager.RequestOperation(unit, row.Hash)
    end)
    if not okRequest then
        return {
            ok = false,
            reason = "UnitManager.RequestOperation errored: " .. tostring(err),
            spy_unit_id = spyUnitId,
            mission = missionType,
        }
    end
    return {
        ok = true,
        spy_unit_id = spyUnitId,
        mission = missionType,
        target_city_id = targetCityId,
        mechanism = "UnitManager.RequestOperation(spy, <OFFENSIVESPY operation hash>)",
    }
end

CivSim_EspionageOrders = {
    assign_mission = CivSim_Espionage_AssignMission,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_EspionageOrders.assign_mission(88, "MISSION_STEAL_TECH_BOOST", 12)))
