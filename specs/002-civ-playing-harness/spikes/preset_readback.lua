local function s(l, f)
  local ok, v = pcall(f)
  print(l .. " = " .. (ok and tostring(v) or "ERR"))
end
local function resolve(label, value, tableName, field)
  local found = nil
  pcall(function()
    for row in GameInfo[tableName]() do
      if row.Hash == value then found = row[field] end
    end
  end)
  print(label .. " = " .. tostring(value) .. "  -> " .. tostring(found))
end

s("RuleSet", function() return GameConfiguration.GetRuleSet() end)
resolve("TurnTimerType", GameConfiguration.GetTurnTimerType(), "Difficulties", "DifficultyType")
print("  TURNTIMER_NONE hash     = " .. tostring(DB.MakeHash("TURNTIMER_NONE")))
print("  TURNTIMER_STANDARD hash = " .. tostring(DB.MakeHash("TURNTIMER_STANDARD")))
resolve("Difficulty", GameConfiguration.GetHandicapType(), "Difficulties", "DifficultyType")
resolve("GameSpeed", GameConfiguration.GetGameSpeedType(), "GameSpeeds", "GameSpeedType")
resolve("StartEra", GameConfiguration.GetStartEra(), "Eras", "EraType")
s("MaxTurns", function() return GameConfiguration.GetMaxTurns() end)
s("StartTurn", function() return GameConfiguration.GetStartTurn() end)
s("AIPlayerCount", function() return GameConfiguration.GetAIPlayerCount() end)
s("HumanPlayerCount", function() return GameConfiguration.GetHumanPlayerCount() end)
s("ParticipatingPlayerCount", function() return GameConfiguration.GetParticipatingPlayerCount() end)
s("IsAnyMultiplayer", function() return GameConfiguration.IsAnyMultiplayer() end)
s("MapScript", function() return MapConfiguration.GetScript() end)
s("MapSize", function() return MapConfiguration.GetValue("MAP_SIZE") end)
s("CityStateCount", function() return MapConfiguration.GetValue("CITY_STATE_COUNT") end)
-- local player slot
local ok = pcall(function()
  local ids = GameConfiguration.GetParticipatingPlayerIDs()
  for i, id in ipairs(ids) do
    local pc = PlayerConfigurations[id]
    print(string.format("  player[%d] id=%s civ=%s leader=%s human=%s",
      i, tostring(id), tostring(pc:GetCivilizationTypeName()),
      tostring(pc:GetLeaderTypeName()), tostring(pc:IsHuman())))
  end
end)
if not ok then print("player enumeration failed") end
