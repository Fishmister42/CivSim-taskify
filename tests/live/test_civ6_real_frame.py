"""T052 acceptance: the first REAL Civilization VI frame through `capture_window()`.

Every prior capture result on this host was measured against throwaway X11
windows. "Identical in principle" is not measured, so this drives the whole
production chain against the live client:

    find_game_window(process) -> capture_preconditions() -> capture_window(window)

and writes the pixels out as a PNG so the frame can be looked at, not just
counted. Run it with Civ VI up and in-game.

    python3 -m tests.live.test_civ6_real_frame [out.png]
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from civsim_harness.host.detect import LinuxSessionType  # noqa: E402
from civsim_harness.host.linux.adapter import LinuxHostPlatform  # noqa: E402
from civsim_harness.host.port import CaptureStatus, GameProcess  # noqa: E402

CIV_PROCESS = "Civ6"


def find_civ_pid() -> int | None:
    """Select by EXACT process name, not by cmdline.

    Under Steam's pressure-vessel runtime several processes carry the game's
    path in their cmdline -- the launch wrapper, `reaper`, `pv-adverb` -- and
    `pgrep -f` returns the wrapper FIRST. Only the real `Civ6` process owns the
    window, so a cmdline match hands `find_game_window` a pid that owns
    nothing and it correctly returns None. Measured 2026-09-20: `pgrep -f`
    gave 841213 (reaper); the window's `_NET_WM_PID` was 841328.
    """
    out = subprocess.run(
        ["pgrep", "-x", CIV_PROCESS], capture_output=True, text=True
    ).stdout.split()
    return int(out[0]) if out else None


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("civ6_real_frame.png")
    adapter = LinuxHostPlatform(session_type=LinuxSessionType.x11)

    pid = find_civ_pid()
    print(f"[1] Civ VI process: pid={pid}")
    if pid is None:
        print("    FAIL: client not running")
        return 1
    process = GameProcess(pid=pid, name=CIV_PROCESS)

    # The method the reachability audit found has no production caller anywhere.
    window = adapter.find_game_window(process)
    print(f"[2] find_game_window() -> {window}")
    if window is None:
        print("    FAIL: no window resolved for the live client")
        return 1

    pre = adapter.capture_preconditions()
    print(f"[3] capture_preconditions() -> {pre}")

    timings = []
    result = None
    for attempt in range(3):
        t0 = time.perf_counter()
        result = adapter.capture_window(window)
        dt = (time.perf_counter() - t0) * 1000
        timings.append(dt)
        print(f"[4.{attempt}] capture_window() -> status={result.status.name} "
              f"reason={result.reason} in {dt:.1f} ms")
        if result.status is not CaptureStatus.ok:
            return 1

    frame = result.frame
    assert frame is not None
    print(f"[5] frame: {frame.width}x{frame.height} format={frame.image_format} "
          f"bytes={len(frame.image_bytes)} rect={frame.rect}")
    expected = frame.width * frame.height * 4
    print(f"    expected BGRA8 bytes={expected} -> {'MATCH' if expected == len(frame.image_bytes) else 'MISMATCH'}")

    # Non-uniform pixels are the real evidence: a black/blank frame would also
    # have the right byte count.
    sample = frame.image_bytes
    distinct = len({sample[i:i + 4] for i in range(0, min(len(sample), 4_000_000), 4004)})
    print(f"[6] distinct sampled pixels: {distinct} "
          f"({'real content' if distinct > 50 else 'SUSPECT - looks blank'})")

    try:
        from PIL import Image
        img = Image.frombytes("RGBA", (frame.width, frame.height), frame.image_bytes)
        b, g, r, a = img.split()
        Image.merge("RGB", (r, g, b)).save(out_path)
        print(f"[7] wrote {out_path}")
    except ImportError:
        print("[7] Pillow absent; skipping PNG write")

    print(f"\ncapture timings (ms): {', '.join(f'{t:.1f}' for t in timings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
