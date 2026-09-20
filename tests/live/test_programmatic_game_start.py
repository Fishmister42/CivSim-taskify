"""The complete programmatic new-game start -- and the pinned-leader question.

Assembled from Firaxis' own automation (`automation_standardtests.lua`):

    GameConfiguration.SetToDefaults()
    Network.LoadGame({FileType = SaveFileTypes.GAME_CONFIGURATION, ...})  -- verified (R8)
    PlayerConfigurations[0]:SetSlotStatus(SlotStatus.SS_TAKEN)            -- the missing piece
    PlayerConfigurations[0]:SetLeaderTypeName / SetCivilizationTypeName   -- verified
    Network.HostGame(ServerType.SERVER_TYPE_NONE)

R8 established that `HostGame` refuses with `1` when the configuration has no
human player (`GetHumanPlayerCount() == 0`), and `ApplyHumanPlayersToConfiguration`
is the helper that fixes it: a human slot is `SlotStatus.SS_TAKEN`.

If the game starts, the real question follows immediately: does the leader pinned
before the start survive map generation? `Play Now` randomises it, so a
reassignment step demonstrably exists on some path.

    python3 -m tests.live.test_programmatic_game_start
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "specs/002-civ-playing-harness/spikes/r5-raw"))

from civsim_harness.host.detect import LinuxSessionType  # noqa: E402
from civsim_harness.host.linux.adapter import LinuxHostPlatform  # noqa: E402
from civsim_harness.host.port import GameProcess, InputEvent, InputEventKind  # noqa: E402
from nexus_probe import Probe  # noqa: E402

LEADER = "LEADER_CYRUS"
CIV = "CIVILIZATION_PERSIA"
PRESET = "CivSim DEFAULT"


def tuner_up() -> bool:
    return b"4318" in subprocess.run(["ss", "-ltn"], capture_output=True).stdout


def connect(attempts: int = 10) -> Probe:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return Probe(app="civsim-game-start")
        except OSError as exc:
            last = exc
            time.sleep(2.0)
    raise RuntimeError(f"tuner unreachable: {last}")


def lua(state: str, body: str, wait: float = 10.0) -> str:
    p = connect()
    try:
        states = p.handshake()
        if state not in states:
            return f"!! state {state!r} absent; {len(states)} states present"
        out, raw = p.exec_lua(states[state], body, wait=wait)
        return (out or f"(no sentinel) RAW={raw[:400]}").replace("\x00", "")
    finally:
        p.close()


def states_now() -> dict[str, int]:
    p = connect()
    try:
        return p.handshake()
    finally:
        p.close()


def wait_for_state(name: str, budget_s: int = 90) -> bool:
    """Front-end states load lazily (R8): 2 at a fresh main menu, then 29."""
    deadline = time.time() + budget_s
    while time.time() < deadline:
        try:
            if name in states_now():
                return True
        except RuntimeError:
            pass
        time.sleep(5)
    return False


def escape_until_port_returns(budget_s: int = 240) -> int:
    adapter = LinuxHostPlatform(session_type=LinuxSessionType.x11)
    sent = 0
    deadline = time.time() + budget_s
    while time.time() < deadline:
        if tuner_up():
            return sent
        out = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
        if out:
            w = adapter.find_game_window(GameProcess(pid=int(out[0]), name="Civ6"))
            if w is not None:
                subprocess.run(["xdotool", "windowactivate", str(w.handle)], check=False)
                time.sleep(0.5)
            adapter.send_input([InputEvent(kind=InputEventKind.key_press, key="Escape")])
            sent += 1
        time.sleep(5.0)
    return sent


def main() -> int:
    st = states_now()
    if "InGame" in st:
        print("[0] in-game -> ExitToMainMenu()")
        lua("InGame", "pcall(function() Events.ExitToMainMenu() end)\nprint('exit issued')")
        for _ in range(40):
            time.sleep(3)
            try:
                if "InGame" not in states_now():
                    break
            except RuntimeError:
                pass

    if not wait_for_state("HostGame"):
        print("!! HostGame state never appeared")
        return 1
    print("[0] front end ready, HostGame present")

    print(f"\n[1] SetToDefaults + load '{PRESET}' + assign a HUMAN slot + pin {LEADER}")
    print(lua("HostGame", f"""
local function show(l, f)
  local ok, v = pcall(f)
  print(string.format("  %-5s %-40s = %s", ok and "OK" or "ERR", l, tostring(v)))
end

show("SlotStatus.SS_TAKEN", function() return SlotStatus.SS_TAKEN end)
show("SetToDefaults", function() GameConfiguration.SetToDefaults(); return "done" end)

local lp = {{}};
lp.Location = SaveLocations.LOCAL_STORAGE; lp.Type = SaveTypes.SINGLE_PLAYER;
lp.FileType = SaveFileTypes.GAME_CONFIGURATION; lp.IsAutosave = false;
lp.IsQuicksave = false; lp.Directory = SaveDirectories.DEFAULT; lp.Name = "{PRESET}";
show("LoadGame(GAME_CONFIGURATION)",
  function() return Network.LoadGame(lp, ServerType.SERVER_TYPE_NONE) end)

show("humanCount BEFORE slot assign", function() return GameConfiguration.GetHumanPlayerCount() end)
show("SetSlotStatus(0, SS_TAKEN)",
  function() PlayerConfigurations[0]:SetSlotStatus(SlotStatus.SS_TAKEN); return "done" end)
show("humanCount AFTER slot assign", function() return GameConfiguration.GetHumanPlayerCount() end)

show("SetLeaderTypeName", function()
  PlayerConfigurations[0]:SetLeaderTypeName("{LEADER}"); return "done" end)
show("SetCivilizationTypeName", function()
  PlayerConfigurations[0]:SetCivilizationTypeName("{CIV}"); return "done" end)
show("leader (setup)", function() return PlayerConfigurations[0]:GetLeaderTypeName() end)
show("civ    (setup)", function() return PlayerConfigurations[0]:GetCivilizationTypeName() end)
show("turnTimer", function() return GameConfiguration.GetTurnTimerType() end)
show("TURNTIMER_NONE hash", function() return DB.MakeHash("TURNTIMER_NONE") end)
"""))

    print("\n[2] Network.HostGame(ServerType.SERVER_TYPE_NONE)")
    print(lua("HostGame", """
local ok, ret = pcall(function()
  return Network.HostGame(ServerType.SERVER_TYPE_NONE)
end)
print("  HostGame ok=" .. tostring(ok) .. " ret=" .. tostring(ret))
"""))

    print("\n[3] waiting for the game (map generation, then the intro screen)")
    time.sleep(25)
    sent = escape_until_port_returns()
    print(f"    {sent} Escape press(es) while waiting")

    deadline = time.time() + 300
    reached = False
    while time.time() < deadline:
        try:
            if "InGame" in states_now():
                reached = True
                break
        except RuntimeError:
            pass
        escape_until_port_returns(budget_s=30)
        time.sleep(5)

    if not reached:
        print("  !! never reached InGame -- the game did not start")
        return 1

    print("\n[4] THE QUESTION: is the pinned leader still the one playing?")
    print(lua("InGame", f"""
local pid = Game.GetLocalPlayer()
local cfg = PlayerConfigurations[pid]
local leader = tostring(cfg:GetLeaderTypeName())
local civ    = tostring(cfg:GetCivilizationTypeName())
print("  localPlayer            = " .. tostring(pid))
print("  leader (in-game)       = " .. leader)
print("  civ    (in-game)       = " .. civ)
print("  turn                   = " .. tostring(Game.GetCurrentGameTurn()))
print("  turnTimer (in-game)    = " .. tostring(GameConfiguration.GetTurnTimerType()))
print("  PINNED LEADER SURVIVED = " .. tostring(leader == "{LEADER}"))
print("  PINNED CIV SURVIVED    = " .. tostring(civ == "{CIV}"))
"""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
