-- lua/ingame/espionage.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/espionage.lua rather
-- than this file.
-- Backs declaration_id: espionage.assign_mission (catalogs/actions/espionage.yaml),
-- capability_id: espionage.orders.
--
-- UNVERIFIED (whole file): the InGame spy-mission-assignment Lua surface is not confidently known.
-- Assumed by analogy with the Unit/City operation-request pattern used elsewhere in this catalog.
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

local function CivSim_Espionage_AssignMission(spyUnitId, missionType, targetCityId)
    local localPlayer = Game.GetLocalPlayer()
    local unit = UnitManager.GetUnit(localPlayer, spyUnitId) -- UNVERIFIED
    if unit == nil then
        return { ok = false, reason = "spy_not_found" }
    end
    local tParameters = {}
    tParameters[UnitOperationTypes.PARAM_SPY_MISSION] = missionType -- UNVERIFIED
    tParameters[UnitOperationTypes.PARAM_X] = targetCityId -- UNVERIFIED: placeholder until the real
    -- target-addressing parameter (city ID vs. plot coordinate) is confirmed
    local accepted = UnitManager.RequestOperation(unit, UnitOperationTypes.SPY_MISSION, tParameters) -- UNVERIFIED
    return { ok = (accepted ~= false), spy_unit_id = spyUnitId, mission = missionType, target_city_id = targetCityId }
end

CivSim_EspionageOrders = {
    assign_mission = CivSim_Espionage_AssignMission,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_EspionageOrders.assign_mission(88, "MISSION_STEAL_TECH_BOOST", 12)))
