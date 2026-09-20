-- T218: confirm the five UNVERIFIED `_SETTING_GETTERS` against a live client.
--
-- Run at the Create Game screen (`HostGame` state) with `CivSim DEFAULT`
-- loaded, then again in-game -- V2 must read the same fields at whichever
-- phase preparation actually runs, and this repo has already been bitten by
-- assuming a phase (see the preparation-sequence defect, 2026-09-20).
--
-- Discipline this file follows, from earlier spikes:
--   * `_G`, `getfenv` and `require` are absent, so exhaustive enumeration is
--     impossible -- candidate names are probed under `pcall` instead.
--   * Config getters return TYPE HASHES, not strings. `DB.MakeHash(name)`
--     resolves forward (best for an assertion); scanning a `GameInfo` table
--     for `row.Hash == value` resolves backward (best for a record).
--   * `MapConfiguration.GetValue(key)` is NOT uniformly available: `MAP_SIZE`
--     returned a hash but `CITY_STATE_COUNT` returned nil on this same client.
--     Never assume a key works because a sibling did.

local function try(label, fn)
  local ok, v = pcall(fn)
  if ok and v ~= nil then
    print(string.format("  OK    %-46s = %s (%s)", label, tostring(v), type(v)))
    return v
  elseif ok then
    print(string.format("  nil   %-46s", label))
  else
    print(string.format("  ERR   %-46s", label))
  end
  return nil
end

-- Resolve a hash backwards by scanning a GameInfo table.
local function resolve(value, tableName, field)
  if value == nil or type(value) ~= "number" then return nil end
  local found = nil
  pcall(function()
    for row in GameInfo[tableName]() do
      if row.Hash == value then found = row[field] end
    end
  end)
  return found
end

local function report(label, value, tableName, field)
  local name = resolve(value, tableName, field)
  print(string.format("  ->    %-46s = %s  resolves to %s via GameInfo.%s",
    label, tostring(value), tostring(name), tostring(tableName)))
end

print("=== T218 setting-getter probe ===")

print("\n[1] map_seed  -- no candidate has ever been probed on this client")
local seed = nil
seed = try("GameConfiguration.GetValue('GAME_SYNC_RANDOM_SEED')",
  function() return GameConfiguration.GetValue("GAME_SYNC_RANDOM_SEED") end) or seed
seed = try("GameConfiguration.GetValue('GAME_RANDOM_SEED')",
  function() return GameConfiguration.GetValue("GAME_RANDOM_SEED") end) or seed
seed = try("GameConfiguration.GetValue('RANDOM_SEED')",
  function() return GameConfiguration.GetValue("RANDOM_SEED") end) or seed
seed = try("MapConfiguration.GetValue('RANDOM_SEED')",
  function() return MapConfiguration.GetValue("RANDOM_SEED") end) or seed
seed = try("MapConfiguration.GetValue('MAP_SEED')",
  function() return MapConfiguration.GetValue("MAP_SEED") end) or seed
try("GameConfiguration.GetRandomSeed()",
  function() return GameConfiguration.GetRandomSeed() end)

print("\n[2] map_settings.map_type  -- MapScript already read back 'Pangaea.lua'")
try("MapConfiguration.GetScript()", function() return MapConfiguration.GetScript() end)
local mapType = try("MapConfiguration.GetValue('MAP_SCRIPT')",
  function() return MapConfiguration.GetValue("MAP_SCRIPT") end)
if mapType then report("MAP_SCRIPT", mapType, "Maps", "File") end

print("\n[3] map_settings.map_size  -- previously a HASH (-1837222328), unresolved")
local mapSize = try("MapConfiguration.GetValue('MAP_SIZE')",
  function() return MapConfiguration.GetValue("MAP_SIZE") end)
if mapSize then
  report("MAP_SIZE", mapSize, "MapSizes", "MapSizeType")
  report("MAP_SIZE", mapSize, "Maps", "MapSizeType")
end
print("  forward check: DB.MakeHash('MAPSIZE_SMALL') = " ..
  tostring(DB.MakeHash("MAPSIZE_SMALL")))

print("\n[4] map_settings.resources  -- never probed; sibling GetValue keys have returned nil")
local res = nil
res = try("MapConfiguration.GetValue('RESOURCES')",
  function() return MapConfiguration.GetValue("RESOURCES") end) or res
res = try("MapConfiguration.GetValue('RESOURCE_DENSITY')",
  function() return MapConfiguration.GetValue("RESOURCE_DENSITY") end) or res
res = try("GameConfiguration.GetValue('RESOURCES')",
  function() return GameConfiguration.GetValue("RESOURCES") end) or res
if res then report("RESOURCES", res, "Resources", "ResourceType") end

print("\n[5] opponents.major_count  -- AIPlayerCount already read back 6")
try("GameConfiguration.GetAIPlayerCount()",
  function() return GameConfiguration.GetAIPlayerCount() end)
try("GameConfiguration.GetParticipatingPlayerCount()",
  function() return GameConfiguration.GetParticipatingPlayerCount() end)
try("GameConfiguration.GetHumanPlayerCount()",
  function() return GameConfiguration.GetHumanPlayerCount() end)
pcall(function()
  local ids = GameConfiguration.GetAIPlayerIDs()
  print("  ->    GetAIPlayerIDs() count = " .. tostring(#ids))
end)

print("\n[6] mod_set  -- 22 mods are active on this install (BBG + MP helper)")
pcall(function()
  local mods = Modding.GetActiveMods()
  print("  OK    Modding.GetActiveMods() count = " .. tostring(#mods))
  for i, m in ipairs(mods) do
    print(string.format("        [%d] id=%s name=%s", i, tostring(m.Id), tostring(m.Name)))
  end
end)
try("Modding.GetEnabledMods()", function() return #Modding.GetEnabledMods() end)

print("\n=== end T218 probe ===")
