"""R5 + R6 Windows live spike, run inside the client's short lifetime window.

The Civilization VI client on this host stays up for roughly 30 seconds before
Steam restarts and takes it down with it (see the spike write-ups). Rather than
fight that, this script does everything in one process, starting the moment the
tuner port opens:

  1. launch the client
  2. poll for 127.0.0.1:4318
  3. connect with the harness's own `NexusClient`, LSQ, and execute real Lua
  4. resolve the game window through the harness's own `WindowsHostPlatform`
  5. raise obvious occluding windows over the client
  6. capture the client **window-scoped** with the WGC border off
  7. crop to each occluder's rectangle and test for its colour

Every artefact lands under the spike directory. Nothing here takes a root or
full-screen capture, and there is no fallback that would (Principle I).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE
sys.path.insert(0, str(HERE))

import wgc_shot  # noqa: E402

from civsim_harness.host.windows.adapter import WindowsHostPlatform  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402

GAME_DIR = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI"
    r"\Base\Binaries\Win64Steam"
)
GAME_EXE = GAME_DIR / "CivilizationVI.exe"

# Two occluders, each a flat, unmistakable colour with legible text. Colour is
# what the assertion actually keys on: text can be antialiased away by a resize,
# a solid fill cannot be mistaken for Civ VI's art.
OCCLUDERS = [
    ("HARNESS CONSOLE - THIS TEXT MUST NOT REACH THE AGENT", "#FF00FF", (300, 300, 900, 260)),
    ("UNRELATED WINDOW - ALSO MUST NOT APPEAR", "#00FF00", (300, 640, 900, 240)),
]

log: list[str] = []


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    log.append(line)


def port_open(host: str = "127.0.0.1", port: int = 4318) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def spawn_occluders() -> threading.Thread:
    """Bright always-on-top windows, in their own thread so tkinter keeps pumping."""

    def run() -> None:
        root = tk.Tk()
        root.withdraw()
        for text, colour, (x, y, w, h) in OCCLUDERS:
            top = tk.Toplevel(root)
            top.overrideredirect(True)
            top.geometry(f"{w}x{h}+{x}+{y}")
            top.configure(bg=colour)
            top.attributes("-topmost", True)
            tk.Label(
                top, text=text, bg=colour, fg="#000000",
                font=("Consolas", 16, "bold"), wraplength=w - 40,
            ).pack(expand=True)
        root.mainloop()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(1.2)
    return t


async def nexus_probe() -> dict:
    """R5 step: connect through the harness's own transport and run real Lua."""
    facts: dict = {}
    client = NexusClient(app_name="civsim_harness", connect_timeout_s=4.0)
    # The tuner's listening socket opens several seconds before the client is
    # ready to speak the protocol: connecting the instant the port answers gets
    # the connection reset. Retry rather than record a false "tuner unreachable".
    indices = None
    attempts: list[str] = []
    for attempt in range(12):
        try:
            indices = await client.connect()
            facts["connect_attempts"] = attempt + 1
            break
        except Exception as exc:  # noqa: BLE001
            attempts.append(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(1.0)
    if indices is None:
        facts["connected"] = False
        facts["connect_failures"] = attempts
        say(f"NEXUS could not connect after {len(attempts)} attempts; last={attempts[-1]}")
        return facts
    facts["connected"] = True
    facts["state_count"] = len(indices.by_name)
    facts["has_game_states"] = indices.has_game_states
    facts["states"] = dict(sorted(indices.by_name.items(), key=lambda kv: kv[1]))
    say(f"NEXUS connected; {len(indices.by_name)} Lua states; "
        f"has_game_states={indices.has_game_states}")

    target_name = "InGame" if indices.in_game is not None else next(iter(indices.by_name), None)
    if target_name is not None:
        idx = indices.by_name[target_name]
        body = LUA_JSON_PRELUDE + lua_print_json(
            {"echo": '"civsim-windows-live"', "lua_version": "_VERSION"}
        )
        try:
            res = await client.execute_command(state_index=idx, lua_body=body, timeout_s=8)
            facts["roundtrip_state"] = target_name
            facts["roundtrip_result"] = res
            say(f"NEXUS round-trip in '{target_name}' -> {json.dumps(res)}")
        except Exception as exc:  # noqa: BLE001
            facts["roundtrip_error"] = f"{type(exc).__name__}: {exc}"
            say(f"NEXUS round-trip FAILED: {facts['roundtrip_error']}")

    # R5's actual question: is there a Lua save path, and does it write a file?
    if indices.in_game is not None:
        facts.update(await try_save(client, indices.in_game))
    else:
        facts["save_attempted"] = False
        facts["save_skipped_reason"] = (
            "InGame Lua state does not exist -- the client never got past its intro/main "
            "menu inside its ~30s lifetime, and Network.SaveGame lives only in InGame"
        )
        say("SAVE skipped: no InGame state (client never reached a loaded game)")
    await client.close()
    return facts


async def try_save(client: NexusClient, in_game_index: int) -> dict:
    host = WindowsHostPlatform()
    saves_dir = host.resolve_game_directories().saves_dir
    name = f"civsim__winspike__t{int(time.time()) % 10000:04d}"
    body = LUA_JSON_PRELUDE + (
        "local gameFile = {}; "
        f'gameFile.Name = "{name}"; '
        "gameFile.Location = SaveLocations.LOCAL_STORAGE; "
        "gameFile.Type = SaveTypes.SINGLE_PLAYER; "
        "gameFile.IsAutosave = false; gameFile.IsQuicksave = false; "
        "local ok, err = pcall(function() return Network.SaveGame(gameFile) end); "
    ) + lua_print_json({"issued": "ok", "error": "tostring(err)", "turn": "Game.GetCurrentGameTurn()"})
    res = await client.execute_command(state_index=in_game_index, lua_body=body, timeout_s=20)
    say(f"SAVE issued -> {json.dumps(res)}")
    # Assert on what the FAR side produced: the file on disk, size-stable.
    target = saves_dir / f"{name}.Civ6Save"
    seen = None
    for _ in range(40):
        if target.exists():
            a = target.stat().st_size
            time.sleep(0.4)
            b = target.stat().st_size
            if a == b and a > 0:
                seen = a
                break
        time.sleep(0.3)
    say(f"SAVE file {target} exists={target.exists()} stable_size={seen}")
    return {
        "save_attempted": True,
        "save_lua_result": res,
        "save_path": str(target),
        "save_file_exists": target.exists(),
        "save_stable_size": seen,
    }


def zorder_snapshot(game_hwnd: int) -> dict:
    """Prove the occluders are stacked **above** the client, not merely present.

    The Linux peer used `wmctrl` for exactly this, and it is the difference
    between "the dialog is absent because the capture excluded it" and "the
    dialog is absent because it was behind the window all along". Without this,
    a pass is indistinguishable from the test never having been run.
    """
    import win32gui

    order: list[tuple[int, str]] = []
    hwnd = win32gui.GetWindow(win32gui.GetDesktopWindow(), 5)  # GW_CHILD
    while hwnd:
        if win32gui.IsWindowVisible(hwnd):
            order.append((hwnd, win32gui.GetWindowText(hwnd)))
        hwnd = win32gui.GetWindow(hwnd, 2)  # GW_HWNDNEXT == further back
    handles = [h for h, _ in order]
    game_pos = handles.index(game_hwnd) if game_hwnd in handles else None

    occ = []
    for h, title in order:
        cls = win32gui.GetClassName(h)
        if cls == "TkTopLevel" or title == "tk":
            rect = win32gui.GetWindowRect(h)
            pos = handles.index(h)
            occ.append({"hwnd": h, "zpos": pos, "rect": rect,
                        "above_game": game_pos is not None and pos < game_pos})
    say(f"ZORDER game hwnd={game_hwnd} zpos={game_pos}; occluders={occ}")
    return {"game_zpos": game_pos, "occluders": occ,
            "all_occluders_above_game": bool(occ) and all(o["above_game"] for o in occ)}


def capture_phase() -> dict:
    host = WindowsHostPlatform()
    proc = host.locate_game_process()
    if proc is None:
        say("CAPTURE: no game process")
        return {"captured": False, "reason": "no game process"}
    window = host.find_game_window(proc)
    if window is None:
        say("CAPTURE: no game window")
        return {"captured": False, "reason": "no game window"}
    say(f"CAPTURE window hwnd={window.handle} rect={window.rect}")

    zorder = zorder_snapshot(window.handle)

    # The client animates a logo intro from black. Capturing the first frame
    # available yields a near-black image, against which "the occluder is
    # absent" is true but says nothing. Poll for a frame with real content.
    shot = OUT / "civ-window-scoped-with-occluders.png"
    facts = {}
    best = -1.0
    for attempt in range(14):
        f = wgc_shot.shoot(window.handle, OUT / "_probe.png", timeout_s=4.0)
        if f["mean_luma"] > best:
            best = f["mean_luma"]
            (OUT / "_probe.png").replace(shot)
            facts = f
            facts["saved"] = str(shot)
        say(f"CAPTURE attempt {attempt} mean_luma={f['mean_luma']:.2f} (best {best:.2f})")
        if best > 8.0:
            break
        time.sleep(0.8)
    facts["zorder"] = zorder
    say(f"CAPTURE ok {facts}")

    from PIL import Image

    img = Image.open(shot).convert("RGB")
    checks = []
    for text, colour, (x, y, w, h) in OCCLUDERS:
        rx, ry = x - window.rect.left, y - window.rect.top
        crop = img.crop((rx, ry, rx + w, ry + h))
        crop.save(OUT / f"crop-{colour.lstrip('#')}.png")
        want = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))
        px = list(crop.getdata())
        hits = sum(
            1 for p in px
            if abs(p[0] - want[0]) < 30 and abs(p[1] - want[1]) < 30 and abs(p[2] - want[2]) < 30
        )
        frac = hits / max(1, len(px))
        checks.append(
            {"occluder": text, "colour": colour, "rect": [rx, ry, w, h],
             "matching_pixel_fraction": round(frac, 6),
             "leaked": frac > 0.01}
        )
        say(f"OCCLUSION {colour}: matching-pixel fraction {frac:.6f} "
            f"-> {'LEAKED' if frac > 0.01 else 'absent (good)'}")
    return {"captured": True, "frame": facts, "window": asdict(window.rect), "checks": checks}


def main() -> int:
    results: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}
    say("spawning occluding windows (always-on-top)")
    spawn_occluders()

    say(f"launching {GAME_EXE.name}")
    subprocess.Popen([str(GAME_EXE)], cwd=str(GAME_DIR))

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not port_open():
        time.sleep(0.5)
    if not port_open():
        say("tuner port never opened")
        results["tuner"] = {"opened": False}
        (OUT / "live_spike_results.json").write_text(json.dumps(results, indent=2))
        return 1
    say("tuner port 4318 OPEN")
    results["tuner"] = {"opened": True}

    try:
        results["nexus"] = asyncio.run(nexus_probe())
    except Exception as exc:  # noqa: BLE001
        results["nexus"] = {"error": f"{type(exc).__name__}: {exc}"}
        say(f"NEXUS phase FAILED: {results['nexus']['error']}")

    try:
        results["capture"] = capture_phase()
    except Exception as exc:  # noqa: BLE001
        results["capture"] = {"error": f"{type(exc).__name__}: {exc}"}
        say(f"CAPTURE phase FAILED: {results['capture']['error']}")

    results["log"] = log
    (OUT / "live_spike_results.json").write_text(json.dumps(results, indent=2, default=str))
    say("wrote live_spike_results.json")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
