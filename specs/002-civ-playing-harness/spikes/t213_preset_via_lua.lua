-- Can the `CivSim DEFAULT` setup preset be applied from Lua?
--
-- The project has treated `CivSim DEFAULT.Civ6Cfg` as UI-only. Firaxis' own
-- automation loads a *setup configuration* through the same `Network.LoadGame`
-- used for savegames, discriminated by a file type
-- (`automation_standardtests.lua:283-298`):
--
--     loadParams.FileType = SaveFileTypes.GAME_CONFIGURATION;
--
-- If that works, preparation can apply the whole preset -- ruleset, map, speed,
-- AI count, turn timer -- without touching the UI, then pin the leader on top.
--
-- Run in the `HostGame` state. Front-end states load LAZILY (2 at a fresh main
-- menu, eventually 29), so wait for `HostGame` to exist before dispatching.

local function show(label, fn)
  local ok, v = pcall(fn)
  print(string.format("  %-5s %-42s = %s", ok and "OK" or "ERR", label, tostring(v)))
  return ok, v
end

print("=== T213: apply the CivSim DEFAULT preset from Lua ===")

print("\n[1] does the file-type discriminator exist here?")
show("type(SaveFileTypes)", function() return type(SaveFileTypes) end)
show("SaveFileTypes.GAME_CONFIGURATION", function() return SaveFileTypes.GAME_CONFIGURATION end)
print("  SaveFileTypes members:")
pcall(function()
  local parts = {}
  for k, v in pairs(SaveFileTypes) do parts[#parts + 1] = tostring(k) .. "=" .. tostring(v) end
  print("    " .. table.concat(parts, ", "))
end)

print("\n[2] configuration BEFORE (so the change is attributable)")
show("GetRuleSet()",       function() return GameConfiguration.GetRuleSet() end)
show("GetAIPlayerCount()", function() return GameConfiguration.GetAIPlayerCount() end)
show("GetHumanPlayerCount()", function() return GameConfiguration.GetHumanPlayerCount() end)
show("GetGameSpeedType()", function() return GameConfiguration.GetGameSpeedType() end)
show("GetTurnTimerType()", function() return GameConfiguration.GetTurnTimerType() end)
show("MapConfiguration.GetScript()", function() return MapConfiguration.GetScript() end)
show("MAP_SIZE",           function() return MapConfiguration.GetValue("MAP_SIZE") end)

print("\n[3] SetToDefaults(), then load the preset as a GAME_CONFIGURATION")
show("SetToDefaults()", function() GameConfiguration.SetToDefaults(); return "done" end)

local loadParams = {}
loadParams.Location    = SaveLocations.LOCAL_STORAGE
loadParams.Type        = SaveTypes.SINGLE_PLAYER
loadParams.FileType    = SaveFileTypes.GAME_CONFIGURATION
loadParams.IsAutosave  = false
loadParams.IsQuicksave = false
loadParams.Directory   = SaveDirectories.DEFAULT
loadParams.Name        = "CivSim DEFAULT"
show("Network.LoadGame(GAME_CONFIGURATION)",
  function() return Network.LoadGame(loadParams, ServerType.SERVER_TYPE_NONE) end)

print("\n[4] configuration AFTER -- did the preset actually land?")
show("GetRuleSet()",       function() return GameConfiguration.GetRuleSet() end)
show("GetAIPlayerCount()", function() return GameConfiguration.GetAIPlayerCount() end)
show("GetHumanPlayerCount()", function() return GameConfiguration.GetHumanPlayerCount() end)
show("GetGameSpeedType()", function() return GameConfiguration.GetGameSpeedType() end)
show("GetTurnTimerType()", function() return GameConfiguration.GetTurnTimerType() end)
show("MapConfiguration.GetScript()", function() return MapConfiguration.GetScript() end)
show("MAP_SIZE",           function() return MapConfiguration.GetValue("MAP_SIZE") end)

print("\n[5] forward hash checks, so the numbers above are readable")
for _, name in ipairs({ "MAPSIZE_SMALL", "GAMESPEED_ONLINE", "TURNTIMER_NONE",
                        "TURNTIMER_STANDARD", "RULESET_EXPANSION_2" }) do
  print(string.format("    DB.MakeHash(%-22s) = %s", name, tostring(DB.MakeHash(name))))
end

print("\n=== end T213 preset probe ===")
