-- lua/gamecore/government.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: government.state (catalogs/observations/government.yaml),
-- capability_id: government.read.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- ACCESSOR AUDIT (2026-09-21, specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md):
-- three accessors in this file named methods that exist nowhere in Firaxis' shipped UI Lua, each
-- behind a guard that swallowed the absence and returned a silent empty value:
--   * `culture:GetSlottablePolicies()` -- no such method; `available_policies` was `[]` always, so
--     `policies.slot_policy` (`target in player.available_policies`) could never be available.
--   * `culture:HasGovernment(index)` -- no such method; `available_governments` was `[]` always, so
--     `policies.change_government` could never be available.
--   * `governorList.Members` / `governor:GetAssignedCityID()` -- neither exists; `governors` and
--     `available_governors` were `[]` always, so `policies.assign_governor` could never be
--     available.
-- Each is replaced below by the accessor Firaxis' own Government screen / Governors panel uses,
-- cited inline. Where a value cannot be read, the body now reports `<field>_reason` and never a
-- silent `[]` -- the convention 1d0b372 (cities) and 6606d4b (units) established.
-- UNVERIFIED LIVE (whole file): read out of Firaxis' callers on this machine, not yet exercised
-- against a running client.
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

local function CivSim_Government_Try(fn)
    local ok, value = pcall(fn)
    if ok then return value end
    return nil
end

-- --------------------------------------------------------------------------
-- The policy cards the Government screen's card picker would show right now.
--
-- SOURCE (this machine, 2026-09-21; steamassets/):
--   base/assets/ui/screens/governmentscreen.lua:2270-2273 -- the catalog is built by iterating
--     GameInfo.Policies() and asking IsPolicyAvailable(kPlayerCulture, row.Hash) per row; there is
--     no "give me the slottable policies" call anywhere in the shipped corpus.
--   base/assets/ui/screens/governmentscreen.lua:2224-2229 -- base IsPolicyAvailable:
--     kPlayerCulture:IsPolicyUnlocked(hash) and not kPlayerCulture:IsPolicyObsolete(hash)
--   dlc/expansion2/ui/replacements/governmentscreen_expansion2.lua:8-14 -- Gathering Storm replaces
--     it with: not IsPolicyBanned(hash) and CanPolicyBeSlotted(hash) and not IsPolicyObsolete(hash).
--     Preferred here when the build carries those two methods, exactly as the ruleset does.
--   base/assets/ui/screens/governmentscreen.lua:1063-1066 -- the picker then hides any card already
--     sitting in a slot (IsPolicyTypeActive), which is the `slotted` set built below from
--     :2284-2289 (GetNumPolicySlots / GetSlotPolicy, zero-based).
--
-- Every card listed is one whose picker row a human could drag into an open slot.
-- --------------------------------------------------------------------------
local function CivSim_Government_AvailablePolicies(culture)
    if culture == nil then return {}, "player_culture_unavailable" end
    local slotted = {}
    local slotCount = CivSim_Government_Try(function() return culture:GetNumPolicySlots() end)
    if type(slotCount) ~= "number" then
        -- governmentscreen.lua:2284 reads this before it can draw a single slot; without it the
        -- picker's "already slotted" filter cannot be applied and the list would overstate.
        return {}, "get_num_policy_slots_unanswerable"
    end
    for i = 0, slotCount - 1 do
        local policyIndex = CivSim_Government_Try(function() return culture:GetSlotPolicy(i) end)
        if type(policyIndex) == "number" and policyIndex ~= -1 then
            local row = CivSim_Government_Try(function() return GameInfo.Policies[policyIndex] end)
            if type(row) == "table" and row.PolicyType ~= nil then slotted[row.PolicyType] = true end
        end
    end
    -- Probe the predicate once, on the first row, so a build that answers none of them says so
    -- instead of reporting "no cards available".
    local probed, answered = false, false
    local available = {}
    for row in GameInfo.Policies() do
        local hash = row.Hash
        local obsolete = CivSim_Government_Try(function() return culture:IsPolicyObsolete(hash) end)
        local slottable = CivSim_Government_Try(function() return culture:CanPolicyBeSlotted(hash) end)
        local banned = CivSim_Government_Try(function() return culture:IsPolicyBanned(hash) end)
        local unlocked = nil
        if type(slottable) ~= "boolean" then
            unlocked = CivSim_Government_Try(function() return culture:IsPolicyUnlocked(hash) end)
        end
        if not probed then
            probed = true
            answered = (type(slottable) == "boolean" or type(unlocked) == "boolean")
        end
        local offered
        if type(slottable) == "boolean" then
            -- governmentscreen_expansion2.lua:9-13
            offered = (banned ~= true) and slottable and (obsolete ~= true)
        else
            -- governmentscreen.lua:2225-2228
            offered = (unlocked == true) and (obsolete ~= true)
        end
        if offered and row.PolicyType ~= nil and not slotted[row.PolicyType] then
            available[#available + 1] = row.PolicyType
        end
    end
    if not answered then
        return {}, "policy_availability_predicate_unanswerable"
    end
    return available, nil
end

-- --------------------------------------------------------------------------
-- The governors a human sees on the Governors panel, and which city each holds.
--
-- SOURCE (this machine, 2026-09-21; steamassets/):
--   dlc/expansion1/ui/additions/governorpanel.lua:54,58,85-87 -- pPlayer:GetGovernors() then
--     `local bHasGovernors, tGovernorList = playerGovernors:GetGovernorList()`; the list is a plain
--     array iterated with ipairs, and each entry answers :GetType() (a GameInfo.Governors INDEX).
--     Identical in dlc/expansion2/ui/additions/governorpanel.lua:54,58.
--   dlc/expansion1/ui/additions/governorsupport.lua:12-14 -- pGovernor:GetAssignedCity() returns a
--     CITY OBJECT (or nil), which the panel reads a name off; there is no GetAssignedCityID.
-- --------------------------------------------------------------------------
local function CivSim_Government_Governors(player)
    local governors = {}
    local availableGovernors = {}
    local playerGovernors = CivSim_Government_Try(function() return player:GetGovernors() end)
    if playerGovernors == nil then
        -- Rise & Fall and later only; a base-ruleset game has no Governors panel at all.
        return governors, availableGovernors, "player_governors_unavailable"
    end
    local ok, hasGovernors, list = pcall(function() return playerGovernors:GetGovernorList() end)
    if not ok then
        return governors, availableGovernors, "get_governor_list_unanswerable"
    end
    if hasGovernors ~= true or type(list) ~= "table" then
        return governors, availableGovernors, nil -- the panel shows no appointed governor yet
    end
    for _, governor in ipairs(list) do
        local typeIndex = CivSim_Government_Try(function() return governor:GetType() end)
        local row = nil
        if type(typeIndex) == "number" then
            row = CivSim_Government_Try(function() return GameInfo.Governors[typeIndex] end)
        end
        local governorType = (type(row) == "table" and row.GovernorType) or nil
        local city = CivSim_Government_Try(function() return governor:GetAssignedCity() end)
        local assignedCityId = nil
        if city ~= nil then
            local cityId = CivSim_Government_Try(function() return city:GetID() end)
            if type(cityId) == "number" then assignedCityId = cityId end
        end
        governors[#governors + 1] = {
            governor_type = governorType,
            assigned_city_id = assignedCityId,
        }
        if governorType ~= nil and assignedCityId == nil then
            availableGovernors[#availableGovernors + 1] = governorType
        end
    end
    return governors, availableGovernors, nil
end

local function CivSim_Government_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local player = Players[localPlayer]
    local culture = CivSim_Government_Try(function() return player:GetCulture() end)

    -- governmentscreen.lua:2250-2254 -- GetCurrentGovernment() returns a GameInfo.Governments row
    -- INDEX, or -1 before Code of Laws; the screen's own "no government" state is that sentinel.
    -- (`culture:HasGovernment(...)`, the method this file used to ask, exists nowhere in the
    -- shipped corpus.)
    local currentGovernment = nil
    local govType = CivSim_Government_Try(function() return culture:GetCurrentGovernment() end)
    if type(govType) == "number" and govType ~= -1 then
        local row = CivSim_Government_Try(function() return GameInfo.Governments[govType] end)
        if type(row) == "table" then currentGovernment = row.GovernmentType end
    end

    -- governmentscreen.lua:2334 -- IsGovernmentUnlocked takes a HASH (not the index
    -- GetCurrentGovernment returns); that asymmetry is Firaxis', not a typo here.
    local availableGovernments = {}
    local governmentsReason = nil
    if culture == nil then
        governmentsReason = "player_culture_unavailable"
    else
        local answered = false
        for row in GameInfo.Governments() do
            local unlocked = CivSim_Government_Try(function()
                return culture:IsGovernmentUnlocked(row.Hash)
            end)
            if type(unlocked) == "boolean" then answered = true end
            if unlocked == true and row.GovernmentType ~= currentGovernment then
                availableGovernments[#availableGovernments + 1] = row.GovernmentType
            end
        end
        if not answered then governmentsReason = "is_government_unlocked_unanswerable" end
    end

    local availablePolicies, policiesReason = CivSim_Government_AvailablePolicies(culture)
    local governors, availableGovernors, governorsReason = CivSim_Government_Governors(player)

    local state = {
        current_government = currentGovernment,
        available_governments = availableGovernments,
        available_policies = availablePolicies,
        governors = governors,
        available_governors = availableGovernors,
    }
    if governmentsReason ~= nil then state.available_governments_reason = governmentsReason end
    if policiesReason ~= nil then state.available_policies_reason = policiesReason end
    if governorsReason ~= nil then state.governors_reason = governorsReason end
    return state
end

CivSim_Government = {
    state = CivSim_Government_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Government.state()))
