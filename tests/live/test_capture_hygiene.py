"""T194 live acceptance: SC-009 / SC-019 -- FireTuner/console/unrelated windows absent from
captures, and no capture border, through the harness's OWN `capture_window()`.

**Task**: T194. **Requirement**: SC-009 ("zero images shown to the agent contain the Firetuner
window, developer console, debug overlay, or other harness UI"), SC-019 (the same, for every
stored capture, not only what reaches the agent).

**Why this is the same assertion as the manual R6 spike, through production code.** The manual
probe (`specs/002-civ-playing-harness/spikes/r6-capture-hygiene-linux.md`) confirmed by hand that
window-scoped `XGetImage` under a compositing WM reads the client's own backing pixmap, not the
screen -- so an `xmessage` dialog placed on top of the client, and the terminal behind it, were
BOTH invisible to a window-scoped capture of the client's rectangle, while a root-scoped grab of
the identical region leaked both (see that spike's "occlusion test" section, including the
retained artifact of harness command text legible inside the leaked capture). That spike used
`import -window <id>` -- a hand-written probe, not the harness's own code path.

**What this module does differently, per T194's own wording**: it drives the exact same occlusion
scenario through `LinuxHostPlatform.capture_window()` (`src/civsim_harness/host/linux/adapter.py`)
-- the harness's real, production capture entry point -- rather than a bespoke `import` call.

**Why three generic colour-tagged proxy windows stand in for "FireTuner window", "a developer
console", and "an unrelated window".** There is no FireTuner GUI window or in-game Lua console
reliably launchable on this host by an unattended test (FireTuner here is a headless TCP tuner
protocol on port 4318, not a window; the in-game debug console has no scripted invocation this
repo already exercises). The R6 finding this module formalises is that window-scoped capture
under a compositing WM excludes ANY non-client top-level window **by construction** -- the
mechanism is "this is not the client's own backing pixmap", not "this looks like FireTuner" --
so three distinct, unambiguous, saturated-colour throwaway windows are a faithful, and honestly
labelled, operationalisation of all three named occluders: if window-scoping ever regressed to
leaking occluder content, it would leak these exactly as it would leak a real FireTuner window or
console. Literally launching real FireTuner chrome or the in-game console is explicitly NOT
covered here -- see "not observed" in this task's final report.

**What a human runs to make this pass, and what would make it fail**: with Civ VI up, X11 with a
compositing manager, and `xdotool`/`wmctrl`/`xmessage` installed, run
``pytest tests/live/test_capture_hygiene.py -m live``. It fails if any proxy occluder's colour
leaks into a `capture_window()` result of the Civ VI window (occlusion-immunity regression), or if
a clean capture's pixel dimensions disagree with the window's own rect (a capture-border/letterbox
regression). This module never sends input to, focuses, or otherwise mutates the Civ VI client --
every proxy window is spawned, moved, and killed independently of it; only `capture_window` reads
the client.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from civsim_harness.host.detect import LinuxSessionType  # noqa: E402
from civsim_harness.host.linux.adapter import LinuxHostPlatform  # noqa: E402
from civsim_harness.host.port import CaptureStatus, GameProcess, GameWindow  # noqa: E402

pytestmark = pytest.mark.live

# Three distinct, saturated, asymmetric colours -- see module docstring for why these stand in
# for "FireTuner window", "a developer console", and "an unrelated window" respectively.
_PROXIES: dict[str, tuple[str, tuple[int, int, int]]] = {
    "firetuner_window_proxy": ("#3366cc", (0x33, 0x66, 0xCC)),
    "developer_console_proxy": ("#cc3366", (0xCC, 0x33, 0x66)),
    "unrelated_window_proxy": ("#66cc33", (0x66, 0xCC, 0x33)),
}


def _require(condition: bool, reason: str) -> None:
    if not condition:
        pytest.skip(reason)


def _adapter() -> LinuxHostPlatform:
    return LinuxHostPlatform(session_type=LinuxSessionType.x11)


def _civ6_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


@pytest.fixture(scope="module")
def civ6_window() -> GameWindow:
    import os

    _require(bool(os.environ.get("DISPLAY")), "no DISPLAY; this test needs a live X11 session")
    pid = _civ6_pid()
    _require(pid is not None, "Civilization VI (Civ6 process) is not running")
    assert pid is not None

    adapter = _adapter()
    preconditions = adapter.capture_preconditions()
    _require(
        preconditions.composite_extension and preconditions.compositing_manager,
        "no compositing manager on this display; window-scoped capture is correctly "
        "unavailable here (R6) rather than something this test can exercise",
    )

    window = adapter.find_game_window(GameProcess(pid=pid, name="Civ6"))
    _require(window is not None, "find_game_window could not resolve the Civ6 window")
    assert window is not None
    return window


def _sample_colours(image_bytes: bytes, *, stride: int = 4004) -> set[tuple[int, int, int]]:
    """Sample BGRA pixels at a fixed stride and return their RGB triples.

    Mirrors `test_linux_xcomposite_capture.py`'s own sampling technique: a stride sample is
    cheap and, for a solid-colour occluder covering any real portion of the frame, a colour that
    is genuinely present is found with overwhelming probability -- this is not a full scan, but a
    real leak would show up here just as it would to a smaller, hand-inspected crop.
    """
    seen: set[tuple[int, int, int]] = set()
    limit = min(len(image_bytes) - 4, 8_000_000)
    for i in range(0, max(limit, 0), stride):
        b, g, r = image_bytes[i], image_bytes[i + 1], image_bytes[i + 2]
        seen.add((r, g, b))
    return seen


def _spawn_proxy(hex_colour: str, rect) -> subprocess.Popen[bytes]:
    """A throwaway, fully-opaque coloured window sized/positioned to cover *rect*."""
    geometry = f"{rect.width}x{rect.height}+{rect.left}+{rect.top}"
    return subprocess.Popen(
        ["xclock", "-digital", "-bg", hex_colour, "-geometry", geometry],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _await_window(pid: int, *, timeout_s: float) -> int | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = subprocess.run(
            ["xdotool", "search", "--pid", str(pid)], capture_output=True, text=True, check=False
        )
        ids = [line for line in found.stdout.split() if line.strip()]
        if ids:
            return int(ids[-1])
        time.sleep(0.2)
    return None


def _raise_above(window_id: int) -> None:
    subprocess.run(["wmctrl", "-i", "-a", "-"], check=False)  # no-op if wmctrl absent
    subprocess.run(["xdotool", "windowraise", str(window_id)], check=False)


def _stop(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        proc.kill()


def test_clean_capture_matches_the_window_rect_with_no_border(civ6_window: GameWindow) -> None:
    """No occluder present: `capture_window` returns a frame whose pixel dimensions equal the
    window's own rect exactly -- a capture-border or letterbox regression would change this."""
    result = _adapter().capture_window(civ6_window)

    assert result.status is CaptureStatus.ok, result.reason
    assert result.frame is not None
    assert (result.frame.width, result.frame.height) == (
        civ6_window.rect.width,
        civ6_window.rect.height,
    ), "captured frame dimensions disagree with the window's own rect -- looks like a border/crop"
    assert len(result.frame.image_bytes) == civ6_window.rect.width * civ6_window.rect.height * 4


@pytest.mark.parametrize("proxy_name", sorted(_PROXIES))
def test_occluding_window_is_absent_from_the_capture(
    civ6_window: GameWindow, proxy_name: str
) -> None:
    """A window on top of the client -- standing in for FireTuner / a developer console / an
    unrelated window (see module docstring) -- must not appear in `capture_window`'s output."""
    hex_colour, rgb = _PROXIES[proxy_name]
    proc = _spawn_proxy(hex_colour, civ6_window.rect)
    try:
        window_id = _await_window(proc.pid, timeout_s=10.0)
        _require(window_id is not None, f"the {proxy_name} throwaway window never appeared")
        assert window_id is not None
        _raise_above(window_id)
        time.sleep(0.3)

        result = _adapter().capture_window(civ6_window)

        assert result.status is CaptureStatus.ok, result.reason
        assert result.frame is not None
        colours = _sample_colours(result.frame.image_bytes)
        assert rgb not in colours, (
            f"{proxy_name} ({hex_colour}) leaked into a window-scoped capture of the client -- "
            "occlusion immunity regressed (FR-025/FR-030, SC-009/SC-019)"
        )
    finally:
        _stop(proc)
