"""Dismiss the client's intro screen so the front-end Lua states exist.

Civilization VI opens on a logo/attract screen and does not reach `MainMenu`
until it receives input. Until it does, the tuner's state table holds only
`Main State` and `DebugHotloadCache`, so there is nothing to drive.

Uses the harness's **own** `WindowsHostPlatform.send_input` rather than calling
pydirectinput directly -- driving the client through the adapter is part of
what is being demonstrated, and it exercises the T050 input path against a real
window for the first time.

Escape only. No clicks: a stray click on the main menu could start or join
something, and dismissing an attract screen does not need one.
"""

from __future__ import annotations

import json
import sys
import time

import win32con
import win32gui

from civsim_harness.host.port import InputEvent, InputEventKind
from civsim_harness.host.windows.adapter import WindowsHostPlatform


def main() -> int:
    host = WindowsHostPlatform()
    proc = host.locate_game_process()
    if proc is None:
        print("no client process")
        return 1
    window = host.find_game_window(proc)
    if window is None:
        print("no client window")
        return 1
    print(f"window hwnd={window.handle} rect={window.rect}")

    win32gui.ShowWindow(window.handle, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(window.handle)
    time.sleep(1.0)
    print("foreground:", win32gui.GetForegroundWindow() == window.handle)

    for i in range(6):
        result = host.send_input([InputEvent(kind=InputEventKind.key_press, key="escape")])
        print(f"  escape {i}: {result.status.value}" + (f" ({result.reason})" if result.reason else ""))
        time.sleep(1.5)

    time.sleep(3.0)
    print(json.dumps({"sent": "escape x6"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
