"""One window-scoped WGC frame, with the capture border explicitly OFF (T099).

**Window-scoped only, by construction.** `WindowsCapture(window_hwnd=...)`
targets one HWND. This module never constructs a monitor capture and never
touches `DxgiDuplicationSession`, which is screen-level -- a root grab
captures occluding windows and leaks content the agent must never see
(Principle I; the Linux peer's `root-scoped-same-region-LEAKS.png` is the
proof). There is deliberately no fallback here that widens the scope: if the
window capture fails, this raises, and the spike records a failure.

`draw_border=False` is this binding's spelling of
`GraphicsCaptureSession.IsBorderRequired = false`, which is the specific
Windows chrome requirement T099 names.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
from windows_capture import Frame, InternalCaptureControl, WindowsCapture


def shoot(hwnd: int, out_path: Path, *, timeout_s: float = 8.0) -> dict:
    """Capture one frame of `hwnd` to `out_path` (PNG). Returns a small fact dict."""
    result: dict = {}
    done = threading.Event()

    capture = WindowsCapture(
        cursor_capture=False,
        draw_border=False,  # == IsBorderRequired(false)
        window_hwnd=hwnd,
    )

    @capture.event
    def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl) -> None:
        if done.is_set():
            return
        # frame.frame_buffer is HxWx4 BGRA
        buf = np.array(frame.frame_buffer, copy=True)
        result["width"] = int(frame.width)
        result["height"] = int(frame.height)
        result["shape"] = tuple(int(x) for x in buf.shape)
        result["mean_luma"] = float(buf[:, :, :3].mean())
        frame.save_as_image(str(out_path))
        result["saved"] = str(out_path)
        done.set()
        capture_control.stop()

    @capture.event
    def on_closed() -> None:
        done.set()

    control = capture.start_free_threaded()
    deadline = time.monotonic() + timeout_s
    while not done.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        control.stop()
    except Exception:  # noqa: BLE001 - already stopped from the callback
        pass

    if not result:
        raise TimeoutError(f"no WGC frame for hwnd {hwnd} within {timeout_s}s")
    return result
