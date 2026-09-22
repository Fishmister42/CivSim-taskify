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

-- ACCESSOR AUDIT (2026-09-21): `Player:GetCulture():SetPolicyActive(slot, policyIndex)` is not
-- callable from Lua. The native method exists (`GameCore::Player::Culture::SetPolicyActive`,
-- steamassets/dlc/expansion2/binaries/win64/gamecore_xp2_finalrelease.map:17226) but it has no
-- `?l...@IPlayerCulture@Lua@Cache@GameCore@@` trampoline in either shipped linker map or either
-- GameCore binary -- it is engine-internal -- and no shipped Lua calls it. The name came from
-- community modding notes; every `policies.slot_policy` order raised inside its pcall and
-- reported `ok = false` forever.
--
-- SOURCE (this machine, 2026-09-21; steamassets/):
--   base/assets/ui/screens/governmentscreen.lua:1549-1575 -- OnConfirmPolicies_Yes builds TWO
--     tables and makes ONE call: `pPlayerCulture:RequestPolicyChanges(clearList, addList)` (:1570).
--     `clearList` (:1559,:1564) is an array of the ZERO-BASED slot indices to empty; `addList`
--     (:1560,:1566) is a sparse map from slot index to the POLICY HASH to place there.
--   base/assets/ui/screens/governmentscreen.lua:1555-1557 -- the removals have to be in the same
--     call as the additions, or the engine still believes a card sits in its old slot. That is why
--     this body re-sends the whole loadout rather than one slot.
--   base/assets/ui/screens/governmentscreen.lua:2284-2289 -- the current loadout is read back with
--     GetNumPolicySlots() and GetSlotPolicy(i), zero-based, -1 for an empty slot.
-- UNVERIFIED LIVE: read out of Firaxis' caller, not yet exercised against a running client.
local function CivSim_EmpireOrders_SlotPolicy(slotIndex, policyType)
    local localPlayer = Game.GetLocalPlayer()
    local row = GameInfo.Policies[policyType]
    if row == nil then
        return { ok = false, reason = "unknown_policy" }
    end
    local okCulture, culture = pcall(function() return Players[localPlayer]:GetCulture() end)
    if not okCulture or culture == nil then
        return { ok = false, reason = "player_culture_unavailable", policy = policyType }
    end
    local okSlots, slotCount = pcall(function() return culture:GetNumPolicySlots() end)
    if not okSlots or type(slotCount) ~= "number" then
        return { ok = false, reason = "get_num_policy_slots_unanswerable", policy = policyType }
    end
    if type(slotIndex) ~= "number" or slotIndex < 0 or slotIndex >= slotCount then
        return { ok = false, reason = "slot_out_of_range", slot = slotIndex, slot_count = slotCount }
    end
    local clearList, addList = {}, {}
    for i = 0, slotCount - 1 do
        local okSlot, occupant = pcall(function() return culture:GetSlotPolicy(i) end)
        local filled = okSlot and type(occupant) == "number" and occupant ~= -1
        if filled or i == slotIndex then
            clearList[#clearList + 1] = i
        end
        if filled and i ~= slotIndex then
            local okRow, existing = pcall(function() return GameInfo.Policies[occupant] end)
            if okRow and type(existing) == "table" and existing.Hash ~= nil then
                addList[i] = existing.Hash
            end
        end
    end
    addList[slotIndex] = row.Hash
    local ok, err = pcall(function() return culture:RequestPolicyChanges(clearList, addList) end)
    if not ok then
        return {
            ok = false,
            reason = "Culture:RequestPolicyChanges errored: " .. tostring(err),
            slot = slotIndex,
            policy = policyType,
        }
    end
    return {
        ok = true,
        slot = slotIndex,
        policy = policyType,
        mechanism = "Culture:RequestPolicyChanges",
    }
end

-- ACCESSOR AUDIT (2026-09-21): `culture:SetCurrentGovernment(index)` is real and IS callable from
-- Lua (`IPlayerCulture::lSetCurrentGovernment` is a registered trampoline), so the objection here
-- is parity, not existence. It is a GAMEPLAY-SCRIPT setter: its only appearance in the shipped
-- corpus is dlc/blackdeathscenario/scripts/blackdeathscenario.lua:187, a scenario forcing a
-- government on a player. It bypasses the anarchy/legality path the human's click goes through,
-- so using it here would be an action no human could issue (Principle I).
--
-- SOURCE (this machine, 2026-09-21; steamassets/):
--   base/assets/ui/screens/governmentscreen.lua:912 and :929 -- the Confirm button calls
--     `pPlayerCulture:RequestChangeGovernment(<government HASH>)`, which returns a boolean.
--     Note the asymmetry Firaxis ships: GetCurrentGovernment() RETURNS an index, this one TAKES a
--     hash (also IsGovernmentUnlocked at :2334).
--   base/assets/ui/screens/governmentscreen.lua:859-873 -- IsAbleToChangeGovernment() is the gate
--     that decides whether the button is offered at all; the harness's availability_predicate is
--     the stand-in for it, and this body does not re-litigate it.
-- UNVERIFIED LIVE.
local function CivSim_EmpireOrders_ChangeGovernment(governmentType)
    local localPlayer = Game.GetLocalPlayer()
    local row = GameInfo.Governments[governmentType]
    if row == nil then
        return { ok = false, reason = "unknown_government" }
    end
    local okCulture, culture = pcall(function() return Players[localPlayer]:GetCulture() end)
    if not okCulture or culture == nil then
        return { ok = false, reason = "player_culture_unavailable", government = governmentType }
    end
    local ok, accepted = pcall(function() return culture:RequestChangeGovernment(row.Hash) end)
    if not ok then
        return {
            ok = false,
            reason = "Culture:RequestChangeGovernment errored: " .. tostring(accepted),
            government = governmentType,
        }
    end
    return {
        ok = (accepted ~= false),
        government = governmentType,
        mechanism = "Culture:RequestChangeGovernment",
    }
end

-- ACCESSOR AUDIT (2026-09-21): `PlayerGovernors:AssignGovernor(type, cityId)` exists nowhere in
-- the shipped corpus, so this order could never have succeeded.
--
-- SOURCE (this machine, 2026-09-21; steamassets/):
--   dlc/expansion1/ui/additions/governorpanel.lua:556-564 -- the "assign to city" button issues
--     UI.RequestPlayerOperation(localPlayer, PlayerOperations.ASSIGN_GOVERNOR, {
--       [PARAM_GOVERNOR_TYPE] = <GameInfo.Governors row INDEX>, [PARAM_CITY_DEST] = cityID })
--     (:560, :561, :562). Identical in dlc/expansion2/ui/additions/governorpanel.lua:568-570.
--   dlc/expansion1/ui/additions/governorassignmentchooser.lua:377-380 -- the chooser's Confirm
--     adds PARAM_PLAYER_ONE for a city another player owns; a governor the harness assigns is
--     always going to one of the local player's own cities (the action's availability_predicate
--     requires `city.owner_is_local_player`), which is the short form above.
-- UNVERIFIED LIVE.
local function CivSim_EmpireOrders_AssignGovernor(governorType, cityId)
    local row = GameInfo.Governors[governorType]
    if row == nil then
        return { ok = false, reason = "unknown_governor", governor = governorType }
    end
    if type(cityId) ~= "number" then
        return { ok = false, reason = "unknown_city", governor = governorType, city_id = cityId }
    end
    local ok, err = pcall(function()
        local tParameters = {}
        tParameters[PlayerOperations.PARAM_GOVERNOR_TYPE] = row.Index
        tParameters[PlayerOperations.PARAM_CITY_DEST] = cityId
        UI.RequestPlayerOperation(
            Game.GetLocalPlayer(), PlayerOperations.ASSIGN_GOVERNOR, tParameters)
    end)
    if not ok then
        return {
            ok = false,
            reason = "UI.RequestPlayerOperation errored: " .. tostring(err),
            governor = governorType,
            city_id = cityId,
        }
    end
    return {
        ok = true,
        governor = governorType,
        city_id = cityId,
        mechanism = "PlayerOperations.ASSIGN_GOVERNOR",
    }
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
