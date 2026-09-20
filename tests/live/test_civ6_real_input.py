"""The first synthetic event into the real Civilization VI client.

`send_input` has been driven only against `Xephyr`/`xev`. Whether Civ VI itself
accepts XTest events was inference. This measures it, and asserts on the far
side -- the client's own pixels -- not on the `InputStatus` the call returned.

Safety: XTest has no window targeting, so events go to whatever holds focus.
This refuses to send unless the Civ VI window is focused at the moment of the
call, so a stray keystroke cannot land in the owner's browser.

    python3 -m tests.live.test_civ6_real_input
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from civsim_harness.host.detect import LinuxSessionType  # noqa: E402
from civsim_harness.host.linux.adapter import LinuxHostPlatform  # noqa: E402
from civsim_harness.host.port import (  # noqa: E402
    CaptureStatus, GameProcess, InputEvent, InputEventKind,
)

CIV_PROCESS = "Civ6"


def civ_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", CIV_PROCESS],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def focused_window_pid() -> int | None:
    """The pid owning the currently focused window, via the WM, not our adapter."""
    wid = subprocess.run(["xdotool", "getwindowfocus"],
                         capture_output=True, text=True).stdout.strip()
    if not wid:
        return None
    out = subprocess.run(["xdotool", "getwindowpid", wid],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else None


def frame_signature(adapter, window) -> tuple[int, bytes] | None:
    result = adapter.capture_window(window)
    if result.status is not CaptureStatus.ok or result.frame is None:
        print(f"    capture failed: {result.status.name} {result.reason}")
        return None
    data = result.frame.image_bytes
    # Cheap content signature: sampled bytes, so a UI change is visible without
    # holding two 9 MB buffers side by side.
    return len(data), bytes(data[i] for i in range(0, len(data), 40009))


def main() -> int:
    adapter = LinuxHostPlatform(session_type=LinuxSessionType.x11)
    pid = civ_pid()
    if pid is None:
        print("FAIL: Civ VI not running")
        return 1
    window = adapter.find_game_window(GameProcess(pid=pid, name=CIV_PROCESS))
    if window is None:
        print("FAIL: no window")
        return 1
    print(f"[1] window handle={window.handle} pid={window.pid} rect={window.rect}")

    # --- focus guard -------------------------------------------------
    subprocess.run(["xdotool", "windowactivate", str(window.handle)], check=False)
    time.sleep(1.0)
    focused = focused_window_pid()
    print(f"[2] focused window pid={focused} (Civ pid={pid})")
    if focused != pid:
        print("    REFUSING to send: Civ VI is not focused; XTest would hit "
              "another window. Nothing was sent.")
        return 2

    before = frame_signature(adapter, window)
    if before is None:
        return 1
    print(f"[3] before: {before[0]} bytes, sig={before[1][:16].hex()}")

    # Escape opens the in-game menu -- a large, unmistakable visual change,
    # and entirely reversible.
    events = [InputEvent(kind=InputEventKind.key_press, key="Escape")]
    result = adapter.send_input(events)
    print(f"[4] send_input(Escape) -> status={result.status.name} reason={result.reason}")
    time.sleep(1.5)

    after = frame_signature(adapter, window)
    if after is None:
        return 1
    print(f"[5] after:  {after[0]} bytes, sig={after[1][:16].hex()}")

    changed = before[1] != after[1]
    print(f"[6] far-side verdict: pixels {'CHANGED' if changed else 'IDENTICAL'} "
          f"-> Civ VI {'ACCEPTED' if changed else 'DID NOT VISIBLY ACCEPT'} the synthetic key")

    # Put the client back the way we found it.
    adapter.send_input([InputEvent(kind=InputEventKind.key_press, key="Escape")])
    time.sleep(1.5)
    restored = frame_signature(adapter, window)
    if restored is not None:
        print(f"[7] after restore: sig={restored[1][:16].hex()} "
              f"({'back to start' if restored[1] == before[1] else 'differs from start'})")
    return 0 if changed else 1


if __name__ == "__main__":
    raise SystemExit(main())
