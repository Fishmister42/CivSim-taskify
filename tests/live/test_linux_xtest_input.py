"""Live XTest input tests for the Linux host adapter (T052, research R5, R19).

**Every test here injects synthetic input into a nested `Xephyr` server on its
own display, never the operator's session.** `xtest.fake_input` has no window
targeting whatsoever -- it injects at the server level, into whatever holds
focus -- so a test that ran against the real `$DISPLAY` could type into the
operator's browser. The isolation is a safety requirement, not tidiness.

What a green run establishes, by reading `xev`'s event stream rather than
trusting `InputResult.status`:

* keystrokes arrive as **real device events** (`synthetic NO`), not
  `XSendEvent` fakes an application is free to ignore
* an event the adapter cannot dispatch is reported `failed`, never silently
  dropped while the batch reports `ok`

Run explicitly -- excluded from the default suite:

    pytest tests/live/test_linux_xtest_input.py -m live
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from civsim_harness.host.detect import LinuxSessionType
from civsim_harness.host.linux.adapter import LinuxHostPlatform
from civsim_harness.host.port import InputEvent, InputEventKind, InputStatus

pytestmark = pytest.mark.live

_NESTED_DISPLAY = ":97"
_SCREEN = "800x600"


def _require(condition: bool, reason: str) -> None:
    if not condition:
        pytest.skip(reason)


@pytest.fixture(scope="module")
def nested_x(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A throwaway X server plus an `xev` inside it, yielding `xev`'s log path."""
    _require(shutil.which("Xephyr") is not None, "Xephyr is not installed (xserver-xephyr)")
    _require(shutil.which("xev") is not None, "xev is not installed (x11-utils)")
    _require(bool(os.environ.get("DISPLAY")), "Xephyr needs a host DISPLAY to draw into")

    log_dir = tmp_path_factory.mktemp("xtest")
    xephyr_log = (log_dir / "xephyr.log").open("wb")
    server = subprocess.Popen(
        ["Xephyr", _NESTED_DISPLAY, "-screen", _SCREEN, "-ac", "-br", "-noreset"],
        stdout=xephyr_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        _await_display(_NESTED_DISPLAY, timeout_s=15.0)

        xev_path = log_dir / "xev.log"
        xev_log = xev_path.open("wb")
        nested_env = {**os.environ, "DISPLAY": _NESTED_DISPLAY}
        viewer = subprocess.Popen(
            ["xev", "-geometry", f"{_SCREEN}+0+0"],
            stdout=xev_log,
            stderr=subprocess.STDOUT,
            env=nested_env,
            start_new_session=True,
        )
        try:
            # xev must be mapped and have the pointer over it before XTest
            # input can reach it: with no window manager in the nested server
            # focus is PointerRoot, so it follows the pointer.
            time.sleep(1.5)
            yield xev_path
        finally:
            _stop(viewer)
            xev_log.close()
    finally:
        _stop(server)
        xephyr_log.close()


def _await_display(display: str, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    probe_env = {**os.environ, "DISPLAY": display}
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["xdpyinfo"], env=probe_env, capture_output=True, check=False
        )
        if probe.returncode == 0:
            return
        time.sleep(0.3)
    pytest.skip(f"nested X server {display} never came up")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        proc.kill()


@pytest.fixture
def adapter(nested_x: Path, monkeypatch: pytest.MonkeyPatch) -> LinuxHostPlatform:
    """The real adapter, pointed at the nested server for the duration of a test."""
    monkeypatch.setenv("DISPLAY", _NESTED_DISPLAY)
    return LinuxHostPlatform(session_type=LinuxSessionType.x11)


def _events_since(log: Path, offset: int) -> str:
    time.sleep(0.4)  # xev writes line-buffered; let the events land
    return log.read_text(errors="replace")[offset:]


def _size(log: Path) -> int:
    return len(log.read_text(errors="replace"))


def test_keystrokes_arrive_as_real_device_events(
    adapter: LinuxHostPlatform, nested_x: Path
) -> None:
    """`synthetic NO` is the point: a game may filter XSendEvent fakes, not these."""
    offset = _size(nested_x)

    result = adapter.send_input([InputEvent(kind=InputEventKind.key_press, key="escape")])

    assert result.status is InputStatus.ok, result.reason
    seen = _events_since(nested_x, offset)
    assert "keysym 0xff1b, Escape" in seen
    assert "synthetic NO" in seen


def test_text_events_actually_type_their_text(
    adapter: LinuxHostPlatform, nested_x: Path
) -> None:
    """Regression: `InputEventKind.text` had no branch and was dropped, reporting `ok`.

    The bespoke save path needs this specifically -- a save dialog wants a
    filename typed into it.
    """
    offset = _size(nested_x)

    result = adapter.send_input([InputEvent(kind=InputEventKind.text, text="civsim")])

    assert result.status is InputStatus.ok, result.reason
    seen = _events_since(nested_x, offset)
    for keysym, char in (("0x63", "c"), ("0x69", "i"), ("0x76", "v"), ("0x73", "s")):
        assert f"keysym {keysym}, {char}" in seen, f"{char!r} never arrived"


def test_text_events_hold_shift_for_capitals_and_symbols(
    adapter: LinuxHostPlatform, nested_x: Path
) -> None:
    """Shift level is taken from the server's keymap, not assumed to be US layout."""
    offset = _size(nested_x)

    result = adapter.send_input([InputEvent(kind=InputEventKind.text, text="Civ_1!")])

    assert result.status is InputStatus.ok, result.reason
    seen = _events_since(nested_x, offset)
    assert "keysym 0xffe1, Shift_L" in seen
    assert "keysym 0x43, C" in seen
    assert "keysym 0x5f, underscore" in seen
    assert "keysym 0x21, exclam" in seen


def test_click_honours_the_requested_button(
    adapter: LinuxHostPlatform, nested_x: Path
) -> None:
    """Regression: `button` was ignored and every click dispatched as button 1."""
    offset = _size(nested_x)

    result = adapter.send_input(
        [InputEvent(kind=InputEventKind.mouse_click, x=400, y=300, button="right")]
    )

    assert result.status is InputStatus.ok, result.reason
    assert "button 3" in _events_since(nested_x, offset)


def test_mouse_coordinates_are_root_absolute(
    adapter: LinuxHostPlatform, nested_x: Path
) -> None:
    """XTest positions the pointer in ROOT coordinates, which the port does not state.

    This is asserted so that a future change to a window-relative
    interpretation cannot pass silently: on a multi-monitor desktop the
    difference is the whole offset of the monitor the client sits on.
    """
    offset = _size(nested_x)

    result = adapter.send_input([InputEvent(kind=InputEventKind.mouse_click, x=400, y=300)])

    assert result.status is InputStatus.ok, result.reason
    assert "root:(400,300)" in _events_since(nested_x, offset)


@pytest.mark.parametrize(
    ("event", "expected_fragment"),
    [
        (InputEvent(kind=InputEventKind.key_press, key="Wingding"), "no keysym for"),
        (InputEvent(kind=InputEventKind.key_press, key=None), "carries no `key`"),
        (InputEvent(kind=InputEventKind.text, text=""), "carries no `text`"),
        (InputEvent(kind=InputEventKind.mouse_click), "not a dispatchable combination"),
        (
            InputEvent(kind=InputEventKind.mouse_click, x=1, y=1, button="thumb"),
            "known buttons",
        ),
    ],
)
def test_undispatchable_events_fail_loudly_instead_of_being_dropped(
    adapter: LinuxHostPlatform, event: InputEvent, expected_fragment: str
) -> None:
    """Silent input loss is worse than failure: the harness would record it acted.

    Every one of these previously returned `ok` having dispatched nothing at
    all, verified against a live XTest server before the fix.
    """
    result = adapter.send_input([event])

    assert result.status is InputStatus.failed
    assert result.reason is not None
    assert expected_fragment in result.reason
