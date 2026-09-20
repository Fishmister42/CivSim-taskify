"""Game-position fingerprint, used to answer FR-034: does load restore an IDENTICAL position?"""
from __future__ import annotations

import re

from nexus_probe import Probe

PRE = re.compile(r"^O\x00[A-Za-z_0-9]+:\s?")


def clean(t: str) -> str:
    return "\n".join(
        PRE.sub("", ln).rstrip() for ln in t.splitlines() if PRE.sub("", ln).strip()
    )


FINGERPRINT_LUA = r"""
local function say(label, f)
  local ok, v = pcall(f)
  print(label .. "=" .. (ok and tostring(v) or "ERR"))
end
local lp = Game.GetLocalPlayer()
print("turn=" .. tostring(Game.GetCurrentGameTurn()))
print("localplayer=" .. tostring(lp))
local p = Players[lp]
say("gold",    function() return string.format("%.2f", p:GetTreasury():GetGoldBalance()) end)
say("science", function() return string.format("%.2f", p:GetTechs():GetScienceYield()) end)
say("culture", function() return string.format("%.2f", p:GetCulture():GetCultureYield()) end)
say("faith",   function() return string.format("%.2f", p:GetReligion():GetFaithBalance()) end)
say("civ",     function() return PlayerConfigurations[lp]:GetCivilizationTypeName() end)
say("leader",  function() return PlayerConfigurations[lp]:GetLeaderTypeName() end)
-- units, sorted so ordering noise cannot masquerade as drift
local ok = pcall(function()
  local list = {}
  for _, u in p:GetUnits():Members() do
    list[#list+1] = string.format("%s@%d,%d hp=%s moves=%s",
      tostring(u:GetType()), u:GetX(), u:GetY(),
      tostring(u:GetDamage()), tostring(u:GetMovesRemaining()))
  end
  table.sort(list)
  print("unitcount=" .. #list)
  for i, s in ipairs(list) do print("  unit[" .. i .. "] " .. s) end
end)
if not ok then print("unitcount=ERR") end
local ok2 = pcall(function()
  local list = {}
  for _, c in p:GetCities():Members() do
    list[#list+1] = string.format("%s@%d,%d pop=%s", tostring(c:GetName()), c:GetX(), c:GetY(), tostring(c:GetPopulation()))
  end
  table.sort(list)
  print("citycount=" .. #list)
  for i, s in ipairs(list) do print("  city[" .. i .. "] " .. s) end
end)
if not ok2 then print("citycount=ERR") end
"""


def fingerprint(p: Probe, ig: int) -> str:
    res, raw = p.exec_lua(ig, FINGERPRINT_LUA, wait=8.0)
    return clean(res if res else raw)


def save_as(p: Probe, ig: int, name: str) -> str:
    lua = f"""
local g = {{}}
g.Name = "{name}"
g.Location = SaveLocations.LOCAL_STORAGE
g.Type = SaveTypes.SINGLE_PLAYER
g.IsAutosave = false
g.IsQuicksave = false
local ok, err = pcall(function() return Network.SaveGame(g) end)
print("save[{name}] ok=" .. tostring(ok) .. " ret=" .. tostring(err))
"""
    res, raw = p.exec_lua(ig, lua, wait=8.0)
    return clean(res if res else raw)
