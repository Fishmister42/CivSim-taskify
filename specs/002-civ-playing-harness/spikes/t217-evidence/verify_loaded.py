"""Far-side assertion for T217: is the client actually in the LOADED game?"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/matt/CivSolver/specs/002-civ-playing-harness/spikes/r5-raw")
from nexus_probe import Probe  # noqa: E402

LUA = r"""
local function show(label, fn)
  local ok, v = pcall(fn)
  print(string.format("  %-5s %-38s = %s", ok and "OK" or "ERR", label, tostring(v)))
end
show("Game.GetCurrentGameTurn()", function() return Game.GetCurrentGameTurn() end)
show("Game.GetLocalPlayer()",     function() return Game.GetLocalPlayer() end)
show("UI.IsInFrontEnd()",         function() return UI.IsInFrontEnd() end)
local pid = Game.GetLocalPlayer()
show("leader",  function() return PlayerConfigurations[pid]:GetLeaderTypeName() end)
show("civ",     function() return PlayerConfigurations[pid]:GetCivilizationTypeName() end)
show("player name", function() return PlayerConfigurations[pid]:GetPlayerName() end)
show("units",   function()
  local n = 0
  for _, u in Players[pid]:GetUnits():Members() do n = n + 1 end
  return n end)
show("cities",  function()
  local n = 0
  for _, c in Players[pid]:GetCities():Members() do n = n + 1 end
  return n end)
show("gold",    function() return Players[pid]:GetTreasury():GetGoldBalance() end)
"""


def main() -> None:
    p = Probe(app="civsim-verify-loaded")
    try:
        states = p.handshake()
        print(f"=== {len(states)} states; game states: "
              f"{[n for n in ('InGame', 'GameCore_Tuner') if n in states]} ===")
        if "InGame" not in states:
            print(f"!! InGame absent. states={sorted(states)}")
            return
        out, raw = p.exec_lua(states["InGame"], LUA, wait=8.0)
        print(out or f"(no sentinel)\nRAW:\n{raw[:2000]}")
    finally:
        p.close()


if __name__ == "__main__":
    main()
