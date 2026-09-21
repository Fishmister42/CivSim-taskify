-- lua/ingame/empire_orders.lua
-- Context: InGame (write/act). Verification reads back through lua/gamecore/research.lua and
-- lua/gamecore/government.lua rather than this file.
-- Backs declaration_ids: research.set_tech, research.set_civic, government.slot_policy,
-- government.change_government, government.assign_governor (catalogs/actions/research.yaml,
-- catalogs/actions/policies.yaml), capability_id: empire.orders.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- This file's own set-side accessors were not covered by the live-client sweep and remain
-- unconfirmed guesses; the per-call UNVERIFIED markers below are left as-is because the sweep did
-- not test them.
--
-- Parity note: choices are restricted to what CivSim_Research.state()/CivSim_Government.state()
-- already reported as researchable/available — exactly what the standard tech tree, civic tree,
-- and government screens would let a human pick right now.

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

-- MEASURED (2026-09-21, gameplay block 2, run-d2184c44): eight `research.set_tech` orders with a
-- valid target were dispatched through the previous body -- `Player:GetTechs():SetResearchingTech`,
-- a set-side name recalled by analogy and never confirmed -- and research.state read back no
-- current research after every one. The human's click in the Research chooser is not that call:
-- Firaxis's researchchooser.lua (lines 259-261) and techtree.lua (243-245) issue
-- `UI.RequestPlayerOperation(Game.GetLocalPlayer(), PlayerOperations.RESEARCH, {PARAM_TECH_TYPE =
-- <hash>, PARAM_INSERT_MODE = VALUE_EXCLUSIVE})`. That is the parity path, so it is the body now.
-- The request is asynchronous and answers nothing; the harness confirms it through
-- research.state's own re-read, never from the `ok` here (which only says the call did not error).
local function CivSim_EmpireOrders_SetResearch(techType)
    local row = GameInfo.Technologies[techType]
    if row == nil then
        return { ok = false, reason = "unknown_tech" }
    end
    local ok, err = pcall(function()
        local tParameters = {}
        tParameters[PlayerOperations.PARAM_TECH_TYPE] = row.Hash
        tParameters[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE
        UI.RequestPlayerOperation(Game.GetLocalPlayer(), PlayerOperations.RESEARCH, tParameters)
    end)
    if not ok then
        return { ok = false, reason = "UI.RequestPlayerOperation errored: " .. tostring(err), tech = techType }
    end
    return { ok = true, tech = techType, mechanism = "PlayerOperations.RESEARCH" }
end

-- The civic twin of the above: the Civics chooser's click is `PlayerOperations.PROGRESS_CIVIC`
-- with `PARAM_CIVIC_TYPE` (civicschooser.lua / civicstree.lua). UNVERIFIED LIVE as of this edit;
-- confirmed the same way, through research.state's `current_civic` re-read.
local function CivSim_EmpireOrders_SetCivic(civicType)
    local row = GameInfo.Civics[civicType]
    if row == nil then
        return { ok = false, reason = "unknown_civic" }
    end
    local ok, err = pcall(function()
        local tParameters = {}
        tParameters[PlayerOperations.PARAM_CIVIC_TYPE] = row.Hash
        tParameters[PlayerOperations.PARAM_INSERT_MODE] = PlayerOperations.VALUE_EXCLUSIVE
        UI.RequestPlayerOperation(Game.GetLocalPlayer(), PlayerOperations.PROGRESS_CIVIC, tParameters)
    end)
    if not ok then
        return { ok = false, reason = "UI.RequestPlayerOperation errored: " .. tostring(err), civic = civicType }
    end
    return { ok = true, civic = civicType, mechanism = "PlayerOperations.PROGRESS_CIVIC" }
end

-- UNVERIFIED: Player:GetCulture():SetPolicyActive(slotIndex, policyIndex) is the pattern recalled
-- from community modding references to policy slotting; exact signature not confirmed.
local function CivSim_EmpireOrders_SlotPolicy(slotIndex, policyType)
    local localPlayer = Game.GetLocalPlayer()
    local culture = Players[localPlayer]:GetCulture()
    local row = GameInfo.Policies[policyType]
    if row == nil then
        return { ok = false, reason = "unknown_policy" }
    end
    local ok, result = pcall(function() return culture:SetPolicyActive(slotIndex, row.Index) end) -- UNVERIFIED
    return { ok = (ok and result ~= false), slot = slotIndex, policy = policyType }
end

-- UNVERIFIED: government change is believed to go through a Culture-scoped method analogous to
-- SetPolicyActive; exact name not confirmed. This is only ever called when a government change is
-- currently legal (a human can only change government when one is newly unlocked or a cooldown
-- has elapsed), mirrored by this action's availability_predicate.
local function CivSim_EmpireOrders_ChangeGovernment(governmentType)
    local localPlayer = Game.GetLocalPlayer()
    local culture = Players[localPlayer]:GetCulture()
    local row = GameInfo.Governments[governmentType]
    if row == nil then
        return { ok = false, reason = "unknown_government" }
    end
    local ok, result = pcall(function() return culture:SetCurrentGovernment(row.Index) end) -- UNVERIFIED
    return { ok = (ok and result ~= false), government = governmentType }
end

-- UNVERIFIED: governor assignment accessor surface not confirmed; Player:GetGovernors() is
-- assumed by analogy with the read side in lua/gamecore/government.lua.
local function CivSim_EmpireOrders_AssignGovernor(governorType, cityId)
    local localPlayer = Game.GetLocalPlayer()
    local player = Players[localPlayer]
    local ok, governors = pcall(function() return player:GetGovernors() end) -- UNVERIFIED
    if not ok or governors == nil then
        return { ok = false, reason = "governors_unavailable" }
    end
    local ok2, result = pcall(function()
        return governors:AssignGovernor(governorType, cityId) -- UNVERIFIED
    end)
    return { ok = (ok2 and result ~= false), governor = governorType, city_id = cityId }
end

CivSim_EmpireOrders = {
    set_research = CivSim_EmpireOrders_SetResearch,
    set_civic = CivSim_EmpireOrders_SetCivic,
    slot_policy = CivSim_EmpireOrders_SlotPolicy,
    change_government = CivSim_EmpireOrders_ChangeGovernment,
    assign_governor = CivSim_EmpireOrders_AssignGovernor,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_EmpireOrders.set_research("TECH_POTTERY")))
