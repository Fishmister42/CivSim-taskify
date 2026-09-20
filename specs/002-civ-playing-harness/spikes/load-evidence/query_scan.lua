-- Scan THIS state's Events/LuaEvents for anything file/save/load shaped.
local function scan(name, t)
  if t == nil then print("=== " .. name .. " = nil ===") return end
  local ok = pcall(function()
    local hits = {}
    for k, v in pairs(t) do
      local l = string.lower(tostring(k))
      if string.find(l, "file", 1, true) or string.find(l, "save", 1, true)
         or string.find(l, "load", 1, true) or string.find(l, "quer", 1, true) then
        hits[#hits + 1] = "  " .. name .. "." .. tostring(k)
      end
    end
    table.sort(hits)
    print("=== " .. name .. " file/save/load/query members: " .. #hits .. " ===")
    for _, h in ipairs(hits) do print(h) end
  end)
  if not ok then print("=== " .. name .. " NOT ITERABLE ===") end
end
scan("Events", Events)
scan("LuaEvents", LuaEvents)
print("UI.QuerySaveGameList = " .. type(UI.QuerySaveGameList))
print("Network.LoadGame = " .. type(Network.LoadGame))
