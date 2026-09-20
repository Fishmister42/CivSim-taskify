print("DB.MakeHash = " .. type(DB.MakeHash))
local target = GameConfiguration.GetTurnTimerType()
print("target TurnTimerType = " .. tostring(target))
local cands = {"TURNTIMER_NONE","NO_TURNTIMER","TURNTIMER_STANDARD","TURNTIMER_DYNAMIC",
"TURNTIMER_FIXED","TURN_TIMER_NONE","NONE","TURNTIMER_OFF","TURNTIMER"}
for _, c in ipairs(cands) do
  local ok, h = pcall(function() return DB.MakeHash(c) end)
  if ok then
    print("  " .. c .. " -> " .. tostring(h) .. ((h == target) and "   <== MATCH" or ""))
  end
end

-- Resolve the other hashed config values against GameInfo tables, for the record.
local function resolve(label, value, tableName)
  local found = nil
  local ok = pcall(function()
    for row in GameInfo[tableName]() do
      if row.Hash == value then found = row.GameSpeedType or row.DifficultyType or row.EraType or row.Type end
    end
  end)
  print(label .. " hash=" .. tostring(value) .. " -> " .. tostring(found) .. (ok and "" or " (lookup failed)"))
end
resolve("GameSpeed",  GameConfiguration.GetGameSpeedType(), "GameSpeeds")
resolve("Difficulty", GameConfiguration.GetHandicapType(),  "Difficulties")
resolve("StartEra",   GameConfiguration.GetStartEra(),      "Eras")
