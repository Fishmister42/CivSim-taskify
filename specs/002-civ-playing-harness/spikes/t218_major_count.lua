-- T218 follow-up: `GetAIPlayerCount()` returned 6 at the Create Game screen
-- (preset_readback.txt) but 16 in-game on a loaded save. If the in-game number
-- counts CITY-STATES as AI players, then reading `opponents.major_count`
-- in-game yields 16 where preparation recorded 6 -- an UnreadSetting-class
-- mismatch that lands a run `failed` while every unit test passes.
--
-- This probe does not assume that. It counts majors and minors separately and
-- lets the arithmetic say whether 16 = majors + city-states.

local function try(label, fn)
  local ok, v = pcall(fn)
  if ok and v ~= nil then
    print(string.format("  OK    %-48s = %s (%s)", label, tostring(v), type(v)))
    return v
  elseif ok then
    print(string.format("  nil   %-48s", label))
  else
    print(string.format("  ERR   %-48s", label))
  end
  return nil
end

print("=== T218 follow-up: what does GetAIPlayerCount() actually count? ===")

print("\n[1] the config-level counts")
try("GameConfiguration.GetAIPlayerCount()",
  function() return GameConfiguration.GetAIPlayerCount() end)
try("GameConfiguration.GetParticipatingPlayerCount()",
  function() return GameConfiguration.GetParticipatingPlayerCount() end)
try("GameConfiguration.GetHumanPlayerCount()",
  function() return GameConfiguration.GetHumanPlayerCount() end)
try("GameConfiguration.GetValue('CITY_STATE_COUNT')",
  function() return GameConfiguration.GetValue("CITY_STATE_COUNT") end)
try("MapConfiguration.GetValue('CITY_STATE_COUNT')",
  function() return MapConfiguration.GetValue("CITY_STATE_COUNT") end)

print("\n[2] count the live Players table by major/minor")
local majors, minors, barbarian, dead, total = 0, 0, 0, 0, 0
pcall(function()
  for i, player in ipairs(Players) do
    total = total + 1
    local isMajor, isMinor, isBarb, alive = nil, nil, nil, nil
    pcall(function() isMajor = player:IsMajor() end)
    pcall(function() isBarb  = player:IsBarbarian() end)
    pcall(function() alive   = player:IsAlive() end)
    if alive then
      if isBarb then barbarian = barbarian + 1
      elseif isMajor then majors = majors + 1
      else minors = minors + 1 end
    else
      dead = dead + 1
    end
  end
end)
print(string.format("  Players table: total=%d  alive-majors=%d  alive-minors=%d  barbarian=%d  not-alive=%d",
  total, majors, minors, barbarian, dead))
print(string.format("  -> majors excluding the human = %d", majors - 1))
print(string.format("  -> majors-1 + minors = %d", (majors - 1) + minors))

print("\n[3] the same question from the config player-ID lists")
for _, fn in ipairs({ "GetAIPlayerIDs", "GetParticipatingPlayerIDs", "GetHumanPlayerIDs" }) do
  pcall(function()
    local ids = GameConfiguration[fn]()
    local parts = {}
    for _, id in ipairs(ids) do parts[#parts + 1] = tostring(id) end
    print(string.format("  %-28s n=%-3d ids=[%s]", fn .. "()", #ids, table.concat(parts, ",")))
  end)
end

print("\n[4] per-player identity, so the classification is auditable")
pcall(function()
  for i, player in ipairs(Players) do
    local id = player:GetID()
    local alive, isMajor, isBarb = false, false, false
    pcall(function() alive = player:IsAlive() end)
    pcall(function() isMajor = player:IsMajor() end)
    pcall(function() isBarb = player:IsBarbarian() end)
    if alive then
      local leader = "?"
      pcall(function() leader = PlayerConfigurations[id]:GetLeaderTypeName() end)
      print(string.format("    id=%-3d major=%-5s barb=%-5s leader=%s",
        id, tostring(isMajor), tostring(isBarb), tostring(leader)))
    end
  end
end)

print("\n=== end T218 follow-up ===")
