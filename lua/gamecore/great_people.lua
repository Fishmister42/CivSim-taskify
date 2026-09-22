-- lua/gamecore/great_people.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: great_people.state (catalogs/observations/great_people.yaml),
-- capability_id: great_people.read.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- CONFIRMED against a live client (spike P3, spikes/sweep-raw/GameCore_Tuner__P3_*.txt):
-- `Game.GetGreatPeople()` is a function in this file's own GameCore_Tuner context as well as in
-- InGame (it is one of the 4 of 8 manager accessors that GameCore_Tuner carries).
--
-- ACCESSOR AUDIT (2026-09-21, specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md):
-- every method this file called *on* that object was invented. `gpMgr:GetAvailableIndividuals()`
-- and `gpMgr:GetPlayerPoints()` appear in none of Firaxis' 645 shipped Lua files and are not
-- registered Lua bindings in any of the shipped engine binaries; both sat behind
-- `if gpMgr.<Method> then`, which swallowed the absence, so `recruitable_individuals` and
-- `points_by_class` were `[]` on every step ever recorded and `great_people.recruit` could never
-- become available. The field reads off their results (`individual.Index/.ClassType/.Name`) were
-- invented too. Replaced below with the Great People screen's own accessors, cited inline.
-- UNVERIFIED LIVE: read out of Firaxis' caller, not yet exercised against a running client.
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

local function CivSim_GreatPeople_Try(fn)
    local ok, value = pcall(fn)
    if ok then return value end
    return nil
end

-- --------------------------------------------------------------------------
-- The individuals the Great People screen is currently offering, one row per card.
--
-- SOURCE (this machine, 2026-09-21; steamassets/base/assets/ui/popups/greatpeoplepopup.lua):
--   :694,:704  local pGreatPeople = Game.GetGreatPeople(); pTimeline = pGreatPeople:GetTimeline()
--              -- one GLOBAL array of offers, not a per-player list; :708 iterates it with ipairs
--   :726,:752,:753,:712  the engine fields on each entry are `Individual`, `Class`, `Era` and
--              `Claimant` (nil while unclaimed); :723 `entry.Cost` is the point threshold the
--              panel renders as "<points>/<threshold>" at :426
--   :741,:746  the human-readable card is GameInfo.GreatPersonIndividuals[entry.Individual]
--   :728       pGreatPeople:CanRecruitPerson(displayPlayerID, entry.Individual) -- this, and only
--              this, is what makes the Recruit button visible (:265-269) or hidden (:276)
--
-- `is_recruitable` is per row because that is where the screen puts it: every current offer is on
-- the screen, and the button is live on some of them. `great_people.recruit`'s availability
-- predicate reads exactly this field off the row its `target` names.
-- --------------------------------------------------------------------------
local function CivSim_GreatPeople_Offered(localPlayer)
    local offered = {}
    local gpMgr = CivSim_GreatPeople_Try(function() return Game.GetGreatPeople() end)
    if gpMgr == nil then
        return offered, "game_great_people_unavailable"
    end
    local timeline = CivSim_GreatPeople_Try(function() return gpMgr:GetTimeline() end)
    if type(timeline) ~= "table" then
        -- Never a silent `[]`: the failure mode this file shipped with for its whole life.
        return offered, "get_timeline_unanswerable"
    end
    for _, entry in ipairs(timeline) do
        local individualIndex = entry.Individual
        local row = nil
        if individualIndex ~= nil then
            row = CivSim_GreatPeople_Try(function()
                return GameInfo.GreatPersonIndividuals[individualIndex]
            end)
        end
        if type(row) == "table" then
            local classRow = CivSim_GreatPeople_Try(function()
                return GameInfo.GreatPersonClasses[entry.Class]
            end)
            local label = CivSim_GreatPeople_Try(function() return Locale.Lookup(row.Name) end)
            local canRecruit = CivSim_GreatPeople_Try(function()
                return gpMgr:CanRecruitPerson(localPlayer, individualIndex)
            end)
            offered[#offered + 1] = {
                individual_id = individualIndex,
                class_type = (type(classRow) == "table" and classRow.GreatPersonClassType) or nil,
                name = (type(label) == "string" and label ~= "" and label) or row.Name,
                -- `entry.Cost` under a name the parity filter allows (`cost` is a banned token).
                points_required = entry.Cost,
                is_recruitable = (canRecruit == true),
            }
        end
    end
    return offered, nil
end

-- --------------------------------------------------------------------------
-- The local player's own great-person points, per class.
--
-- SOURCE (greatpeoplepopup.lua:780-781, :800-801): the class loop is
-- `for classInfo in GameInfo.GreatPersonClasses() do local classID = classInfo.Index`, and both
-- numbers come off a PLAYER object -- `player:GetGreatPeoplePoints():GetPointsTotal(classID)` and
-- `:GetPointsPerTurn(classID)` -- not off Game.GetGreatPeople(), which is where this file used to
-- ask. Corroborated at base/assets/ui/tutorialuiroot.lua:2095,2098. Both return floats; the panel
-- rounds them for display (:426, :441).
-- --------------------------------------------------------------------------
local function CivSim_GreatPeople_Points(player)
    local pointsByClass = {}
    local points = CivSim_GreatPeople_Try(function() return player:GetGreatPeoplePoints() end)
    if points == nil then
        return pointsByClass, "player_great_people_points_unavailable"
    end
    local answered = false
    for row in GameInfo.GreatPersonClasses() do
        local total = CivSim_GreatPeople_Try(function() return points:GetPointsTotal(row.Index) end)
        local perTurn = CivSim_GreatPeople_Try(function()
            return points:GetPointsPerTurn(row.Index)
        end)
        if type(total) == "number" then
            answered = true
            pointsByClass[#pointsByClass + 1] = {
                class_type = row.GreatPersonClassType,
                points = total,
                points_per_turn = (type(perTurn) == "number" and perTurn) or nil,
            }
        end
    end
    if not answered then
        return {}, "get_points_total_unanswerable"
    end
    return pointsByClass, nil
end

local function CivSim_GreatPeople_GetState()
    local localPlayer = Game.GetLocalPlayer()
    local player = Players[localPlayer]

    local offered, offeredReason = CivSim_GreatPeople_Offered(localPlayer)
    local pointsByClass, pointsReason = CivSim_GreatPeople_Points(player)

    local state = {
        recruitable_individuals = offered,
        points_by_class = pointsByClass,
    }
    if offeredReason ~= nil then state.recruitable_individuals_reason = offeredReason end
    if pointsReason ~= nil then state.points_by_class_reason = pointsReason end
    return state
end

CivSim_GreatPeople = {
    state = CivSim_GreatPeople_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_GreatPeople.state()))
