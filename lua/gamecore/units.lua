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
    -- MEASURED (2026-09-21, live, T213): `UnitManager.GetReachablePlots` does not exist. The real
    -- call is `UnitManager.GetReachableMovement(unit)` -- InGame only (nil in GameCore_Tuner) --
    -- and it returns plot INDICES (a Settler at turn 1: 8 of them), resolved through
    -- `Map.GetPlotByIndex`. This is what the game's own move-preview highlighting shows.
    local reachable = {}
    local ok, result = pcall(function()
        return UnitManager.GetReachableMovement(unit)
    end)
    if ok and type(result) == "table" then
        for _, index in ipairs(result) do
            local okP, p = pcall(function() return Map.GetPlotByIndex(index) end)
            if okP and p ~= nil then
                reachable[#reachable + 1] = { x = p:GetX(), y = p:GetY() }
            end
        end
    end
    return reachable
end

local function CivSim_Units_Try(fn)
    local ok, value = pcall(fn)
    if ok then return value end
    return nil
end

-- --------------------------------------------------------------------------
-- The unit panel's build buttons, for the unit whose panel a human is looking at.
--
-- SOURCE (Firaxis' shipped UI on this machine, read 2026-09-21):
--   steamassets/base/assets/ui/panels/unitpanel.lua
--     :545-546  the operation rows are listed at all only while `pUnit:GetMovesRemaining() > 0`
--     :336-338  IsBuildingImprovement(actionHash) -> actionHash == UnitOperationTypes.BUILD_IMPROVEMENT
--     :346-351  GetBuildImprovementParameters -> {PARAM_X = pUnit:GetX(), PARAM_Y = pUnit:GetY()}
--     :554-561  UnitManager.CanStartOperation(pUnit, actionHash, nil, tParameters, true), then
--               tResults[UnitOperationResults.IMPROVEMENTS] -- the improvements this plot offers
--     :566-573  each improvement re-checked with tParameters[PARAM_IMPROVEMENT_TYPE] = eImprovement;
--               `local isDisabled = not bCanStart` is exactly what greys that row's button out
--     :563,:595-600,:919-933  BEST_IMPROVEMENT -> the row the panel frames as "Recommended"
--     :574      the row's human-visible label, Locale.Lookup(improvement.Name)
--   dlc/expansion2/ui/replacements/unitpanel_expansion2.lua:54-57,:107-118 -- Gathering Storm adds a
--   second build-button family, BUILD_IMPROVEMENT_ADJACENT, whose click opens an interface mode and
--   then waits for a map click (UI.SetInterfaceMode). That click is an act the harness has no
--   action for, so those rows are deliberately NOT listed here -- the same call cities.state makes
--   for a `requires_placement` production row: listing a button the agent could not finish pressing
--   would be a promise the catalog cannot keep.
--
-- Parity: only the SELECTED unit is asked. The build buttons exist only on the panel of the unit a
-- human has selected, and this is one CanStartOperation per offered improvement -- asking it of
-- every unit on the map would be both a read no human performs and a per-turn cost nobody pays.
-- UNVERIFIED LIVE: T213 measured `UnitManager.CanStartOperation` answering in InGame (which is why
-- units.state is declared there), but this five-argument, results-returning form and
-- `UnitOperationResults.IMPROVEMENTS` have not yet been exercised against a live client.
-- --------------------------------------------------------------------------
local function CivSim_GetAvailableBuilds(unit)
    local options = {}
    local moves = CivSim_Units_Try(function() return unit:GetMovesRemaining() end)
    if type(moves) ~= "number" or moves <= 0 then
        return options, nil -- unitpanel.lua:545-546: no moves left, no operation buttons at all
    end
    local parameters = CivSim_Units_Try(function()
        local p = {}
        p[UnitOperationTypes.PARAM_X] = unit:GetX()
        p[UnitOperationTypes.PARAM_Y] = unit:GetY()
        return p
    end)
    if type(parameters) ~= "table" then
        return options, "build_improvement_parameters_unavailable"
    end
    local ok, canStart, results = pcall(function()
        return UnitManager.CanStartOperation(
            unit, UnitOperationTypes.BUILD_IMPROVEMENT, nil, parameters, true)
    end)
    if not ok then
        -- Never a silent `[]` -- the failure mode that hid cities.state's empty
        -- `available_productions` for 399 steps. The list stays empty and says why.
        return options, "can_start_operation_unanswerable"
    end
    if canStart ~= true or type(results) ~= "table" then
        return options, nil -- the panel is showing no build buttons on this plot
    end
    local improvements = CivSim_Units_Try(function()
        return results[UnitOperationResults.IMPROVEMENTS]
    end)
    if type(improvements) ~= "table" then
        return options, nil
    end
    local best = CivSim_Units_Try(function() return results[UnitOperationResults.BEST_IMPROVEMENT] end)
    for _, eImprovement in ipairs(improvements) do
        parameters[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = eImprovement
        local okRow, rowCanStart = pcall(function()
            return UnitManager.CanStartOperation(
                unit, UnitOperationTypes.BUILD_IMPROVEMENT, nil, parameters, true)
        end)
        local row = CivSim_Units_Try(function() return GameInfo.Improvements[eImprovement] end)
        if type(row) == "table" and row.ImprovementType ~= nil then
            local label = CivSim_Units_Try(function() return Locale.Lookup(row.Name) end)
            options[#options + 1] = {
                improvement_type = row.ImprovementType,
                name = (type(label) == "string" and label ~= "" and label) or row.Name,
                disabled = not (okRow and rowCanStart == true),
                is_recommended = (best ~= nil and best ~= -1 and best == eImprovement),
            }
        end
    end
    return options, nil
end

-- --------------------------------------------------------------------------
-- The promotions the unit's own promotion tree is offering it.
--
-- MEASURED (2026-09-21): this list was `[]` for every unit on every record in the store, and the
-- promote goal (tests/live/goals/promote_unit.yaml) wrote that off as "a game-state fact, not a
-- body gap". It was a body gap. The body asked `unit:GetAvailablePromotions()`, a method that
-- appears nowhere in Firaxis' shipped UI, under a `if unit.GetAvailablePromotions then` guard that
-- swallowed its absence silently -- so `units.promote`'s availability predicate
-- (`target in unit.available_promotions`) could never hold, whatever the unit had earned.
--
-- SOURCE (this machine, 2026-09-21) -- what the game's own promotion list is:
--   steamassets/base/assets/ui/panels/unitpanel.lua:435-446 -- the Promote button is listed from
--     UnitManager.CanStartCommand(pUnit, UnitCommandTypes.PROMOTE, true, true) and
--     tResults[UnitCommandResults.PROMOTIONS]
--   steamassets/base/assets/ui/popups/unitpromotionpopup.lua:285-291 -- `item == row.Index` over
--     GameInfo.UnitPromotions(): the entries are promotion row Indices, resolved to the name a
--     human reads through GameInfo.UnitPromotions[...].UnitPromotionType
--
-- Asked per own unit, as `can_found_city` already is: a human sees the same list by selecting that
-- unit, and the promotion-available banner on a unit's flag is what sends them to look.
-- UNVERIFIED LIVE: read out of Firaxis' callers, not yet exercised against a running client.
-- --------------------------------------------------------------------------
local function CivSim_GetAvailablePromotions(unit)
    local promotions = {}
    local ok, canStart, results = pcall(function()
        return UnitManager.CanStartCommand(unit, UnitCommandTypes.PROMOTE, true, true)
    end)
    if not ok then
        return promotions, "can_start_command_unanswerable"
    end
    if canStart ~= true or type(results) ~= "table" then
        return promotions, nil -- the panel is offering this unit no promotion
    end
    local offered = CivSim_Units_Try(function() return results[UnitCommandResults.PROMOTIONS] end)
    if type(offered) ~= "table" then
        return promotions, nil
    end
    for _, ePromotion in ipairs(offered) do
        local row = CivSim_Units_Try(function() return GameInfo.UnitPromotions[ePromotion] end)
        if type(row) == "table" and row.UnitPromotionType ~= nil then
            promotions[#promotions + 1] = row.UnitPromotionType
        end
    end
    return promotions, nil
end

local function CivSim_DescribeUnit(unit, localPlayer)
    -- MEASURED (2026-09-21, live, T213): `unit:GetUnitType()` exists in InGame only (nil in
    -- GameCore_Tuner), `GetMovesRemaining`/`GetMaxMoves` answer in both ("2/2" for a Settler),
    -- `Unit:IsFortified` does not exist while `GetFortifyTurns` does. This read is therefore
    -- declared in the InGame context (catalogs/observations/units.yaml), where every accessor
    -- below has answered.
    local okType, unitType = pcall(function() return GameInfo.Units[unit:GetUnitType()].UnitType end)
    local okFort, fortifyTurns = pcall(function() return unit:GetFortifyTurns() end)
    -- MEASURED (2026-09-21, first model-driven runs): every unit action's availability predicate
    -- starts `unit.is_selected and ...`, and no observation produced that field, so every unit
    -- action was structurally unavailable_to_human_now -- 24 of 24 found-city decisions were
    -- refused before dispatch. The game's selection is `UI.GetHeadSelectedUnit()` (InGame; the
    -- unit whose action panel a human sees). Reported truthfully: the game selects the unit
    -- needing orders at turn start, and only that unit is "selected".
    local okSel, selected = pcall(function() return UI.GetHeadSelectedUnit() end)
    local isSelected = okSel and selected ~= nil and selected:GetID() == unit:GetID()
        and selected:GetOwner() == unit:GetOwner()
    local entry = {
        unit_id = unit:GetID(), -- Unit:GetID()
        unit_type = okType and unitType or nil,
        owner_player_id = unit:GetOwner(),
        owner_is_local_player = (unit:GetOwner() == localPlayer),
        is_selected = (isSelected == true),
        plot = { x = unit:GetX(), y = unit:GetY() },
        movement_remaining = unit:GetMovesRemaining(),
        max_movement = unit:GetMaxMoves(),
        is_fortified = (okFort and type(fortifyTurns) == "number" and fortifyTurns > 0) or false,
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
        local promotions, promotionsReason = CivSim_GetAvailablePromotions(unit)
        entry.available_promotions = promotions
        if promotionsReason ~= nil then
            entry.available_promotions_reason = promotionsReason
        end
        entry.charges_remaining = unit.GetBuildCharges and unit:GetBuildCharges() or nil -- UNVERIFIED
        -- The build buttons a human sees, and only on the panel they are looking at (see the
        -- source note above CivSim_GetAvailableBuilds). `build_options` is that list row for row,
        -- greyed ones included; `available_builds` is the subset whose button is live, which is
        -- what units.build_improvement's availability predicate reads
        -- (`target in unit.available_builds`), so it stays a list of plain type names.
        if entry.is_selected then
            local buildOptions, buildsReason = CivSim_GetAvailableBuilds(unit)
            entry.build_options = buildOptions
            local availableBuilds = {}
            for _, option in ipairs(buildOptions) do
                if not option.disabled then
                    availableBuilds[#availableBuilds + 1] = option.improvement_type
                end
            end
            entry.available_builds = availableBuilds
            if buildsReason ~= nil then
                entry.available_builds_reason = buildsReason
            end
        end
        -- ACCESSOR AUDIT (2026-09-21, spikes/lua-accessor-audit-2026-09-21.md): this block carried
        -- two phantoms and made one non-claim.
        --   * `unit:GetActivityType()` is not a unit method. The real accessor is
        --     `UnitManager.GetActivityType(pUnit)` -- a UnitManager function
        --     (base/assets/ui/panels/unitpanel.lua:2147, unitflagmanager.lua:856).
        --   * `UnitActivityType` is not a table in Civilization VI; the enum is `ActivityTypes`
        --     (unitpanel.lua:2148, unitflagmanager.lua:857-869).
        --   * `queued_path = { destination = nil }` promised a destination that no accessor in the
        --     shipped corpus can answer. `UnitManager.GetMoveToPathEx(unit, endPlotId)`
        --     (worldinput.lua:961) computes a *prospective* path to a plot the caller already
        --     names; it cannot say where a unit is already headed. Reporting a field whose only
        --     possible value is null is a claim the harness cannot keep, so the field is gone
        --     (Principle I non-claim) rather than emitted empty.
        -- What a human can see is kept: the unit's activity, which is what the flag badge shows.
        local activity = CivSim_Units_Try(function() return UnitManager.GetActivityType(unit) end)
        if type(activity) == "number" then
            entry.has_queued_orders = (activity == ActivityTypes.ACTIVITY_OPERATION)
        else
            entry.has_queued_orders_reason = "unit_manager_get_activity_type_unanswerable"
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
            for _, unit in playerUnits:Members() do -- Player:GetUnits():Members() iterator pattern (verified live)
                local isOwn = (player:GetID() == localPlayer)
                -- `Plot:IsVisible(playerID)` does not exist (measured); visibility is the
                -- player's: PlayersVisibility[playerID]:IsVisible(x, y) -- see lua/gamecore/map.lua.
                local visible = false
                pcall(function()
                    visible = PlayersVisibility[localPlayer]:IsVisible(unit:GetX(), unit:GetY()) == true
                end)
                if isOwn or visible then
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
