"""Bring the client from cold to in-game, unattended.

Repeatable because the client on this host is not durable: Steam restarts
every few minutes (`LogonFailure No Connection`) and takes the tracked game
process with it, so any live spike has to be able to re-establish the world.

Sequence, all of it through the harness's own adapters where one exists:
  1. launch `CivilizationVI.exe` (DX11; DX12 exits immediately on this host)
  2. wait for the tuner port
  3. `send_input(escape)` to dismiss the attract screen -- the front-end Lua
     states do not exist until the client leaves it
  4. `Network.LoadGame` on the newest autosave, from the `MainMenu` context
  5. `send_input(click)` on the leader-intro "Continue Game" button
  6. wait for `GameCore_Tuner` + `InGame` to appear in the state table
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

import win32gui

sys.path.insert(0, str(Path(__file__).resolve().parent))
from r5_save_spike import LiveNexusClient, say  # noqa: E402

from civsim_harness.host.port import InputEvent, InputEventKind  # noqa: E402
from civsim_harness.host.windows.adapter import WindowsHostPlatform  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402

GAME_DIR = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI"
    r"\Base\Binaries\Win64Steam"
)


def tuner_up() -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", 4318)) == 0


async def bringup(*, load_save: bool = True) -> bool:
    host = WindowsHostPlatform()

    if host.locate_game_process() is None:
        say("launching client")
        subprocess.Popen([str(GAME_DIR / "CivilizationVI.exe")], cwd=str(GAME_DIR))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not tuner_up():
            time.sleep(0.5)
        if not tuner_up():
            say("tuner never came up")
            return False
        say("tuner up")
        time.sleep(4)

    proc = host.locate_game_process()
    window = host.find_game_window(proc) if proc else None
    if window is None:
        say("no window")
        return False
    try:
        win32gui.SetForegroundWindow(window.handle)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1)

    client = LiveNexusClient(app_name="civsim_harness", connect_timeout_s=8.0)
    for _ in range(10):
        try:
            await client.connect()
            break
        except Exception as exc:  # noqa: BLE001
            say(f"  connect retry: {type(exc).__name__}")
            await asyncio.sleep(2)
    else:
        return False

    idx = client.state_indices.by_name
    say(f"connected: {len(idx)} states")

    if "InGame" in idx:
        say("already in game")
        await client.close()
        return True

    # Dismiss the attract screen until the front end appears. The client needs
    # ~30-60s after the tuner opens before it is even showing the attract
    # screen, and input only lands while its window has focus -- refocusing
    # every round matters, because launching and OBS both steal it.
    for attempt in range(40):
        if "MainMenu" in client.state_indices.by_name:
            break
        try:
            win32gui.SetForegroundWindow(window.handle)
        except Exception:  # noqa: BLE001
            pass
        for key in ("escape", "space", "return"):
            host.send_input([InputEvent(kind=InputEventKind.key_press, key=key)])
            await asyncio.sleep(0.3)
        await asyncio.sleep(2.5)
        try:
            await client.refresh_state_indices()
        except Exception as exc:  # noqa: BLE001
            say(f"  refresh: {type(exc).__name__}")
        if attempt % 4 == 0:
            say(f"  attract-dismiss {attempt}: {len(client.state_indices.by_name)} states")
    if "MainMenu" not in client.state_indices.by_name:
        say("front end never appeared")
        await client.close()
        return False
    say("front end reached")

    if not load_save:
        await client.close()
        return True

    dirs = host.resolve_game_directories()
    autos = sorted((dirs.saves_dir / "auto").glob("*.Civ6Save"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not autos:
        say("no autosave to load")
        await client.close()
        return False
    stem = autos[0].stem
    say(f"loading {stem}")
    body = LUA_JSON_PRELUDE + (
        "local f = {}; "
        f'f.Name = "{stem}"; '
        "f.Location = SaveLocations.LOCAL_STORAGE; "
        "f.Type = SaveTypes.SINGLE_PLAYER; f.GameType = SaveTypes.SINGLE_PLAYER; "
        "f.IsAutosave = true; f.IsQuicksave = false; "
        "local ok, err = pcall(function() "
        "return Network.LoadGame(f, ServerType.SERVER_TYPE_NONE) end); "
    ) + lua_print_json({"ok": "tostring(ok)", "err": "tostring(err)"})
    try:
        res = await client.execute_command(
            state_index=client.state_indices.by_name["MainMenu"], lua_body=body, timeout_s=30
        )
        say(f"LoadGame -> {res}")
    except Exception as exc:  # noqa: BLE001
        say(f"LoadGame call: {type(exc).__name__}: {exc}")
    await client.close()

    # The leader-intro modal needs a click before InGame appears.
    for attempt in range(30):
        await asyncio.sleep(5)
        probe = LiveNexusClient(app_name="civsim_harness", connect_timeout_s=6.0)
        try:
            await probe.connect()
            names = probe.state_indices.by_name
            if "InGame" in names:
                say(f"IN GAME ({len(names)} states)")
                await probe.close()
                return True
            await probe.close()
        except Exception:  # noqa: BLE001
            pass
        if attempt in (2, 5, 8, 12, 16):
            try:
                win32gui.SetForegroundWindow(window.handle)
            except Exception:  # noqa: BLE001
                pass
            host.send_input(
                [InputEvent(kind=InputEventKind.mouse_click, x=1057, y=1077, button="left")]
            )
            say(f"  clicked Continue Game (attempt {attempt})")
    say("never reached InGame")
    return False


if __name__ == "__main__":
    ok = asyncio.run(bringup())
    print("BRINGUP", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)
