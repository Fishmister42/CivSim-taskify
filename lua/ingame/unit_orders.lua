-- lua/ingame/unit_orders.lua
-- Context: InGame (write/act). Orders only; verification reads back through
-- lua/gamecore/units.lua's CivSim_Units.state() rather than this file.
-- Backs declaration_ids: units.move_to, units.found_city, units.promote, units.build_improvement
-- (catalogs/actions/units.yaml), capability_id: units.orders.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- CORRECTED against a live client (spike P4): `Units.GetUnit` is confirmed `nil` — there is no
-- `Units` global with that shape. This file's own per-ID lookup used a different guess
-- (`UnitManager.GetUnit(playerID, unitID)`), also never confirmed to exist under that name and
-- absent from the spike's confirmed list; it has been replaced below with a lookup built entirely
-- from confirmed primitives (`Players`, `Player:GetUnits()`, `Units:Members()`, `Unit:GetID()` —
-- P4 spot-check, and the `Player:GetUnits():Members()` idiom already used in
-- lua/gamecore/units.lua), per the correction's guidance that unit access goes through `Players`.
--
-- Parity note: every function here issues exactly the order a human could issue by selecting the
-- unit and clicking the corresponding UI command; none accepts a target the availability
-- predicate would not already have accepted (movement outside declared reachable_plots, an
-- unavailable promotion, founding where founding is illegal).

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

-- VERIFIED (P4 spot-check): finds one of the local player's own units by id, built from confirmed
-- primitives (Players, Player:GetUnits(), Units:Members(), Unit:GetID()) rather than the
-- unconfirmed single-shot `UnitManager.GetUnit(playerID, unitID)` this file used to guess at (see
-- header). A unit belonging to another player is deliberately not found here — every action in
-- this file only ever targets the local player's own units, matching this catalog's parity rule.
local function CivSim_FindLocalUnit(unitId)
    local localPlayer = Game.GetLocalPlayer()
    -- MEASURED (2026-09-21, first model-driven run, Linux 1.0.12.9): the model chose
    -- units.found_city eight times in a row with an empty parameter set -- no action declaration
    -- carries a parameter schema, so it was never told a unit id was wanted -- and every call
    -- reported `unit_not_found`. A human's "Found City" acts on the unit they have SELECTED, and
    -- `UI.GetHeadSelectedUnit()` answers in InGame (verified: a table for the selected Settler).
    -- So a nil id means "the selected unit", which is exactly the parity basis of every action in
    -- this file; a wrong id is still not found, and nothing here ever picks a unit on its own.
    if unitId == nil then
        local ok, selected = pcall(function() return UI.GetHeadSelectedUnit() end)
        if ok and selected ~= nil and selected:GetOwner() == localPlayer then
            return selected
        end
        return nil
    end
    local units = Players[localPlayer]:GetUnits()
    for _, u in units:Members() do
        if u:GetID() == unitId then
            return u
        end
    end
    return nil
end

-- Move the given unit to (x, y). VERIFIED (P4 spot-check): UnitManager.RequestOperation,
-- UnitOperationTypes.MOVE_TO, and UnitOperationTypes.PARAM_X/PARAM_Y are all confirmed to exist.
-- UNVERIFIED: existence is not arity — that this exact parameter-table shape
-- (`tParameters[PARAM_X] = x`) is how RequestOperation consumes them was not independently
-- exercised by the spike (no function beyond UI.RequestAction/UI.CanEndTurn/GetCurrentGameTurn/
-- ContextPtr:IsHidden/the GameConfiguration getters was actually called).
local function CivSim_UnitOrders_MoveTo(unitId, x, y)
    -- The dispatcher passes the decision's `target` as the LAST positional argument, and for
    -- a move the target is the destination plot `{x, y}` (the unit is the selected one --
    -- catalogs/README.md §4). So a lone table argument is the plot, and the unit is nil ->
    -- selected. The (unitId, x, y) form is kept for a caller that names the unit explicitly.
    if type(unitId) == "table" and x == nil then
        x, y, unitId = unitId.x, unitId.y, nil
    end
    local unit = CivSim_FindLocalUnit(unitId)
    if unit == nil then
        return { ok = false, reason = "unit_not_found" }
    end
    local tParameters = {}
    tParameters[UnitOperationTypes.PARAM_X] = x
    tParameters[UnitOperationTypes.PARAM_Y] = y
    local accepted = UnitManager.RequestOperation(unit, UnitOperationTypes.MOVE_TO, tParameters)
    return { ok = (accepted ~= false), unit_id = unitId, requested_plot = { x = x, y = y } }
end

-- Found a city with the given settler unit at its current plot. VERIFIED (P4 spot-check):
-- UnitManager.RequestOperation and UnitOperationTypes.FOUND_CITY both confirmed to exist
-- (existence only — see the MoveTo comment above on arity).
local function CivSim_UnitOrders_FoundCity(unitId)
    local unit = CivSim_FindLocalUnit(unitId)
    if unit == nil then
        return { ok = false, reason = "unit_not_found", unit_id = unitId }
    end
    -- The plot is checked the way the game itself checks it before the button lights up
    -- (verified live: UnitManager.CanStartOperation(unit, FOUND_CITY) -> true for a Settler).
    local okCan, can = pcall(function()
        return UnitManager.CanStartOperation(unit, UnitOperationTypes.FOUND_CITY)
    end)
    if okCan and can == false then
        return { ok = false, reason = "cannot_found_here", unit_id = unit:GetID() }
    end
    local accepted = UnitManager.RequestOperation(unit, UnitOperationTypes.FOUND_CITY, {})
    return { ok = (accepted ~= false), unit_id = unit:GetID() }
end

-- Apply a promotion the unit has earned -- clicking a slot in its promotion tree.
--
-- MEASURED (2026-09-21, reading the dispatch path rather than the game): this function could never
-- work. `act/executor.py`'s `_build_arguments` passes a decision's `target` as the LAST positional
-- argument and units.promote's target is the promotion *name*, so every call arrived as
-- `promote("PROMOTION_BATTLECRY")` -- the name landed in `unitId`, `CivSim_FindLocalUnit` looped
-- over the player's units comparing ids to a string, found nothing, and answered `unit_not_found`
-- for a unit that was selected and standing right there. `move_to` already carried the
-- normalisation guard for exactly this (a lone table is the plot); it is applied here too.
--
-- The body was also a guess: `unit:SetPromotion(...)` appears nowhere in Firaxis' shipped UI.
-- SOURCE (this machine, 2026-09-21) -- what the game's own promote path actually is:
--   steamassets/base/assets/ui/panels/unitpanel.lua
--     :435-446    the Promote button is listed from
--                 UnitManager.CanStartCommand(pUnit, UnitCommandTypes.PROMOTE, true, true) and
--                 tResults[UnitCommandResults.PROMOTIONS] -- the promotions this unit is offered
--     :2708-2717  OnPromoteUnit, the click itself:
--                   tParameters[UnitCommandTypes.PARAM_PROMOTION_TYPE] = ePromotion
--                   UnitManager.RequestCommand(pSelectedUnit, UnitCommandTypes.PROMOTE, tParameters)
--   steamassets/base/assets/ui/popups/unitpromotionpopup.lua
--     :285-291    `if (item == row.Index)` over GameInfo.UnitPromotions() -- so the value carried
--                 by the clicked slot, and therefore PARAM_PROMOTION_TYPE, is the promotion row's
--                 own Index, not its hash and not its name
--     :61-72      the popup's own click, the same two lines as the panel's
-- Both of those are reproduced below. UNVERIFIED LIVE: read out of Firaxis' callers, not yet
-- exercised against a running client.
local function CivSim_UnitOrders_Promote(unitId, promotionType)
    -- A lone string argument is the promotion; the unit is the selected one (see the note above).
    if type(unitId) == "string" and promotionType == nil then
        unitId, promotionType = nil, unitId
    end
    local unit = CivSim_FindLocalUnit(unitId)
    if unit == nil then
        return { ok = false, reason = "unit_not_found" }
    end
    if type(promotionType) ~= "string" then
        return { ok = false, reason = "no_promotion_named", unit_id = unit:GetID() }
    end
    local okRow, row = pcall(function() return GameInfo.UnitPromotions[promotionType] end)
    if not okRow or type(row) ~= "table" or row.Index == nil then
        return { ok = false, reason = "unknown_promotion", unit_id = unit:GetID(),
                 promotion = promotionType }
    end
    -- The panel's own gate (unitpanel.lua:437-439): a slot is clickable only for a promotion in
    -- this unit's offered list. A command that cannot be asked at all is not treated as a refusal.
    local okCan, canStart, results = pcall(function()
        return UnitManager.CanStartCommand(unit, UnitCommandTypes.PROMOTE, true, true)
    end)
    if okCan and canStart == false then
        return { ok = false, reason = "promotion_not_available", unit_id = unit:GetID(),
                 promotion = promotionType }
    end
    if okCan and type(results) == "table" then
        local offered = nil
        pcall(function() offered = results[UnitCommandResults.PROMOTIONS] end)
        if type(offered) == "table" then
            local isOffered = false
            for _, ePromotion in ipairs(offered) do
                if ePromotion == row.Index then isOffered = true end
            end
            if not isOffered then
                return { ok = false, reason = "promotion_not_available", unit_id = unit:GetID(),
                         promotion = promotionType }
            end
        end
    end
    local tParameters = {}
    tParameters[UnitCommandTypes.PARAM_PROMOTION_TYPE] = row.Index
    local accepted = UnitManager.RequestCommand(unit, UnitCommandTypes.PROMOTE, tParameters)
    return { ok = (accepted ~= false), unit_id = unit:GetID(), promotion = promotionType }
end

-- Spend a build charge: put an improvement on the plot the selected unit is standing on. This is
-- the unit panel's build button, and nothing more.
--
-- SOURCE (Firaxis' shipped UI on this machine, read 2026-09-21):
--   steamassets/base/assets/ui/panels/unitpanel.lua
--     :554-573    the panel lists one button per improvement the plot offers, each greyed exactly
--                 when UnitManager.CanStartOperation(pUnit, BUILD_IMPROVEMENT, nil, tParameters,
--                 true) is false with tParameters[PARAM_IMPROVEMENT_TYPE] set
--     :604,:915   the button carries that improvement's own `Hash` as its click payload
--                 (AddActionToTable(..., improvement.Hash) -> UnitActionButton:SetVoid1)
--     :2548-2557  OnUnitActionClicked_BuildImprovement, the click itself:
--                   tParameters[PARAM_X] = pSelectedUnit:GetX()
--                   tParameters[PARAM_Y] = pSelectedUnit:GetY()
--                   tParameters[PARAM_IMPROVEMENT_TYPE] = improvementHash
--                   UnitManager.RequestOperation(pSelectedUnit, BUILD_IMPROVEMENT, tParameters)
-- Those four lines are reproduced verbatim below; the improvement is the one the caller named, on
-- the unit's own plot, and no other plot is reachable through this function at all.
--
-- The panel's own gate is re-asked here before the order goes out (`cannot_build_here` is a greyed
-- button, not a refusal invented by the harness), matching CivSim_UnitOrders_FoundCity above.
-- UNVERIFIED LIVE: UnitOperationTypes.BUILD_IMPROVEMENT / PARAM_IMPROVEMENT_TYPE and this
-- parameter-table shape are read out of Firaxis' own caller, not yet exercised against a client.
local function CivSim_UnitOrders_BuildImprovement(unitId, improvementType)
    -- The dispatcher passes the decision's `target` as the LAST positional argument, and for a
    -- build the target is the improvement type name; a lone string argument is therefore the
    -- improvement and the unit is the selected one (same normalisation as MoveTo above).
    if type(unitId) == "string" and improvementType == nil then
        unitId, improvementType = nil, unitId
    end
    local unit = CivSim_FindLocalUnit(unitId)
    if unit == nil then
        return { ok = false, reason = "unit_not_found" }
    end
    if type(improvementType) ~= "string" then
        return { ok = false, reason = "no_improvement_named", unit_id = unit:GetID() }
    end
    local okRow, row = pcall(function() return GameInfo.Improvements[improvementType] end)
    if not okRow or type(row) ~= "table" or row.Hash == nil then
        return { ok = false, reason = "unknown_improvement", unit_id = unit:GetID(),
                 improvement = improvementType }
    end
    local tParameters = {}
    tParameters[UnitOperationTypes.PARAM_X] = unit:GetX()
    tParameters[UnitOperationTypes.PARAM_Y] = unit:GetY()
    tParameters[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = row.Hash
    local okCan, can = pcall(function()
        return UnitManager.CanStartOperation(
            unit, UnitOperationTypes.BUILD_IMPROVEMENT, nil, tParameters, true)
    end)
    if okCan and can == false then
        return { ok = false, reason = "cannot_build_here", unit_id = unit:GetID(),
                 improvement = improvementType }
    end
    local accepted = UnitManager.RequestOperation(
        unit, UnitOperationTypes.BUILD_IMPROVEMENT, tParameters)
    -- Bounded read-back: the two things a human watches change -- the charge counter on the unit's
    -- own panel, and the improvement that appears on the tile under it. Two single reads, no loop
    -- and no waiting. Evidence only: whether this counts as `applied` is decided upstream by the
    -- declaration's verification_predicate against a freshly re-assembled observation, never by
    -- what this function returns (src/civsim_harness/act/executor.py's own module docstring).
    local charges = nil
    pcall(function() if unit.GetBuildCharges then charges = unit:GetBuildCharges() end end)
    local plotImprovement = nil
    pcall(function()
        local plot = Map.GetPlot(unit:GetX(), unit:GetY())
        if plot ~= nil and plot:GetImprovementType() ~= -1 then
            plotImprovement = GameInfo.Improvements[plot:GetImprovementType()].ImprovementType
        end
    end)
    return {
        ok = (accepted ~= false),
        unit_id = unit:GetID(),
        improvement = improvementType,
        plot = { x = unit:GetX(), y = unit:GetY() },
        charges_remaining = charges,
        plot_improvement = plotImprovement,
    }
end

CivSim_UnitOrders = {
    move_to = CivSim_UnitOrders_MoveTo,
    found_city = CivSim_UnitOrders_FoundCity,
    promote = CivSim_UnitOrders_Promote,
    build_improvement = CivSim_UnitOrders_BuildImprovement,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_UnitOrders.move_to(42, 10, 12)))
