-- lua/ingame/camera.lua
-- Context: InGame (write/act). Verification reads back through this file's own read_state
-- function.
-- Backs declaration_ids: camera.move, camera.zoom, camera.set_view_mode
-- (catalogs/actions/camera.yaml), capability_id: camera.control.
--
-- Not separately assigned a task ID (T082-T089 name the domain files explicitly), but required
-- for T131's camera action declarations to resolve a real Lua implementation per research R8
-- ("Camera moves, zoom changes, and view-mode toggles are entries in catalogs/actions/camera.yaml,
-- executed through InGame Lua"). Added as necessary supporting infrastructure within this
-- assignment's owned lua/ directory.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library exists in
-- either. This file must stay entirely self-contained — no shared module can ever be factored out
-- and `require`d elsewhere — and carries its own hand-rolled JSON encoder.
--
-- Parity note (FR-026, R8): every move/zoom/toggle here is validated by the harness before this
-- file is invoked — target plot must already be revealed, zoom must be within the standard UI's
-- range, and the view mode must be one a human can toggle to. This file performs the camera change
-- and reads it back; it does not itself decide whether the request was legal.

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

-- UNVERIFIED: `UI.LookAtPlot(x, y)` is recalled as the standard camera-pan call used by Civ VI's
-- own UI Lua (e.g. jumping to a notification's location); exact signature not confirmed.
--
-- THIRD INSTANCE OF A KNOWN DEFECT (live, 2026-09-22): the dispatcher passes the decision's
-- `target` as the LAST positional argument, and for a camera move the target is the destination
-- plot `{x, y}` -- so a lone table argument is the plot, and the (x, y) two-scalar form is kept
-- for a caller that already has two numbers. Without this guard the table itself lands in `x`
-- with `y` nil (`UI.LookAtPlot(table, nil)`), which Firaxis' own accessor does not throw on, so
-- `ok` came back true and `camera.target_plot == target` was unsatisfiable -- the recorded cause
-- of `out_of_parity_camera`. Same normalisation as `lua/ingame/unit_orders.lua`'s
-- `CivSim_UnitOrders_MoveTo` (unit_orders.lua:107-109), and before it `CivSim_UnitOrders_Promote`
-- (unit_orders.lua:169-171): a lone table/string in the first parameter with the second nil is
-- unpacked, never passed through as-is.
--
-- Guard like Firaxis does (specs/002-civ-playing-harness/spikes/client-segfault-2026-09-21.md):
-- an engine call fed something it does not expect is the one failure `pcall` cannot always turn
-- into a reason, so the type is checked BEFORE `UI.LookAtPlot` is ever called. Anything that is
-- not, after normalisation, two numbers is a named refusal -- never a guess, and never a call.
local function CivSim_Camera_Move(x, y)
    if type(x) == "table" and y == nil then
        x, y = x.x, x.y
    end
    if type(x) ~= "number" or type(y) ~= "number" then
        return { ok = false, reason = "target_plot_invalid" }
    end
    local ok, result = pcall(function() return UI.LookAtPlot(x, y) end) -- UNVERIFIED
    return { ok = (ok and result ~= false), target_plot = { x = x, y = y } }
end

-- ACCESSOR AUDIT (2026-09-21, specs/002-civ-playing-harness/spikes/lua-accessor-audit-2026-09-21.md):
-- `UI.SetCameraZoom` was a placeholder name that never became real -- and no `UI.*` function whose
-- name contains "Camera" exists anywhere in Firaxis' 645 shipped Lua files. The camera API is
-- spelled "Map". Under its pcall this order reported `ok = false` every time, which is why
-- `camera.zoom` is one of the three camera actions the coverage report lists as never applied.
--
-- SOURCE (this machine, 2026-09-21; steamassets/base/assets/ui/worldinput.lua:1204): the zoom
-- hotkey issues `UI.SetMapZoom( oldZoom - ZOOM_SPEED, 0.0, 0.0 )` -- three arguments, and Firaxis
-- passes 0.0, 0.0 at every call site (the mouse-anchored variants are commented out at :355,
-- :358, :1461, :1464). The value is the same normalized scale `UI.GetMapZoom()` reads back
-- (base/assets/ui/worldview/cameramanager.lua:41), which this file's own read_state already uses
-- and which T213 MEASURED at 0.707 in the default view -- so the action and its
-- verification_predicate are finally on the same scale.
local function CivSim_Camera_Zoom(zoomLevel)
    if type(zoomLevel) ~= "number" then
        return { ok = false, reason = "zoom_not_a_number", zoom = zoomLevel }
    end
    local ok, err = pcall(function() UI.SetMapZoom(zoomLevel, 0.0, 0.0) end)
    if not ok then
        return {
            ok = false,
            reason = "UI.SetMapZoom errored: " .. tostring(err),
            zoom = zoomLevel,
        }
    end
    return { ok = true, zoom = zoomLevel, mechanism = "UI.SetMapZoom" }
end

-- ACCESSOR AUDIT (2026-09-21): `ActionTypes["ToggleStrategicView"]` was a placeholder key. It
-- reads nil, so this body called `UI.RequestAction(nil)` on both branches -- and both branches
-- were identical, so even a real toggle key could not have honoured a requested *mode*.
--
-- SOURCE (this machine, 2026-09-21; steamassets/base/assets/ui/minimappanel.lua:369-381,
-- `Toggle2DView`): the strategic-view switch is not an action hotkey at all. It reads
-- `UI.GetWorldRenderView()` and sets the other one:
--   UI.SetWorldRenderView( WorldRenderView.VIEW_3D )  -- the world view
--   UI.SetWorldRenderView( WorldRenderView.VIEW_2D )  -- the strategic view
-- Corroborated at base/assets/ui/worldview/citybannermanager.lua:1466 and
-- base/assets/ui/automation/automation_observercamera.lua:246,250. Setting the view directly is
-- also what makes this action idempotent: asking for "strategic" while already strategic leaves
-- it strategic, where a toggle would have flipped it away and failed its own verification.
local function CivSim_Camera_SetViewMode(mode)
    if mode ~= "world" and mode ~= "strategic" then
        return { ok = false, reason = "unknown_view_mode", mode = mode }
    end
    local ok, err = pcall(function()
        if mode == "strategic" then
            UI.SetWorldRenderView(WorldRenderView.VIEW_2D)
        else
            UI.SetWorldRenderView(WorldRenderView.VIEW_3D)
        end
    end)
    if not ok then
        return { ok = false, reason = "UI.SetWorldRenderView errored: " .. tostring(err), mode = mode }
    end
    return { ok = true, mode = mode, mechanism = "UI.SetWorldRenderView" }
end

-- T260 -- the camera's look-at plot, through Firaxis' own pair of accessors.
--
-- SOURCE (read out of the shipped UI Lua on this machine, 2026-09-21; steamassets/base/assets/ui/):
--   automation/automation_observercamera.lua:381-382
--       wx, wy = UI.GetMapLookAtWorldTarget();
--       x, y   = UI.GetPlotCoordFromWorld(wx, wy);
--   worldinput.lua:522 and :534 take the same world target for drag-focus;
--   minimappanel.lua:893 and fullscreenmappopup.lua:60 map a world point to a plot the same way,
--   fullscreenmappopup.lua:111-115 shows the off-map answer is -1, and
--   unitflagmanager.lua:1172 passes the optional world Z as a third argument.
--
-- UNVERIFIED LIVE: neither accessor has been probed on this build (1.0.12.9). T213 MEASURED that
-- `UI.GetCameraTargetPlot` and `UI.GetMapLookAtPlot` are absent here; this pair is Firaxis' own
-- way of answering the same question, taken from shipped code rather than from a live probe.
-- Every failure below returns a REASON naming the accessor and no plot at all -- the capture is
-- then withheld carrying that reason, which is the fail-closed direction FR-026 requires.
local function CivSim_Camera_LookAtPlot()
    local okProbe, hasWorldTarget = pcall(function() return UI.GetMapLookAtWorldTarget ~= nil end)
    if not okProbe then
        return nil, nil, "UI is not readable in this context"
    end
    if not hasWorldTarget then
        -- ACCESSOR AUDIT (2026-09-21): the fallback here used to try `UI.GetCameraTargetPlot()`,
        -- "for builds that do carry it". No build carries it: the name appears in none of
        -- Firaxis' 645 shipped Lua files and is not a registered UI binding in any shipped
        -- binary, so there is no build to fork for. T213 had already MEASURED it absent here.
        return nil, nil, "UI.GetMapLookAtWorldTarget is absent on this build"
    end
    local okWorld, wx, wy, wz = pcall(function() return UI.GetMapLookAtWorldTarget() end)
    if not okWorld then
        return nil, nil, "UI.GetMapLookAtWorldTarget errored: " .. tostring(wx)
    end
    if type(wx) ~= "number" or type(wy) ~= "number" then
        return nil, nil, "UI.GetMapLookAtWorldTarget returned no numeric world target"
    end
    local okPlot, px, py = pcall(function()
        -- unitflagmanager.lua:1172 passes a third (world Z) argument; the two call sites that
        -- start from a look-at target pass two. Pass whatever the world target actually gave.
        if type(wz) == "number" then
            return UI.GetPlotCoordFromWorld(wx, wy, wz)
        end
        return UI.GetPlotCoordFromWorld(wx, wy)
    end)
    if not okPlot then
        return nil, nil, "UI.GetPlotCoordFromWorld errored: " .. tostring(px)
    end
    if type(px) ~= "number" or type(py) ~= "number" then
        return nil, nil, "UI.GetPlotCoordFromWorld returned no numeric plot coordinate"
    end
    px, py = math.floor(px), math.floor(py)
    if px < 0 or py < 0 then
        -- fullscreenmappopup.lua:114-116 reads -1 back from this call as "off the map".
        return nil, nil, "the camera's world target maps to no plot (off-map)"
    end
    return px, py, nil
end

-- Is the camera's target plot one THIS player has revealed (Principle I: a human only sees
-- revealed terrain)?
--
-- MEASURED (T213, live, 1.0.12.9): `Plot:IsRevealed` does not exist on this build; the
-- `PlayersVisibility` table does, and `PlayersVisibility[pid]:IsRevealed(x, y)` is the known-good
-- accessor. Firaxis' own minimappanel.lua:891-898 (and fullscreenmappopup.lua:55-64) call the same
-- object with a plot INDEX -- `Map.GetPlotIndex(x, y)` -- so that overload is tried second, each
-- form under its own pcall so a raise in the first never hides the second. LOCAL player only;
-- another player's visibility is never read. Any failure returns false plus a reason, never a
-- guess.
local function CivSim_Camera_TargetIsRevealed(x, y)
    local okVis, vis = pcall(function() return PlayersVisibility[Game.GetLocalPlayer()] end)
    if not okVis or vis == nil then
        return false, "PlayersVisibility[Game.GetLocalPlayer()] is not readable on this build"
    end
    local okCoord, byCoord = pcall(function() return vis:IsRevealed(x, y) end)
    if okCoord and type(byCoord) == "boolean" then
        return byCoord, nil
    end
    local okIndex, byIndex = pcall(function() return vis:IsRevealed(Map.GetPlotIndex(x, y)) end)
    if okIndex and type(byIndex) == "boolean" then
        return byIndex, nil
    end
    return false,
        "PlayersVisibility[pid]:IsRevealed answered neither the (x, y) nor the plot-index form"
end

-- Read back current camera state for the verification_predicate.
local function CivSim_Camera_ReadState()
    local zoom, mode = nil, "world"
    -- MEASURED (2026-09-21, Linux 1.0.12.9, live, T213): `UI.GetCameraTargetPlot`,
    -- `UI.GetMapLookAtPlot`, `UI.GetCameraZoom` and `UI.IsStrategicView` do NOT exist on this
    -- build; every read here was silently failing under its pcall, so `zoom` was nil and the
    -- provenance gate withheld every capture ("camera_state carries no numeric zoom").
    -- ACCESSOR AUDIT (2026-09-21): those four are not build-specific absences -- none of them
    -- appears in any of Firaxis' 645 shipped Lua files or as a registered UI binding in any
    -- shipped binary. There is no build on which they answer, so the two first-tries are gone and
    -- the real accessors are the only reads.
    local okZoom, z = pcall(function() return UI.GetMapZoom() end) -- MEASURED: 0.70710706710815
    if okZoom and type(z) == "number" then zoom = z end
    -- minimappanel.lua:369 and citybannermanager.lua:1466 both test
    -- `UI.GetWorldRenderView() == WorldRenderView.VIEW_2D`; VIEW_2D *is* the strategic view
    -- (minimappanel.lua:369-381 `Toggle2DView`). Comparing against the named enum rather than the
    -- literal 1 also retires this file's old "the strategic value is UNVERIFIED and assumed 1".
    local okMode, m = pcall(function()
        return (UI.GetWorldRenderView() == WorldRenderView.VIEW_2D) and "strategic" or "world"
    end)
    if okMode and type(m) == "string" then mode = m end
    -- T221/T260: every read in this function is pcall-guarded, because run/composition.py reads
    -- this function every decision step to satisfy each view's declared camera_requirements -- a
    -- raised `UI`, `Map` or `IsRevealed` error would fail the whole command rather than degrade
    -- the capture. `target_is_revealed` stays false on any failure, which is the fail-closed
    -- direction FR-026 requires (screening.py: "A camera state missing that confirmation is
    -- treated as *not* revealed"), and `target_unavailable_reason` says WHY, naming the accessor,
    -- so a withheld capture's record blames something specific instead of a silent false.
    local x, y, reason = CivSim_Camera_LookAtPlot()
    local revealed = false
    if x ~= nil and y ~= nil then
        revealed, reason = CivSim_Camera_TargetIsRevealed(x, y)
    end
    return {
        mode = mode,
        zoom = zoom,
        target_plot = (x ~= nil and { x = x, y = y } or nil),
        target_is_revealed = revealed,
        target_unavailable_reason = reason,
    }
end

CivSim_Camera = {
    move = CivSim_Camera_Move,
    zoom = CivSim_Camera_Zoom,
    set_view_mode = CivSim_Camera_SetViewMode,
    read_state = CivSim_Camera_ReadState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Camera.move(24, 30)))
