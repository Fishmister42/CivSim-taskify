print("fired=" .. tostring(CIVSIM_FIRED))
if type(CIVSIM_SAVES) ~= "table" then
  print("CIVSIM_SAVES type=" .. type(CIVSIM_SAVES))
else
  local n = 0
  for _ in pairs(CIVSIM_SAVES) do n = n + 1 end
  print("results entries=" .. n)
  for k, v in pairs(CIVSIM_SAVES) do
    if type(v) == "table" then
      local f = {}
      for a, b in pairs(v) do f[#f + 1] = tostring(a) .. "=" .. tostring(b) end
      table.sort(f)
      print("  [" .. tostring(k) .. "] " .. table.concat(f, " | "))
    else
      print("  [" .. tostring(k) .. "] " .. type(v) .. " " .. tostring(v))
    end
  end
end
