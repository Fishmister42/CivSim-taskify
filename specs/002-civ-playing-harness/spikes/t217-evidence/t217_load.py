"""T217 step 3: attempt the load from a FRONT-END state using Firaxis' own
canonical table (automation_dailysmoketest.lua:258-279).

Asserts on what the far side did, not on what the call returned: the return
value is recorded, and then the game's own phase is re-read on a fresh
connection to see whether a load actually happened.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/matt/CivSolver/specs/002-civ-playing-harness/spikes/r5-raw")
from nexus_probe import Probe  # noqa: E402

SAVE_NAME = sys.argv[1] if len(sys.argv) > 1 else "civsim__spike__t0001"
STATE = sys.argv[2] if len(sys.argv) > 2 else "MainMenu"

LUA_LOAD = f"""
local function show(label, fn)
  local ok, v = pcall(fn)
  print(string.format("  %-5s %-40s = %s", ok and "OK" or "ERR", label, tostring(v)))
  return ok, v
end

show("UI.IsInFrontEnd()", function() return UI.IsInFrontEnd() end)
show("type(Network.LoadGame)", function() return type(Network.LoadGame) end)

local loadGame = {{}};
loadGame.Location   = SaveLocations.LOCAL_STORAGE;
loadGame.Type       = SaveTypes.SINGLE_PLAYER;
loadGame.IsAutosave = false;
loadGame.IsQuicksave= false;
loadGame.Directory  = SaveDirectories.DEFAULT;
loadGame.Name       = "{SAVE_NAME}";

print("  table: Location=" .. tostring(loadGame.Location)
   .. " Type=" .. tostring(loadGame.Type)
   .. " Directory=" .. tostring(loadGame.Directory)
   .. " Name=" .. tostring(loadGame.Name))

CIVSIM_T217_LOAD = "attempted"
local ok, ret = pcall(function()
  return Network.LoadGame(loadGame, ServerType.SERVER_TYPE_NONE)
end)
CIVSIM_T217_LOAD = "ok=" .. tostring(ok) .. " ret=" .. tostring(ret)
print("  >>> Network.LoadGame ok=" .. tostring(ok) .. " ret=" .. tostring(ret))
"""


def main() -> None:
    print(f"=== T217 front-end load attempt: save={SAVE_NAME!r} state={STATE!r} ===")
    p = Probe(app="civsim-t217-load")
    try:
        states = p.handshake()
        if STATE not in states:
            print(f"!! {STATE} absent; states present: {sorted(states)}")
            return
        out, raw = p.exec_lua(states[STATE], LUA_LOAD, wait=8.0)
        print(out or f"(no sentinel)\nRAW:\n{raw[:2000]}")
    finally:
        p.close()

    # --- far-side assertion: did the phase actually change? ---
    for delay in (5, 10, 20):
        time.sleep(delay)
        print(f"\n--- re-probe after ~{delay}s: what phase is the game in now? ---")
        try:
            q = Probe(app="civsim-t217-verify")
        except OSError as exc:
            print(f"  (tuner unreachable: {exc}) -- consistent with a phase transition")
            continue
        try:
            st = q.handshake()
            game_states = [n for n in st if n in ("InGame", "GameCore_Tuner")]
            print(f"  {len(st)} states; game states present: {game_states or 'NONE (still front end)'}")
            if game_states:
                out, _ = q.exec_lua(
                    st["InGame"],
                    'print("  turn=" .. tostring(Game.GetCurrentGameTurn())'
                    ' .. " localPlayer=" .. tostring(Game.GetLocalPlayer()))',
                    wait=5.0,
                )
                print(out)
                return
        finally:
            q.close()


if __name__ == "__main__":
    main()
