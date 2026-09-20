-- lua/gamecore/great_people.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: great_people.state (catalogs/observations/great_people.yaml),
-- capability_id: great_people.read.
--
-- UNVERIFIED (whole file): `Game.GetGreatPeople()` as a global manager is believed to exist
-- (referenced in community modding discussions of great-person points and recruitment), but the
-- exact methods for "currently recruitable individuals" and "this player's points per class" are
-- not confirmed against a live client.
--
-- Parity note: reports only great people currently recruitable/visible to the local player on the
-- standard Great People screen (available individuals and the local player's own accumulated
-- points per class) — never another civilization's point totals, which are not shown to the human
-- player either.

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

local function CivSim_GreatPeople_GetState()
    local localPlayer = Game.GetLocalPlayer()

    local recruitable = {}
    local ok1, gpMgr = pcall(function() return Game.GetGreatPeople() end) -- UNVERIFIED
    if ok1 and gpMgr ~= nil and gpMgr.GetAvailableIndividuals then
        for _, individual in ipairs(gpMgr:GetAvailableIndividuals(localPlayer)) do -- UNVERIFIED
            recruitable[#recruitable + 1] = {
                individual_id = individual.Index, -- UNVERIFIED
                class_type = individual.ClassType, -- UNVERIFIED
                name = individual.Name, -- UNVERIFIED
            }
        end
    end

    local pointsByClass = {}
    if ok1 and gpMgr ~= nil and gpMgr.GetPlayerPoints then
        for row in GameInfo.GreatPersonClasses() do -- UNVERIFIED: GameInfo table name
            local ok2, points = pcall(function()
                return gpMgr:GetPlayerPoints(localPlayer, row.Index) -- UNVERIFIED
            end)
            if ok2 then
                pointsByClass[#pointsByClass + 1] = { class_type = row.GreatPersonClassType, points = points } -- UNVERIFIED
            end
        end
    end

    return {
        recruitable_individuals = recruitable,
        points_by_class = pointsByClass,
    }
end

CivSim_GreatPeople = {
    state = CivSim_GreatPeople_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_GreatPeople.state()))
