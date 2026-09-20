"""Produce R6 evidence frames through the harness's OWN capture path.

Every frame written here comes from `LinuxHostPlatform.capture_window()` --
not from `import`, not from a root grab. The point is to show that the
window-scoped XComposite readback returns each window's own contents even
when the windows overlap on screen.

Deliberately no root/screen grab anywhere, including to illustrate the
overlap: a root grab is the FR-025 parity breach this path exists to avoid
(`r6-evidence/root-scoped-same-region-LEAKS.png`), and it would also drag
the operator's unrelated windows into a committed artefact. The overlap is
evidenced by the reported geometry and stacking order instead.

    python specs/002-civ-playing-harness/spikes/capture_evidence.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

from civsim_harness.host.detect import LinuxSessionType
from civsim_harness.host.linux.adapter import LinuxHostPlatform
from civsim_harness.host.port import CaptureStatus, GameWindow, WindowRect

_OUT = Path(__file__).parent / "r6-evidence"

_TARGET = ("#3366cc", 760, 520, "+120+120")
_OCCLUDER = ("#cc3344", 420, 260, "+300+280")


def _spawn(bg: str, width: int, height: int, offset: str, analog: bool) -> subprocess.Popen[bytes]:
    argv = ["xclock", "-bg", bg, "-fg", "#ffffff", "-geometry", f"{width}x{height}{offset}"]
    if not analog:
        argv.insert(1, "-digital")
    return subprocess.Popen(
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )


def _window_for(pid: int, timeout_s: float = 10.0) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = subprocess.run(
            ["xdotool", "search", "--pid", str(pid)], capture_output=True, text=True, check=False
        )
        ids = found.stdout.split()
        if ids:
            return int(ids[-1])
        time.sleep(0.2)
    raise RuntimeError(f"no window ever appeared for pid {pid}")


def _geometry(window_id: int) -> tuple[int, int, int, int]:
    shell = subprocess.run(
        ["xdotool", "getwindowgeometry", "--shell", str(window_id)],
        capture_output=True,
        text=True,
        check=True,
    )
    values = dict(line.split("=", 1) for line in shell.stdout.splitlines() if "=" in line)
    return (int(values["X"]), int(values["Y"]), int(values["WIDTH"]), int(values["HEIGHT"]))


def _capture(adapter: LinuxHostPlatform, window_id: int, pid: int, name: str) -> Path:
    x, y, width, height = _geometry(window_id)
    window = GameWindow(
        handle=window_id,
        title=name,
        rect=WindowRect(left=x, top=y, width=width, height=height),
        pid=pid,
    )
    result = adapter.capture_window(window)
    if result.status is not CaptureStatus.ok or result.frame is None:
        raise RuntimeError(f"capture of {name} failed: {result.status} {result.reason}")

    frame = result.frame
    image = Image.frombytes(
        "RGBA", (frame.width, frame.height), frame.image_bytes, "raw", "BGRA"
    ).convert("RGB")
    path = _OUT / f"harness-xcomposite-{name}.png"
    image.save(path)
    print(f"  {name:<9} rect=({x},{y},{width}x{height}) format={frame.image_format} -> {path.name}")
    return path


def main() -> int:
    _OUT.mkdir(parents=True, exist_ok=True)
    adapter = LinuxHostPlatform(session_type=LinuxSessionType.x11)

    pre = adapter.capture_preconditions()
    print("preconditions:")
    print(f"  Composite extension  : {pre.composite_extension}")
    print(f"  compositing manager  : {pre.compositing_manager}")
    print(f"  unredirect-fullscreen: {pre.unredirect_fullscreen_windows}")
    print(f"  can_capture          : {pre.can_capture}")
    for warning in pre.warnings:
        print(f"  WARNING: {warning}")
    if not pre.can_capture:
        print("refusing to produce evidence on a host that cannot capture")
        return 1

    target = _spawn(*_TARGET, analog=True)
    occluder = _spawn(*_OCCLUDER, analog=False)
    try:
        target_id = _window_for(target.pid)
        occluder_id = _window_for(occluder.pid)

        # Put the occluder unambiguously on top of the target.
        subprocess.run(["xdotool", "windowraise", str(occluder_id)], check=False)
        time.sleep(1.0)

        tx, ty, tw, th = _geometry(target_id)
        ox, oy, ow, oh = _geometry(occluder_id)
        overlap_w = max(0, min(tx + tw, ox + ow) - max(tx, ox))
        overlap_h = max(0, min(ty + th, oy + oh) - max(ty, oy))
        print("\ngeometry:")
        print(f"  target   0x{target_id:08x} ({tx},{ty}) {tw}x{th}")
        print(f"  occluder 0x{occluder_id:08x} ({ox},{oy}) {ow}x{oh}  [raised above target]")
        print(f"  overlap  {overlap_w}x{overlap_h} px of the target is covered on screen")

        print("\nframes captured through LinuxHostPlatform.capture_window():")
        _capture(adapter, target_id, target.pid, "target")
        _capture(adapter, occluder_id, occluder.pid, "occluder")
    finally:
        for proc in (target, occluder):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
