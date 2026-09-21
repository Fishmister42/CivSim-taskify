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

-- Read back current camera state for the verification_predicate.
local function CivSim_Camera_ReadState()
    local x, y, zoom, mode = nil, nil, nil, "world"
    -- MEASURED (2026-09-21, Linux 1.0.12.9, live, T213): `UI.GetCameraTargetPlot`,
    -- `UI.GetMapLookAtPlot`, `UI.GetCameraZoom` and `UI.IsStrategicView` do NOT exist on this
    -- build; every read below was silently failing under its pcall, so `zoom` was nil and the
    -- provenance gate withheld every capture ("camera_state carries no numeric zoom"). What
    -- exists: `UI.GetMapZoom()` (0..1, 0.707 at the default view), `UI.GetWorldRenderView()`
    -- (0 at the world view; the strategic value is UNVERIFIED and assumed 1), `UI.LookAtPlot`,
    -- `UI.GetCursorPlotID`. There is no look-at getter, so the camera's target plot stays
    -- unknown and `target_is_revealed` stays false -- the fail-closed direction.
    local ok, cx, cy = pcall(function() return UI.GetCameraTargetPlot() end) -- absent on 1.0.12.9
    if ok then x, y = cx, cy end
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
    -- T221: pcall-guarded like every other read in this function. It was the one unguarded call
    -- here, and it became load-bearing once run/composition.py started reading this function every
    -- decision step to satisfy each view's declared camera_requirements -- a raised `Map` or
    -- `IsRevealed` error would have failed the whole command rather than degrading the capture.
    -- `target_is_revealed` stays false on any failure, which is the fail-closed direction FR-026
    -- requires (screening.py: "A camera state missing that confirmation is treated as *not*
    -- revealed"). Reveal is read for the LOCAL player only -- never another player's visibility.
    local revealed = false
    if x ~= nil and y ~= nil then
        local okReveal, isRevealed = pcall(function()
            local plot = Map.GetPlot(x, y)
            return (plot ~= nil and PlayersVisibility[Game.GetLocalPlayer()]:IsRevealed(plot:GetX(), plot:GetY())) -- MEASURED T213: Plot:IsRevealed does not exist; PlayersVisibility does
        end)
        revealed = (okReveal and isRevealed == true)
    end
    return {
        mode = mode,
        zoom = zoom,
        target_plot = (x ~= nil and { x = x, y = y } or nil),
        target_is_revealed = revealed,
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
