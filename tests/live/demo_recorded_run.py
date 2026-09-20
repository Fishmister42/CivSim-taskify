"""Record a harness-driven run using the harness's own capture path.

Deliberately NOT an OBS desktop capture. Every frame here comes from
`capture_window()` -- the same XComposite path a run uses to show an agent the
screen -- so the artifact is window-scoped by construction and demonstrates the
capture path rather than merely depicting it. A desktop recorder would show the
same pixels while proving nothing about our code.

The sequence, all driven from Lua except one keystroke:

  exit to front end -> apply CivSim DEFAULT from Lua -> assign a human slot
  -> pin Cyrus/Persia -> Network.HostGame -> Escape dismisses the intro
  -> observe -> end turn -> turn advances -> save

Frames are captured on a background thread throughout, then written as a GIF
plus a few full-resolution key frames.

    python3 -m tests.live.demo_recorded_run <out_dir>
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "specs/002-civ-playing-harness/spikes/r5-raw"))

from PIL import Image  # noqa: E402

from civsim_harness.host.detect import LinuxSessionType  # noqa: E402
from civsim_harness.host.linux.adapter import LinuxHostPlatform  # noqa: E402
from civsim_harness.host.port import (  # noqa: E402
    CaptureStatus, GameProcess, InputEvent, InputEventKind,
)
from nexus_probe import Probe  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-out")
LEADER, CIV, PRESET = "LEADER_CYRUS", "CIVILIZATION_PERSIA", "CivSim DEFAULT"
GIF_WIDTH = 720
FRAME_INTERVAL_S = 1.0

adapter = LinuxHostPlatform(session_type=LinuxSessionType.x11)
frames: list[tuple[float, Image.Image]] = []
captions: list[tuple[float, str]] = []
_stop = threading.Event()


def note(text: str) -> None:
    captions.append((time.time(), text))
    print(f"  [{time.strftime('%H:%M:%S')}] {text}", flush=True)


def window():
    out = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
    if not out:
        return None
    return adapter.find_game_window(GameProcess(pid=int(out[0]), name="Civ6"))


def recorder() -> None:
    """Capture through the production path; a closed tuner does not stop capture,
    which is the point -- capture is the one channel that stays open during a load."""
    while not _stop.is_set():
        w = window()
        if w is not None:
            r = adapter.capture_window(w)
            if r.status is CaptureStatus.ok and r.frame is not None:
                img = Image.frombytes("RGBA", (r.frame.width, r.frame.height), r.frame.image_bytes)
                b, g, rr, a = img.split()
                rgb = Image.merge("RGB", (rr, g, b))
                h = int(rgb.height * GIF_WIDTH / rgb.width)
                frames.append((time.time(), rgb.resize((GIF_WIDTH, h), Image.LANCZOS)))
        _stop.wait(FRAME_INTERVAL_S)


def tuner_up() -> bool:
    return b"4318" in subprocess.run(["ss", "-ltn"], capture_output=True).stdout


def connect(attempts: int = 10) -> Probe:
    last = None
    for _ in range(attempts):
        try:
            return Probe(app="civsim-demo")
        except OSError as exc:
            last = exc
            time.sleep(2.0)
    raise RuntimeError(f"tuner unreachable: {last}")


def lua(state: str, body: str, wait: float = 10.0) -> str:
    p = connect()
    try:
        st = p.handshake()
        if state not in st:
            return f"!! {state} absent"
        out, raw = p.exec_lua(st[state], body, wait=wait)
        return (out or f"(no sentinel) {raw[:200]}").replace("\x00", "")
    finally:
        p.close()


def states() -> dict[str, int]:
    p = connect()
    try:
        return p.handshake()
    finally:
        p.close()


def escape_until_port(budget_s: int = 240) -> int:
    sent = 0
    end = time.time() + budget_s
    while time.time() < end:
        if tuner_up():
            return sent
        w = window()
        if w is not None:
            subprocess.run(["xdotool", "windowactivate", str(w.handle)], check=False)
            time.sleep(0.5)
            adapter.send_input([InputEvent(kind=InputEventKind.key_press, key="Escape")])
            sent += 1
        time.sleep(5.0)
    return sent


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    t = threading.Thread(target=recorder, daemon=True)
    t.start()
    note("recording started (frames via capture_window(), window-scoped)")

    try:
        if "InGame" in states():
            note("leaving the current game: Events.ExitToMainMenu()")
            lua("InGame", "pcall(function() Events.ExitToMainMenu() end) print('ok')")
            for _ in range(40):
                time.sleep(3)
                try:
                    if "InGame" not in states():
                        break
                except RuntimeError:
                    pass
        for _ in range(20):
            if "HostGame" in states():
                break
            time.sleep(5)

        note(f"applying the '{PRESET}' preset from Lua -- no UI")
        print(lua("HostGame", f"""
GameConfiguration.SetToDefaults()
local lp = {{}}
lp.Location=SaveLocations.LOCAL_STORAGE; lp.Type=SaveTypes.SINGLE_PLAYER
lp.FileType=SaveFileTypes.GAME_CONFIGURATION; lp.IsAutosave=false
lp.IsQuicksave=false; lp.Directory=SaveDirectories.DEFAULT; lp.Name="{PRESET}"
print("  preset loaded = " .. tostring(Network.LoadGame(lp, ServerType.SERVER_TYPE_NONE)))
PlayerConfigurations[0]:SetSlotStatus(SlotStatus.SS_TAKEN)
PlayerConfigurations[0]:SetLeaderTypeName("{LEADER}")
PlayerConfigurations[0]:SetCivilizationTypeName("{CIV}")
print("  humans = " .. tostring(GameConfiguration.GetHumanPlayerCount()))
print("  leader = " .. tostring(PlayerConfigurations[0]:GetLeaderTypeName()))
print("  turnTimer NONE = " .. tostring(
  GameConfiguration.GetTurnTimerType() == DB.MakeHash("TURNTIMER_NONE")))
"""))

        note("starting the game: Network.HostGame(SERVER_TYPE_NONE)")
        lua("HostGame", "pcall(function() Network.HostGame(ServerType.SERVER_TYPE_NONE) end)\nprint('issued')")
        time.sleep(25)
        note("dismissing the leader-intro screen with send_input(Escape)")
        n = escape_until_port()
        note(f"intro dismissed after {n} Escape press(es)")

        for _ in range(60):
            try:
                if "InGame" in states():
                    break
            except RuntimeError:
                pass
            escape_until_port(budget_s=20)
            time.sleep(5)

        note("in game -- observing through the tuner")
        print(lua("InGame", """
print("  leader = " .. tostring(PlayerConfigurations[Game.GetLocalPlayer()]:GetLeaderTypeName()))
print("  turn   = " .. tostring(Game.GetCurrentGameTurn()))
"""))
        time.sleep(3)

        note("ending the turn")
        lua("InGame", "pcall(function() UI.RequestAction(ActionTypes.ACTION_ENDTURN) end)\nprint('issued')")
        time.sleep(10)
        note("verifying the turn advanced (far side, later command)")
        print(lua("InGame", 'print("  turn now = " .. tostring(Game.GetCurrentGameTurn()))'))
        time.sleep(3)
    finally:
        _stop.set()
        t.join(timeout=10)

    note(f"recording stopped: {len(frames)} frames")
    if not frames:
        print("!! no frames captured")
        return 1

    imgs = [f for _, f in frames]
    gif = OUT / "harness-driven-run.gif"
    imgs[0].save(gif, save_all=True, append_images=imgs[1:],
                 duration=int(FRAME_INTERVAL_S * 1000), loop=0, optimize=True)
    print(f"  wrote {gif} ({gif.stat().st_size // 1024} KB, {len(imgs)} frames)")

    t0 = frames[0][0]
    for i, idx in enumerate({0, len(frames) // 3, 2 * len(frames) // 3, len(frames) - 1}):
        ts, img = frames[idx]
        img.save(OUT / f"keyframe_{i}_t{int(ts - t0):03d}s.png")
    with (OUT / "timeline.txt").open("w") as fh:
        for ts, text in captions:
            fh.write(f"t+{int(ts - t0):03d}s  {text}\n")
    print(f"  wrote key frames + timeline.txt to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
