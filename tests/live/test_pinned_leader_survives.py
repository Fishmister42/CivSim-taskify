"""Does a leader pinned in Lua at setup survive into the running game?

The owner's ruling is that runs start as LEADER_CYRUS / CIVILIZATION_PERSIA, and
because a saved setup configuration does NOT hold a civ selection, preparation
pins it in Lua at the HostGame state and reads it back there. That read-back is
known to pass *at the setup screen*.

What has never been checked is whether it survives **map generation and game
start**. It matters because `Play Now` is known to randomise the leader, which
proves a reassignment step exists somewhere on that path -- so a pin that
verifies at preparation time could still play the run as someone else, and V2
would record a leader the game never used.

Sequence: pin -> read back at setup -> Network.HostGame -> dismiss intro ->
read back in-game. The last read is the whole point; the rest is scaffolding.

    python3 -m tests.live.test_pinned_leader_survives
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


def tuner_up() -> bool:
    return b"4318" in subprocess.run(["ss", "-ltn"], capture_output=True).stdout


def connect(attempts: int = 10) -> Probe:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return Probe(app="civsim-pinned-leader")
        except OSError as exc:
            last = exc
            time.sleep(2.0)
    raise RuntimeError(f"tuner unreachable: {last}")


def lua(state: str, body: str, wait: float = 8.0) -> str:
    p = connect()
    try:
        states = p.handshake()
        if state not in states:
            return f"!! state {state!r} absent; have {len(states)} states"
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


def press_escape_until_port_returns(budget_s: int = 180) -> None:
    """Same retry shape the loader needs: the intro screen is unobservable through
    the tuner it is holding closed, so the only signal is the port returning."""
    adapter = LinuxHostPlatform(session_type=LinuxSessionType.x11)
    sent = 0
    deadline = time.time() + budget_s
    while time.time() < deadline:
        if tuner_up():
            if sent:
                print(f"    port back after {sent} Escape press(es)")
            return
        out = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
        if out:
            window = adapter.find_game_window(GameProcess(pid=int(out[0]), name="Civ6"))
            if window is not None:
                subprocess.run(["xdotool", "windowactivate", str(window.handle)], check=False)
                time.sleep(0.5)
            adapter.send_input([InputEvent(kind=InputEventKind.key_press, key="Escape")])
            sent += 1
        time.sleep(5.0)
    print("    gave up waiting for the port")


def main() -> int:
    st = states_now()
    print(f"[0] {len(st)} states; in front end = {'InGame' not in st}")

    if "InGame" in st:
        print("[1] in-game -> Events.ExitToMainMenu()")
        print(lua("InGame", 'pcall(function() Events.ExitToMainMenu() end)\nprint("  exit issued")'))
        for _ in range(30):
            time.sleep(3)
            try:
                if "InGame" not in states_now():
                    break
            except RuntimeError:
                continue
        print("    now in the front end")

    print("\n[2] Firaxis' own new-game sequence (automation_standardtests.lua:277-309)")
    print(lua("HostGame", """
print("  SaveFileTypes.GAME_CONFIGURATION = " .. tostring(SaveFileTypes.GAME_CONFIGURATION))
local ok = pcall(function() GameConfiguration.SetToDefaults() end)
print("  GameConfiguration.SetToDefaults() ok = " .. tostring(ok))
"""))

    print("\n[2b] load the CivSim DEFAULT preset as a GAME_CONFIGURATION")
    print(lua("HostGame", """
local loadParams = {};
loadParams.Location   = SaveLocations.LOCAL_STORAGE;
loadParams.Type       = SaveTypes.SINGLE_PLAYER;
loadParams.FileType   = SaveFileTypes.GAME_CONFIGURATION;
loadParams.IsAutosave = false;
loadParams.IsQuicksave= false;
loadParams.Directory  = SaveDirectories.DEFAULT;
loadParams.Name       = "CivSim DEFAULT";
local ok, ret = pcall(function()
  return Network.LoadGame(loadParams, ServerType.SERVER_TYPE_NONE)
end)
print("  LoadGame(GAME_CONFIGURATION) ok=" .. tostring(ok) .. " ret=" .. tostring(ret))
print("  ruleset   = " .. tostring(GameConfiguration.GetRuleSet()))
print("  aiCount   = " .. tostring(GameConfiguration.GetAIPlayerCount()))
print("  mapScript = " .. tostring(MapConfiguration.GetScript()))
"""))

    print(f"\n[3] pin {LEADER} / {CIV} AFTER the config load, then read back at setup")
    print(lua("HostGame", f"""
local cfg = PlayerConfigurations[0];
local okL = pcall(function() cfg:SetLeaderTypeName("{LEADER}") end)
local okC = pcall(function() cfg:SetCivilizationTypeName("{CIV}") end)
print("  SetLeaderTypeName ok       = " .. tostring(okL))
print("  SetCivilizationTypeName ok = " .. tostring(okC))
print("  readback leader (setup)    = " .. tostring(cfg:GetLeaderTypeName()))
print("  readback civ    (setup)    = " .. tostring(cfg:GetCivilizationTypeName()))
"""))

    print("\n[3b] Network.HostGame(ServerType.SERVER_TYPE_NONE)")
    print(lua("HostGame", """
local ok, ret = pcall(function()
  return Network.HostGame(ServerType.SERVER_TYPE_NONE)
end)
print("  HostGame ok=" .. tostring(ok) .. " ret=" .. tostring(ret))
"""))

    print("\n[4] waiting for the game to come up (map generation + intro screen)")
    time.sleep(20)
    press_escape_until_port_returns()

    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            if "InGame" in states_now():
                break
        except RuntimeError:
            pass
        press_escape_until_port_returns(budget_s=30)
        time.sleep(5)
    else:
        print("  !! never reached InGame")
        return 1

    print("\n[5] THE QUESTION: read the leader back IN-GAME")
    print(lua("InGame", f"""
local pid = Game.GetLocalPlayer()
local cfg = PlayerConfigurations[pid]
local leader = tostring(cfg:GetLeaderTypeName())
local civ    = tostring(cfg:GetCivilizationTypeName())
print("  localPlayer        = " .. tostring(pid))
print("  leader (in-game)   = " .. leader)
print("  civ    (in-game)   = " .. civ)
print("  turn               = " .. tostring(Game.GetCurrentGameTurn()))
print("  PINNED LEADER SURVIVED = " .. tostring(leader == "{LEADER}"))
print("  PINNED CIV SURVIVED    = " .. tostring(civ == "{CIV}"))
"""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
