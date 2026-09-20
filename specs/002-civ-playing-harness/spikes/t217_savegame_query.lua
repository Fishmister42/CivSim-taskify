-- T217: the two remaining leads on `UI.QuerySaveGameList`, before the load
-- path may honestly be declared a Principle II `firetuner_gap`.
--
-- Run in the **`LoadGameMenu`** state (index 112 in-game, 18 at the setup
-- screen -- RE-RESOLVE BY NAME, indices move across phase transitions), with
-- the Load Game screen OPEN. Round 3 of load-path-linux.md tried it both open
-- and closed with identical results, so the screen being open is not itself
-- the missing ingredient -- but it costs nothing and removes one variable.
--
-- Why this exists: Principle II requires *documented evidence* of a Firetuner
-- gap before a bespoke UI driver is justified. The spike has strong evidence
-- (six `gameFile` shapes refused, handler registers but never fires, `getfenv`
-- closed) but two cheap leads were never tried. Declaring the gap without
-- trying them would be an assertion, not evidence.
--
-- Uses the established async pattern: Lua globals PERSIST across tuner
-- commands, so a handler stores into a global and a LATER command reads it.
-- Run section [4] as a SEPARATE command after [3].

local function try(label, fn)
  local ok, v = pcall(fn)
  print(string.format("  %-5s %-52s = %s", ok and "OK" or "ERR", label, tostring(v)))
  return ok, v
end

print("=== T217 save-game query probe ===")

-- [1] The strongest untried lead: is LuaEvents ITERABLE?
-- `_G` and `getfenv` are nil in this sandbox and `UI`/`Network` are opaque
-- userdata yielding 0 entries, but `Game` IS iterable -- so iterability is
-- per-object here, not a blanket property. If LuaEvents enumerates, the
-- event name stops being a guessing game entirely.
print("\n[1] enumerate LuaEvents (the event name, if this works)")
local enumerated = 0
local ok = pcall(function()
  for k, _ in pairs(LuaEvents) do
    enumerated = enumerated + 1
    local key = tostring(k)
    if key:find("File") or key:find("Save") or key:find("Query") or key:find("List") then
      print("  MATCH  LuaEvents." .. key)
    end
  end
end)
print(string.format("  pairs(LuaEvents) ok=%s entries=%d", tostring(ok), enumerated))
if enumerated == 0 then
  print("  -> opaque like UI/Network; fall through to name probing")
end

-- [2] Probe candidate event names for existence.
print("\n[2] candidate event names")
local candidates = {
  "FileListQueryResults",
  "FileListQueryComplete",
  "SaveFileQueryResults",
  "SaveGameListQueryResults",
  "QuerySaveGameListComplete",
  "FileListQueryUpdated",
}
for _, name in ipairs(candidates) do
  local exists = false
  pcall(function() exists = (LuaEvents[name] ~= nil) end)
  print(string.format("  %-32s exists=%s", name, tostring(exists)))
end

-- [3] Register handlers on every candidate, storing into PERSISTENT globals,
-- then vary the query parameters. Read the results in a LATER command ([4]).
print("\n[3] register handlers + issue queries with varied parameters")
CIVSIM_T217 = {}
for _, name in ipairs(candidates) do
  pcall(function()
    LuaEvents[name].Add(function(...)
      local n = select("#", ...)
      CIVSIM_T217[name] = "fired argc=" .. tostring(n)
      local first = select(1, ...)
      if type(first) == "table" then
        local count = 0
        for _ in pairs(first) do count = count + 1 end
        CIVSIM_T217[name] = CIVSIM_T217[name] .. " table_entries=" .. tostring(count)
      end
    end)
  end)
end

-- Round 3 used one parameter shape. `ret=0` may be a result COUNT rather than
-- a query handle -- in which case the query matched nothing and the PARAMETERS
-- are wrong, which is what this varies.
local shapes = {
  { label = "Directory+SaveLocation", q = function()
      return UI.QuerySaveGameList({ Directory = SaveLocations.LOCAL_STORAGE }) end },
  { label = "Location+SaveLocation", q = function()
      return UI.QuerySaveGameList({ Location = SaveLocations.LOCAL_STORAGE }) end },
  { label = "Location+FileType", q = function()
      return UI.QuerySaveGameList({
        Location = SaveLocations.LOCAL_STORAGE,
        FileType = SaveFileTypes.GAME_STATE }) end },
  { label = "Directory+GameType", q = function()
      return UI.QuerySaveGameList({
        Directory = SaveLocations.LOCAL_STORAGE,
        GameType = SaveGameTypes.SINGLE_PLAYER }) end },
  { label = "empty table", q = function() return UI.QuerySaveGameList({}) end },
  { label = "no argument", q = function() return UI.QuerySaveGameList() end },
}
for _, shape in ipairs(shapes) do
  try(shape.label, shape.q)
end

-- What do the enum tables actually contain? Round 3 assumed members that were
-- never confirmed to exist.
print("\n  enum members actually present:")
for _, enumName in ipairs({ "SaveLocations", "SaveFileTypes", "SaveGameTypes" }) do
  local found = {}
  pcall(function()
    for k, v in pairs(_ENV and _ENV[enumName] or nil) do
      found[#found + 1] = tostring(k) .. "=" .. tostring(v)
    end
  end)
  if #found == 0 then
    -- _ENV may be absent too; probe the known names directly
    pcall(function()
      local t = enumName == "SaveLocations" and SaveLocations
        or enumName == "SaveFileTypes" and SaveFileTypes or SaveGameTypes
      for k, v in pairs(t) do found[#found + 1] = tostring(k) .. "=" .. tostring(v) end
    end)
  end
  print(string.format("    %-16s %s", enumName,
    #found > 0 and table.concat(found, ", ") or "(not enumerable / absent)"))
end

print("\n=== run section [4] as a SEPARATE tuner command, after a second or two ===")
print("for k,v in pairs(CIVSIM_T217) do print(k..' -> '..tostring(v)) end")
print("=== end T217 probe ===")
