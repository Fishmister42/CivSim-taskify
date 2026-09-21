-- lua/gamecore/map.lua
-- Context: GameCore_Tuner (read-only).
-- Backs declaration_id: map.state (catalogs/observations/map.yaml), capability_id: map.read.
--
-- SANDBOX CONSTRAINT (specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md, P5):
-- neither tuner context exposes `require`, `io`, or `debug`, and no JSON library
-- (`json`/`JSON`/`cjson`/`dkjson`/`Serialize`) exists in either — confirmed, not merely assumed;
-- see the encoder note just below. This file must stay entirely self-contained — no shared module
-- can ever be factored out and `require`d elsewhere.
--
-- VERIFIED (P4 spot-check): Map.GetGridSize, Map.GetPlot, Map.GetPlotByIndex, and
-- Map.GetPlotDistance all confirmed to exist as functions.
--
-- Parity note: every field here is something a human player can already see on the minimap,
-- the main map view, or by hovering a revealed plot in the standard UI. This file must never
-- walk plots the local player has not revealed (Plot:IsRevealed / Plot:IsVisible gate every
-- read) and must never report another civilization's units, improvements, or yields on plots
-- outside the local player's revealed set.

-- Minimal JSON encoder. VERIFIED (P5): no JSON library (`json`/`JSON`/`cjson`/`dkjson`/
-- `Serialize`) exists in either tuner context, confirming this hand-rolled encoder is the
-- correct approach, not merely a defensive assumption.
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

-- Returns the revealed-map snapshot: dimensions, and per-revealed-plot terrain/feature/resource/
-- ownership/improvement, matching what the standard map view shows for a revealed (not
-- necessarily currently visible-in-fog-of-war-cleared-sense) plot.
local function CivSim_Map_GetState()
    -- VERIFIED: Game.GetLocalPlayer() was itself exercised successfully in the spike's bonus
    -- GameConfiguration section (PlayerConfigurations[Game.GetLocalPlayer()] resolved
    -- LocalPlayerID=0), not merely assumed.
    local localPlayer = Game.GetLocalPlayer()
    local width, height = Map.GetGridSize()   -- VERIFIED (P4): Map.GetGridSize() confirmed to exist.
    -- MEASURED (2026-09-21, Linux 1.0.12.9, live, T213): `Plot:IsRevealed(playerID)` and
    -- `Plot:IsVisible(playerID)` do NOT exist ("function expected instead of nil") -- the unguarded
    -- call errored on the first plot and the whole read timed out in every real run. Visibility
    -- is the player's, not the plot's: `PlayersVisibility[playerID]:IsRevealed(x, y)` /
    -- `:IsVisible(x, y)` answer in both Lua contexts (22 revealed plots at turn 1, Small map 74x46).
    local visibility = PlayersVisibility[localPlayer]
    local playerResources = nil
    pcall(function() playerResources = Players[localPlayer]:GetResources() end)

    local plots = {}
    for y = 0, height - 1 do
        for x = 0, width - 1 do
            local plot = Map.GetPlot(x, y) -- VERIFIED (P4): Map.GetPlot() confirmed to exist.
            if plot ~= nil and visibility:IsRevealed(x, y) then
                local entry = {
                    x = x,
                    y = y,
                    terrain = GameInfo.Terrains[plot:GetTerrainType()].TerrainType, -- VERIFIED live (TERRAIN_OCEAN)
                    is_currently_visible = visibility:IsVisible(x, y),
                    owner_player_id = plot:GetOwner(), -- -1 when unowned (verified live); standard UI shows borders for this
                }
                if plot:GetFeatureType() ~= -1 then
                    entry.feature = GameInfo.Features[plot:GetFeatureType()].FeatureType -- UNVERIFIED
                end
                local resourceType = plot:GetResourceType()
                if resourceType ~= -1 then
                    -- `Plot:IsResourceVisible` does not exist (measured). Some resources need a
                    -- tech before the human map shows them, so a resource is reported ONLY when
                    -- the player's own resource table confirms it visible -- UNVERIFIED accessor,
                    -- under pcall: if it is absent, no resource is reported at all. Withholding is
                    -- the Principle I-safe failure; over-revealing is not.
                    local okV, visible = pcall(function()
                        return playerResources:IsResourceVisible(GameInfo.Resources[resourceType].Hash)
                    end)
                    if okV and visible == true then
                        entry.resource = GameInfo.Resources[resourceType].ResourceType
                    end
                end
                if plot:GetImprovementType() ~= -1 then
                    entry.improvement = GameInfo.Improvements[plot:GetImprovementType()].ImprovementType -- UNVERIFIED
                end
                plots[#plots + 1] = entry
            end
        end
    end

    return {
        width = width,
        height = height,
        revealed_plots = plots,
    }
end

CivSim_Map = {
    state = CivSim_Map_GetState,
}

-- Example dispatch (performed by the Nexus dispatcher, not by this file):
--   print(CivSim_JsonEncode(CivSim_Map.state()))
