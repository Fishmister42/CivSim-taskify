"""T077 part 2: _G is nil and UI/Network are opaque userdata, so enumerate via
getfenv() and probe candidate save-path names directly. Evidence -> files."""
from __future__ import annotations

import re
from pathlib import Path

from nexus_probe import Probe

OUT = Path(__file__).parent / "t077_raw"
OUT.mkdir(exist_ok=True)
PREFIX = re.compile(r"^O\x00[A-Za-z_0-9]+:\s?")


def clean(t: str) -> str:
    return "\n".join(
        PREFIX.sub("", ln).rstrip() for ln in t.splitlines() if PREFIX.sub("", ln).strip()
    )


ENV_LUA = r"""
local env = nil
if getfenv then local ok, e = pcall(getfenv, 1); if ok then env = e end end
print("getfenv available: " .. tostring(getfenv ~= nil) .. ", env type: " .. type(env))
if type(env) == "table" then
  local keys = {}
  for k, v in pairs(env) do keys[#keys+1] = tostring(k) .. "  :  " .. type(v) end
  table.sort(keys)
  print("=== ENV (" .. #keys .. " entries) ===")
  for _, k in ipairs(keys) do print("  " .. k) end
end
for _, nm in ipairs({"UI", "Network", "Game", "SaveLocations", "SaveTypes", "UIManager", "Automation"}) do
  local v = env and env[nm]
  print("global " .. nm .. " : " .. type(v))
  local mt = nil
  if v ~= nil then local ok, m = pcall(getmetatable, v); if ok then mt = m end end
  if type(mt) == "table" then
    local mk = {}
    for k, _ in pairs(mt) do mk[#mk+1] = tostring(k) end
    table.sort(mk)
    print("  metatable keys: " .. table.concat(mk, ", "))
    if type(mt.__index) == "table" then
      local ik = {}
      for k, vv in pairs(mt.__index) do ik[#ik+1] = tostring(k) .. ":" .. type(vv) end
      table.sort(ik)
      print("  __index (" .. #ik .. "): " .. table.concat(ik, ", "))
    else
      print("  __index type: " .. type(mt.__index))
    end
  end
end
"""

CANDIDATES = [
    # The path Civ VI's own SaveGameMenu.lua is documented to use
    "Network.SaveGame", "Network.LoadGame", "Network.QuickSave", "Network.QuickLoad",
    "Network.BeginSaveGame", "Network.IsSaveGameInProgress", "Network.GetSaveGameList",
    # Civ V-era names (expected absent; absence is the evidence)
    "UI.QuickSave", "UI.QuickLoad", "UI.SaveGame", "UI.LoadGame",
    "UI.RequestSave", "UI.SaveGameAs", "UI.QueueSaveGame",
    # Other plausible surfaces
    "Game.SaveGame", "Game.QuickSave", "Game.Save",
    "UI.GetInterfaceMode", "UI.SetInterfaceMode",  # control probes: should exist
    "Game.GetCurrentGameTurn",                      # control probe: known to exist
    "SaveLocations.LOCAL_STORAGE", "SaveTypes.SINGLE_PLAYER",
    "UIManager.QueuePopup", "Automation.SaveGame",
]

probe_lua = 'print("=== CANDIDATE SAVE PATHS ===")\n'
for dotted in CANDIDATES:
    root, _, attr = dotted.partition(".")
    probe_lua += (
        f'do local ok, v = pcall(function() return {dotted} end)\n'
        f'  if not ok then print("  {dotted}  ->  ERROR")\n'
        f'  else print("  {dotted}  ->  " .. type(v)) end end\n'
    )

p = Probe()
states = p.handshake()
summary = []
for label in ("GameCore_Tuner", "InGame"):
    idx = states[label]
    for tag, lua in (("env", ENV_LUA), ("candidates", probe_lua)):
        res, raw = p.exec_lua(idx, lua, wait=12.0)
        body = clean(res if res else raw)
        (OUT / f"{label}__{tag}.txt").write_text(body)
        summary.append(f"{label}/{tag}: {len(body.splitlines())} lines")
p.close()
print("\n".join(summary))
