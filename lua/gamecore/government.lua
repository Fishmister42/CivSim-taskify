-- lua/gamecore/government.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: government.state (catalogs/observations/government.yaml),
-- capability_id: government.read.
--
-- Parity note: reports only the local player's own government, policy slots, and governor
-- assignments — everything visible on the standard Government screen. No opponent government
-- data (their form of government is generally public in Civ VI's diplomacy overview, but their
-- policy slotting and governor assignments are not, and are not reported here).

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

-- UNVERIFIED: Civ VI's government/policy accessor surface. The base game exposes government type
-- and active policy cards through Player:GetCulture(), but the exact method names for "current
-- government", "unlocked governments", and "slotted policies by slot type" are not confirmed.
local function CivSim_Government_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local player = Players[localPlayer]
    local culture = player:GetCulture()

    local currentGovernment = nil
    local ok, govType = pcall(function() return culture:GetCurrentGovernment() end) -- UNVERIFIED
    if ok and govType and govType ~= -1 then
        currentGovernment = GameInfo.Governments[govType].GovernmentType -- UNVERIFIED
    end

    local availablePolicies = {}
    local okPolicies, policyList = pcall(function() return culture:GetSlottablePolicies() end) -- UNVERIFIED
    if okPolicies and type(policyList) == "table" then
        for _, policyType in ipairs(policyList) do
            availablePolicies[#availablePolicies + 1] = GameInfo.Policies[policyType].PolicyType -- UNVERIFIED
        end
    end

    local availableGovernments = {}
    for row in GameInfo.Governments() do -- UNVERIFIED: GameInfo table iteration idiom
        local okUnlocked, unlocked = pcall(function() return culture:HasGovernment(row.Index) end) -- UNVERIFIED
        if okUnlocked and unlocked and row.GovernmentType ~= currentGovernment then
            availableGovernments[#availableGovernments + 1] = row.GovernmentType
        end
    end

    local governors = {}
    local availableGovernors = {}
    local okGov, governorList = pcall(function() return player:GetGovernors() end) -- UNVERIFIED: Player:GetGovernors()
    if okGov and governorList ~= nil and governorList.Members then
        for _, governor in governorList:Members() do
            local governorType = governor.GetType and governor:GetType() or nil -- UNVERIFIED
            local assignedCityId = governor.GetAssignedCityID and governor:GetAssignedCityID() or nil -- UNVERIFIED
            governors[#governors + 1] = {
                governor_type = governorType,
                assigned_city_id = assignedCityId,
            }
            if governorType ~= nil and assignedCityId == nil then
                availableGovernors[#availableGovernors + 1] = governorType
            end
        end
    end

    return {
        current_government = currentGovernment,
        available_governments = availableGovernments,
        available_policies = availablePolicies,
        governors = governors,
        available_governors = availableGovernors,
    }
end

CivSim_Government = {
    state = CivSim_Government_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Government.state()))
