-- lua/gamecore/espionage.lua
-- Context: InGame (read-only). Moved out of GameCore_Tuner on 2026-09-21 -- see below.
-- Backs declaration_id: espionage.state (catalogs/observations/espionage.yaml),
-- capability_id: espionage.read.
--
-- ACCESSOR AUDIT (2026-09-21, specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md):
-- every accessor this file used was invented. `player:GetEspionage()`, `:GetSpies()`,
-- `spy:GetUnitID()`, `:GetCurrentMission()`, `:GetTargetCityID()` and `:IsAvailable()` appear in
-- none of Firaxis' 645 shipped Lua files and are not registered engine bindings. There is no
-- espionage object in Civilization VI at all. Behind `if espionageMgr.GetSpies then` the whole
-- loop was skipped, so `spies` was `[]` on every step and `espionage.assign_mission` (whose
-- predicate is `spy.exists and spy.is_available`) could never become available.
--
-- CONTEXT: a spy is a unit, and the read below needs `unit:GetUnitType()` (MEASURED InGame-only,
-- T213) and `Cities.GetPlotPurchaseCity`, so this declaration moves to InGame with units.state.
-- UNVERIFIED LIVE: read out of Firaxis' callers, not yet exercised against a running client.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- Parity note: only the local player's own spies, their current missions, and *outcomes* of
-- resolved missions the human would see in the notification/reports log are reported. Never an
-- opponent's spy locations, counter-espionage posture, or in-progress opposing missions — those
-- are exactly the kind of hidden-opponent-state FR-019 forbids.

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

local function CivSim_Espionage_Try(fn)
    local ok, value = pcall(fn)
    if ok then return value end
    return nil
end

-- --------------------------------------------------------------------------
-- The local player's own spies, exactly as the Espionage panel finds them.
--
-- SOURCE (this machine, 2026-09-21; steamassets/base/assets/ui/):
--   partialscreens/espionageoverview.lua:88-101 -- there is no spy list to ask for. The panel
--     iterates `Players[localPlayerID]:GetUnits():Members()` and keeps a unit when
--     `GameInfo.Units[unit:GetUnitType()].Spy` is true; it then splits idle from active on
--     `unit:GetSpyOperation() == -1`.
--   partialscreens/espionageoverview.lua:659,:673 -- the mission is
--     `unit:GetSpyOperation()` resolved through `GameInfo.UnitOperations[...]`; :677 the remaining
--     turns are `unit:GetSpyOperationEndTurn() - Game.GetCurrentGameTurn()`.
--   partialscreens/espionageoverview.lua:643-644 -- the target city is derived from the spy's own
--     plot: `Cities.GetPlotPurchaseCity(Map.GetPlot(unit:GetX(), unit:GetY()))`. There is no
--     GetTargetCity/GetTargetCityID anywhere in the shipped corpus.
--   choosers/espionagechooser.lua:741-755 -- a spy is ready for new orders when it
--     `IsReadyToMove()` and `UnitManager.GetActivityType(unit) == ActivityTypes.ACTIVITY_AWAKE`;
--     that pair is what decides whether the chooser opens at all.
--
-- The three spy-unit methods that exist in the whole corpus are `GetSpyOperation`,
-- `GetSpyOperationEndTurn` and `GetPursuingSpyName`. Nothing else.
--
-- Parity: only the local player's own units are inspected, exactly what the Espionage panel lists.
-- Captured/off-map spies (playerDiplomacy:GetNumSpiesCaptured/OffMap) are a separate panel section
-- and are not claimed here.
-- --------------------------------------------------------------------------
local function CivSim_Espionage_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local spies = {}

    local units = CivSim_Espionage_Try(function() return Players[localPlayer]:GetUnits() end)
    if units == nil then
        return { spies = spies, spies_reason = "player_units_unavailable" }
    end
    local okIterate = pcall(function()
        for _, unit in units:Members() do
            local unitInfo = CivSim_Espionage_Try(function()
                return GameInfo.Units[unit:GetUnitType()]
            end)
            if type(unitInfo) == "table" and unitInfo.Spy == true then
                local operation = CivSim_Espionage_Try(function() return unit:GetSpyOperation() end)
                local mission = nil
                if type(operation) == "number" and operation ~= -1 then
                    local row = CivSim_Espionage_Try(function()
                        return GameInfo.UnitOperations[operation]
                    end)
                    if type(row) == "table" then mission = row.OperationType end
                end
                local targetCityId = nil
                local city = CivSim_Espionage_Try(function()
                    return Cities.GetPlotPurchaseCity(Map.GetPlot(unit:GetX(), unit:GetY()))
                end)
                if city ~= nil then
                    local cityId = CivSim_Espionage_Try(function() return city:GetID() end)
                    if type(cityId) == "number" then targetCityId = cityId end
                end
                local activity = CivSim_Espionage_Try(function()
                    return UnitManager.GetActivityType(unit)
                end)
                local readyToMove = CivSim_Espionage_Try(function() return unit:IsReadyToMove() end)
                spies[#spies + 1] = {
                    unit_id = unit:GetID(),
                    mission = mission,
                    target_city_id = targetCityId,
                    -- espionageoverview.lua:92-97 (idle) plus espionagechooser.lua:741-748 (the
                    -- chooser will actually open). Both, so `is_available` means the panel would
                    -- take an order, not merely that no mission is running.
                    is_available = (
                        operation == -1
                        and readyToMove == true
                        and activity == ActivityTypes.ACTIVITY_AWAKE
                    ),
                }
            end
        end
    end)
    if not okIterate then
        return { spies = {}, spies_reason = "player_units_members_unanswerable" }
    end
    return { spies = spies }
end

CivSim_Espionage = {
    state = CivSim_Espionage_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Espionage.state()))
