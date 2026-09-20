"""SaveLoader checklist item 4: a full load -> observe -> end turn -> persist cycle.

Run against an already-loaded client. Deliberately instrumented per-second around
the end-turn, because an earlier end-turn today was followed ~6s later by a clean
client exit whose cause was never established (see r7-live-client-session-linux.md,
"Trap 2"). Either that reproduces here -- which attributes it -- or it does not,
which is equally worth knowing. Nothing is inferred either way.

Order is deliberate: persist BEFORE the risky step, so a lost client still leaves
a restore point and still proves the save half.

    python3 -m tests.live.test_full_turn_cycle
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "specs/002-civ-playing-harness/spikes/r5-raw"))

from nexus_probe import Probe  # noqa: E402

SAVES = Path.home() / ".local/share/aspyr-media/Sid Meier's Civilization VI/Saves/Single"
SAVE_NAME = "civsim__cycle__probe"


def alive() -> bool:
    return bool(subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True).stdout.strip())


def connect_with_retry(attempts: int = 8) -> Probe:
    """The client accepts ONE tuner connection at a time and needs a beat between
    them -- a refused connection right after closing the previous socket is
    expected, not a dead client. Measured: refused immediately, fine ~2s later."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return Probe(app="civsim-cycle")
        except OSError as exc:
            last = exc
            time.sleep(2.0)
    raise RuntimeError(f"tuner unreachable after {attempts} attempts: {last}")


def lua(state: str, body: str, wait: float = 6.0) -> str:
    p = connect_with_retry()
    try:
        states = p.handshake()
        if state not in states:
            return f"!! state {state} absent (have: {sorted(states)[:6]}...)"
        out, raw = p.exec_lua(states[state], body, wait=wait)
        return (out or f"(no sentinel) RAW={raw[:400]}").replace("\x00", "")
    finally:
        p.close()


def main() -> int:
    print("=== [1] OBSERVE ===")
    print(lua("InGame", r"""
print("  turn        = " .. tostring(Game.GetCurrentGameTurn()))
print("  canEndTurn  = " .. tostring(UI.CanEndTurn()))
print("  localPlayer = " .. tostring(Game.GetLocalPlayer()))
local pid = Game.GetLocalPlayer()
print("  gold        = " .. tostring(Players[pid]:GetTreasury():GetGoldBalance()))
"""))

    print("\n=== [2] PERSIST (before the risky step) ===")
    before = set(p.name for p in SAVES.glob("*.Civ6Save"))
    print(lua("InGame", f"""
local gameFile = {{}};
gameFile.Name = "{SAVE_NAME}";
gameFile.Location = SaveLocations.LOCAL_STORAGE;
gameFile.Type = SaveTypes.SINGLE_PLAYER;
gameFile.IsAutosave = false;
gameFile.IsQuicksave = false;
gameFile.Directory = SaveDirectories.DEFAULT;
local ok, ret = pcall(function() return Network.SaveGame(gameFile) end)
print("  Network.SaveGame ok=" .. tostring(ok) .. " ret=" .. tostring(ret))
"""))
    time.sleep(6)
    after = set(p.name for p in SAVES.glob("*.Civ6Save"))
    new = sorted(after - before)
    print(f"  new files on disk: {new or 'NONE'}")
    if new:
        f = SAVES / new[0]
        print(f"  size={f.stat().st_size} bytes  -> persist half PASSES")

    print("\n=== [3] END TURN, instrumented per second ===")
    print("  issuing UI.RequestAction(ActionTypes.ACTION_ENDTURN)")
    print(lua("InGame", r"""
local ok, err = pcall(function() UI.RequestAction(ActionTypes.ACTION_ENDTURN) end)
print("  RequestAction ok=" .. tostring(ok) .. " err=" .. tostring(err))
"""))

    print("\n  second-by-second liveness after the end-turn:")
    died_at = None
    for s in range(1, 21):
        time.sleep(1)
        a = alive()
        port = b"4318" in subprocess.run(["ss", "-ltn"], capture_output=True).stdout
        print(f"    t+{s:2d}s  Civ6={'alive' if a else 'GONE'}  tuner={'up' if port else 'down'}")
        if not a:
            died_at = s
            break

    if died_at is not None:
        print(f"\n  ⚠️  client exited {died_at}s after the end-turn -- REPRODUCES the earlier event")
        return 1

    print("\n=== [4] VERIFY the turn advanced (far side, later command) ===")
    print(lua("InGame", r"""
print("  turn now   = " .. tostring(Game.GetCurrentGameTurn()))
print("  canEndTurn = " .. tostring(UI.CanEndTurn()))
"""))
    print("\n  client survived the end-turn; earlier exit did NOT reproduce")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
