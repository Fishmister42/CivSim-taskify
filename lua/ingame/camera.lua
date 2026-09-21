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
local function CivSim_Camera_Move(x, y)
    local ok, result = pcall(function() return UI.LookAtPlot(x, y) end) -- UNVERIFIED
    return { ok = (ok and result ~= false), target_plot = { x = x, y = y } }
end

-- UNVERIFIED: no confirmed direct "set zoom to value" call; `UI.SetCameraZoom` is a placeholder
-- name, not a confirmed one.
local function CivSim_Camera_Zoom(zoomLevel)
    local ok, result = pcall(function() return UI.SetCameraZoom(zoomLevel) end) -- UNVERIFIED
    return { ok = (ok and result ~= false), zoom = zoomLevel }
end

-- UNVERIFIED: view-mode toggle (world <-> strategic view) is known in the base game as a hotkey
-- action; the exact `ActionTypes` key (`ToggleStrategicView` is a placeholder name) is not
-- confirmed. CORRECTED against a live client (spike P1) in one respect: the table this must be
-- read from is `ActionTypes`, not `ActionTypeIndex` — `ActionTypeIndex` was the same wrong table
-- name lua/ingame/turn_control.lua used to guess at for ACTION_ENDTURN; the confirmed table is
-- `ActionTypes` (observed holding `ACTION_ENDTURN = 751412917`, a Civ VI type hash, not a small
-- stable enum — read any member from this table at call time and never hard-code its value, the
-- same rule turn_control.lua follows). Whether `ActionTypes.ToggleStrategicView` (or whatever its
-- real key is) actually exists remains unconfirmed — only the table name is corrected here, not
-- the key.
local function CivSim_Camera_SetViewMode(mode)
    local ok, result = pcall(function()
        if mode == "strategic" then
            return UI.RequestAction(ActionTypes and ActionTypes["ToggleStrategicView"]) -- UNVERIFIED: key name
        else
            return UI.RequestAction(ActionTypes and ActionTypes["ToggleStrategicView"]) -- UNVERIFIED: same
            -- hotkey is assumed to toggle back; a client that requires two distinct action types
            -- would need this branch corrected against the live client.
        end
    end)
    return { ok = (ok and result ~= false), mode = mode }
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
        -- Absent on 1.0.12.9 (MEASURED T213); tried anyway for builds that do carry it, so this
        -- file needs no per-build fork.
        local okLegacy, lx, ly = pcall(function() return UI.GetCameraTargetPlot() end)
        if okLegacy and type(lx) == "number" and type(ly) == "number" then
            return math.floor(lx), math.floor(ly), nil
        end
        return nil, nil,
            "UI.GetMapLookAtWorldTarget is absent on this build and UI.GetCameraTargetPlot answered no plot"
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
    -- build; every read below was silently failing under its pcall, so `zoom` was nil and the
    -- provenance gate withheld every capture ("camera_state carries no numeric zoom"). What
    -- exists: `UI.GetMapZoom()` (0..1, 0.707 at the default view), `UI.GetWorldRenderView()`
    -- (0 at the world view; the strategic value is UNVERIFIED and assumed 1), `UI.LookAtPlot`,
    -- `UI.GetCursorPlotID`. The missing look-at getter is what T260 replaces, above.
    local okZoom, z = pcall(function() return UI.GetCameraZoom() end) -- absent on 1.0.12.9
    if not okZoom or type(z) ~= "number" then
        okZoom, z = pcall(function() return UI.GetMapZoom() end) -- MEASURED: 0.70710706710815
    end
    if okZoom and type(z) == "number" then zoom = z end
    local okMode, m = pcall(function() return UI.IsStrategicView() and "strategic" or "world" end) -- absent
    if not okMode then
        okMode, m = pcall(function()
            local view = UI.GetWorldRenderView() -- MEASURED: 0 at the world view
            return (view == 1) and "strategic" or "world" -- UNVERIFIED: the strategic value
        end)
    end
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
