-- lua/gamecore/units.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: units.state (catalogs/observations/units.yaml), capability_id: units.read.
-- Also supplies the `unit.*` predicate symbols (catalogs/README.md §4) for action availability
-- and verification: the harness re-invokes CivSim_Units.state() before evaluating a unit action's
-- predicates and again afterward to verify the effect.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- CORRECTED against a live client (spike P4): `Units.GetUnit` is confirmed `nil` — there is no
-- `Units` global with that shape. This file never used it directly (it already enumerated units
-- via `Players`), so nothing here needed changing for that specific finding; it is documented
-- because lua/ingame/unit_orders.lua's per-ID lookup did need correcting for the same reason (see
-- that file). `UnitOperationTypes.FOUND_CITY` below, and `UnitManager.RequestOperation`/
-- `CanStartOperation` used in this file, are all confirmed to exist (P4 spot-check).
--
-- Parity note: only the local human player's own units, plus enemy/other-civ units that are
-- currently visible on a revealed plot (exactly what the standard UI renders as a unit flag),
-- are reported. No hidden unit intent, no fog-of-war peeking, no other civilization's orders.

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

-- UNVERIFIED: UnitManager.GetUnitMovementRange / equivalent reachable-plot enumeration. The real
-- Civ VI Lua API is believed to expose reachable-plot queries through UnitManager (used by the
-- base game's own move-preview highlighting), but the exact function name and return shape are
-- not confirmed here. This helper is written against the most likely shape and must be corrected
-- against the live API before first use.
local function CivSim_GetReachablePlots(unit)
    local reachable = {}
    local ok, result = pcall(function()
        return UnitManager.GetReachablePlots(unit) -- UNVERIFIED: function name
    end)
    if ok and type(result) == "table" then
        for _, p in ipairs(result) do
            reachable[#reachable + 1] = { x = p:GetX(), y = p:GetY() }
        end
    end
    return reachable
end

local function CivSim_DescribeUnit(unit, localPlayer)
    local plot = Map.GetPlot(unit:GetX(), unit:GetY())
    local entry = {
        unit_id = unit:GetID(), -- Unit:GetID()
        unit_type = GameInfo.Units[unit:GetUnitType()].UnitType, -- UNVERIFIED: exact row/column name
        owner_player_id = unit:GetOwner(),
        owner_is_local_player = (unit:GetOwner() == localPlayer),
        plot = { x = unit:GetX(), y = unit:GetY() },
        movement_remaining = unit:GetMovesRemaining(), -- UNVERIFIED: exact accessor name
        max_movement = unit:GetMaxMoves(), -- UNVERIFIED
        is_fortified = unit.IsFortified and unit:IsFortified() or false, -- UNVERIFIED: guard for read-only visible units
    }
    if entry.owner_is_local_player then
        entry.reachable_plots = CivSim_GetReachablePlots(unit)
        -- VERIFIED (P4 spot-check): UnitManager.CanStartOperation and UnitOperationTypes.FOUND_CITY
        -- both confirmed to exist. CanStartOperation is confirmed as a function on the
        -- `UnitManager` table (like RequestOperation), not as a method on the unit object itself
        -- — the previous `unit:CanStartOperation(...)` call shape here was an untested guess and
        -- has been corrected to the UnitManager-table call convention `RequestOperation` also
        -- uses. UNVERIFIED: the exact argument order/count (assumed `(unit, opType)` by analogy
        -- with RequestOperation) is existence-only confirmed, not exercised.
        local okFound, canFound = pcall(function()
            return UnitManager.CanStartOperation(unit, UnitOperationTypes.FOUND_CITY)
        end)
        entry.can_found_city = (okFound and canFound == true)
        local promotions = {}
        if unit.GetAvailablePromotions then -- UNVERIFIED: no confirmed accessor for eligible promotions
            for _, promo in ipairs(unit:GetAvailablePromotions()) do
                promotions[#promotions + 1] = promo
            end
        end
        entry.available_promotions = promotions
        entry.charges_remaining = unit.GetBuildCharges and unit:GetBuildCharges() or nil -- UNVERIFIED
        -- Queued path, if any (mirrors the little destination marker the UI shows a unit with
        -- a pending multi-turn move order).
        if unit.GetActivityType and unit:GetActivityType() == UnitActivityType.ACTIVITY_OPERATION then -- UNVERIFIED
            entry.queued_path = { destination = nil } -- UNVERIFIED: no confirmed path-destination accessor
        end
    end
    return entry
end

-- VERIFIED (P4 spot-check): PlayerManager.GetAlive() exists. The previous `GetAliveMajors` name
-- here was an untested guess and is not confirmed to exist under that name; it has been replaced.
-- UNVERIFIED: whether GetAlive() returns every alive player (including city-states) or majors
-- only was not checked. Player:IsMajor() is applied defensively (only if present) so this file
-- keeps reporting majors only, matching its parity note, even if GetAlive() turns out to include
-- minors; if IsMajor is unavailable every returned player is kept, preserving prior behavior.
local function CivSim_Units_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local units = {}
    for i, player in ipairs(PlayerManager.GetAlive()) do
        if not player.IsMajor or player:IsMajor() then -- UNVERIFIED: Player:IsMajor()
            local playerUnits = player:GetUnits()
            for _, unit in playerUnits:Members() do -- Player:GetUnits():Members() iterator pattern
                local isOwn = (player:GetID() == localPlayer)
                local plot = Map.GetPlot(unit:GetX(), unit:GetY())
                if isOwn or (plot ~= nil and plot:IsVisible(localPlayer)) then
                    units[#units + 1] = CivSim_DescribeUnit(unit, localPlayer)
                end
            end
        end
    end
    return { units = units }
end

CivSim_Units = {
    state = CivSim_Units_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Units.state()))
