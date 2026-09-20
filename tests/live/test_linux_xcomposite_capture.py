"""Live XComposite capture tests for the Linux host adapter (T052, T099, research R6).

These drive the **real** `LinuxHostPlatform.capture_window` against a **real**
X11 window on a **real** compositing desktop. They deliberately do not target
Civilization VI: the XComposite path is window-agnostic, so a throwaway window
of a known colour proves the pixel pipeline without needing a game client, and
without capturing anything of the operator's own screen.

What a green run here actually establishes:

* `NameWindowPixmap` + `GetImage` produce correctly-ordered pixels, not merely
  a frame of the right size. A silent red/blue swap would satisfy every
  dimension assertion while corrupting every image the agent is ever shown, so
  the colour assertions are the point of this module.
* The emitted `image_format` is one the parity screening gates can decode.

Run explicitly -- excluded from the default suite (`addopts = --ignore=tests/live`):

    pytest tests/live/test_linux_xcomposite_capture.py -m live
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest

from civsim_harness.host.detect import LinuxSessionType
from civsim_harness.host.linux.adapter import LinuxHostPlatform
from civsim_harness.host.port import CaptureStatus, GameWindow, WindowRect

pytestmark = pytest.mark.live

# A saturated, asymmetric colour: every channel is distinct, so any channel
# permutation (BGR read as RGB, or a rotation) changes the observed value.
# A grey or a symmetric colour would pass a swapped decode and prove nothing.
_BG_HEX = "#3366cc"
_BG_RGB = (0x33, 0x66, 0xCC)

_WIDTH, _HEIGHT = 480, 320


def _require(condition: bool, reason: str) -> None:
    if not condition:
        pytest.skip(reason)


@pytest.fixture(scope="module")
def x11_display() -> str:
    _require(bool(os.environ.get("DISPLAY")), "no DISPLAY; these tests need a live X11 server")
    _require(shutil.which("xclock") is not None, "xclock is not installed (x11-apps)")
    return os.environ["DISPLAY"]


@pytest.fixture(scope="module")
def compositing(x11_display: str) -> None:
    """Skip unless a compositing manager is actually running.

    Detected by selection ownership of `_NET_WM_CM_Sn` -- the same condition
    the adapter checks, and deliberately NOT `XDG_SESSION_TYPE`, because an
    X11 session with no compositor maintains no off-screen pixmap and the
    capture is then correctly expected to report `unavailable`.

    Asked through the adapter's own `capture_preconditions()` rather than by
    importing `Xlib` here: a repo lint rule (TID251, research R19) confines
    OS-specific imports to `src/civsim_harness/host/`, and this keeps the
    skip condition and the capture path reading the same answer.
    """
    preconditions = _adapter().capture_preconditions()
    _require(
        preconditions.composite_extension,
        "X server has no Composite extension on this display",
    )
    _require(
        preconditions.compositing_manager,
        "no compositing manager owns _NET_WM_CM_Sn; window-scoped capture is "
        "correctly unavailable here",
    )


@pytest.fixture
def known_colour_window(x11_display: str, compositing: None) -> Iterator[GameWindow]:
    """Map a throwaway window of a known colour and yield it as a `GameWindow`."""
    proc = subprocess.Popen(
        [
            "xclock",
            "-digital",
            "-bg",
            _BG_HEX,
            "-geometry",
            f"{_WIDTH}x{_HEIGHT}+80+80",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        window_id = _await_window(proc.pid, timeout_s=10.0)
        _require(window_id is not None, "the throwaway window never appeared")
        assert window_id is not None

        width, height = _window_size(window_id)
        rect = WindowRect(left=0, top=0, width=width, height=height)

        yield GameWindow(handle=window_id, title="xclock", rect=rect, pid=proc.pid)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()


def _await_window(pid: int, *, timeout_s: float) -> int | None:
    """Poll `xdotool` for a top-level window owned by `pid`.

    Polls rather than sleeping a fixed interval: the window is not mapped
    synchronously with the process start, and a window that is merely
    *created* does not yet have the off-screen pixmap this test reads.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = subprocess.run(
            ["xdotool", "search", "--pid", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        ids = [line for line in found.stdout.split() if line.strip()]
        if ids:
            return int(ids[-1])
        time.sleep(0.2)
    return None


def _window_size(window_id: int) -> tuple[int, int]:
    """Read a window's own geometry via `xdotool`, independent of the adapter."""
    shell = subprocess.run(
        ["xdotool", "getwindowgeometry", "--shell", str(window_id)],
        capture_output=True,
        text=True,
        check=True,
    )
    values = dict(line.split("=", 1) for line in shell.stdout.splitlines() if "=" in line)
    return int(values["WIDTH"]), int(values["HEIGHT"])


def _adapter() -> LinuxHostPlatform:
    return LinuxHostPlatform(session_type=LinuxSessionType.x11)


def test_capture_returns_a_frame_of_the_windows_own_size(
    known_colour_window: GameWindow,
) -> None:
    result = _adapter().capture_window(known_colour_window)

    assert result.status is CaptureStatus.ok, result.reason
    assert result.frame is not None
    assert (result.frame.width, result.frame.height) == (_WIDTH, _HEIGHT)
    assert len(result.frame.image_bytes) == _WIDTH * _HEIGHT * 4


def test_captured_pixels_carry_the_windows_actual_colour(
    known_colour_window: GameWindow,
) -> None:
    """The channel-order assertion: a swapped decode fails here and only here."""
    from PIL import Image

    result = _adapter().capture_window(known_colour_window)
    assert result.status is CaptureStatus.ok, result.reason
    frame = result.frame
    assert frame is not None

    image = Image.frombytes(
        "RGBA", (frame.width, frame.height), frame.image_bytes, "raw", "BGRA"
    ).convert("RGB")

    # Sample a corner rather than the centre: xclock paints its timestamp
    # across the middle of the window, so the centre pixel is text, not
    # background, and would make this assertion flaky for the wrong reason.
    assert image.getpixel((4, 4)) == _BG_RGB


def test_frame_format_is_decodable_by_the_parity_screening_gates(
    known_colour_window: GameWindow,
) -> None:
    """A frame the gates cannot decode is treated as un-provable, never as clean."""
    from civsim_harness.parity.screening import _decode_frame

    result = _adapter().capture_window(known_colour_window)
    assert result.status is CaptureStatus.ok, result.reason
    assert result.frame is not None

    decoded = _decode_frame(result.frame)

    assert decoded is not None, (
        f"screening could not decode image_format={result.frame.image_format!r}"
    )
    assert decoded.size == (_WIDTH, _HEIGHT)
    assert decoded.getpixel((4, 4)) == _BG_RGB


def test_capture_of_a_destroyed_window_fails_without_falling_back_to_a_screen_grab(
    known_colour_window: GameWindow,
) -> None:
    """FR-025: no frame is strictly better than a frame containing someone else's pixels.

    A root-scoped grab of the same region would "succeed" here while leaking
    whatever occupies that screen area (see
    `spikes/r6-evidence/root-scoped-same-region-LEAKS.png`). The contract is
    that the adapter reports the failure instead.
    """
    stale = GameWindow(
        handle=0xDEADBEEF,
        title="does-not-exist",
        rect=known_colour_window.rect,
        pid=known_colour_window.pid,
    )

    result = _adapter().capture_window(stale)

    assert result.status in (CaptureStatus.failed, CaptureStatus.unavailable)
    assert result.frame is None
    assert result.reason


def test_preconditions_report_this_hosts_real_capture_capability() -> None:
    """Preflight must answer from the display's actual state, not from env vars."""
    _require(bool(os.environ.get("DISPLAY")), "no DISPLAY")

    preconditions = _adapter().capture_preconditions()

    assert preconditions.composite_extension is True
    assert preconditions.compositing_manager is True
    assert preconditions.can_capture is True
    # False is the safe value; True or None each carry a warning explaining
    # how a fullscreen client would lose its off-screen pixmap.
    assert preconditions.unredirect_fullscreen_windows is False
    assert preconditions.warnings == ()
