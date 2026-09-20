CIVSIM_SAVES = nil
CIVSIM_FIRED = 0
if CIVSIM_HANDLER_ADDED == nil then
  CIVSIM_HANDLER_ADDED = true
  local ok, err = pcall(function()
    LuaEvents.FileListQueryResults.Add(function(results)
      CIVSIM_FIRED = (CIVSIM_FIRED or 0) + 1
      CIVSIM_SAVES = results
    end)
  end)
  print("handler registered ok=" .. tostring(ok) .. " err=" .. tostring(err))
else
  print("handler already registered")
end
local q = {}
q.Directory = SaveLocations.LOCAL_STORAGE
q.FileType = SaveFileTypes.GAME_STATE
q.GameType = SaveTypes.SINGLE_PLAYER
local ok2, ret = pcall(function() return UI.QuerySaveGameList(q) end)
print("query issued ok=" .. tostring(ok2) .. " ret=" .. tostring(ret))
