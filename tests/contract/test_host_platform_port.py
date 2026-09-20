"""`HostPlatform` port conformance suite (T053).

Runs the same assertions against every adapter -- windows, macos, linux
(x11 and wayland session flavours) -- so "this platform is supported" is a
test result rather than a claim (plan.md Testing; research R19).

This suite needs no running Civilization VI client, matching plan.md's
`tests/contract` tier ("Needs Civ VI? No"). It gets there by design, not by
skipping the hard parts:

- `locate_game_process`, `resolve_game_directories`, and `free_disk_space`
  never touch an OS-specific library in this repo's implementation (R19's
  own capability table lists `psutil`/stdlib for every platform), so they
  are exercised for real against every adapter regardless of which OS is
  actually running the suite. `resolve_game_directories` is exercised
  against an injected, disposable `home` (a pytest `tmp_path`) rather than
  the operator's real filesystem, so "directories resolve and exist" is a
  genuine, deterministic assertion instead of one that depends on Civ VI
  being installed here.
- `capture_window` and `send_input` are two of the six capabilities R19
  and R5 document as genuinely conditional (a missing capture path, a
  missing optional platform dependency, or -- on Wayland -- input blocked
  by design). Their ports return a tagged `CaptureResult`/`InputResult`
  rather than raising, so "this adapter cannot do this on this host" is a
  directly assertable, never-raising outcome -- this is what T053 means by
  "an unavailable capability reports unavailable rather than raising an
  opaque error", and it is exercised for real for every adapter here, not
  skipped.
- `find_game_window` is the one method with no typed "unavailable"
  outcome, because window identity is not documented as an optional
  capability (only capture and input are). When the platform dependency it
  needs is not installed, it raises `civsim_harness.errors.PreflightError`
  -- an actionable, structured failure, never an opaque one -- and that is
  the one place this suite skips-with-reason rather than asserting further
  (never silently passing the adapter overall: every other capability for
  that adapter is still exercised in the other test functions below).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from civsim_harness.errors import PreflightError
from civsim_harness.host.detect import LinuxSessionType
from civsim_harness.host.port import (
    CaptureStatus,
    DiskSpace,
    GameDirectories,
    GameProcess,
    GameWindow,
    HostPlatform,
    InputEvent,
    InputEventKind,
    InputResult,
    InputStatus,
    WindowRect,
)


def _build_windows() -> HostPlatform:
    from civsim_harness.host.windows.adapter import WindowsHostPlatform

    return WindowsHostPlatform()


def _build_macos() -> HostPlatform:
    from civsim_harness.host.macos.adapter import MacOSHostPlatform

    return MacOSHostPlatform()


def _build_linux_x11() -> HostPlatform:
    from civsim_harness.host.linux.adapter import LinuxHostPlatform

    return LinuxHostPlatform(session_type=LinuxSessionType.x11)


def _build_linux_wayland() -> HostPlatform:
    from civsim_harness.host.linux.adapter import LinuxHostPlatform

    return LinuxHostPlatform(session_type=LinuxSessionType.wayland)


# Every adapter, exercised by the same assertions below. Construction
# itself never imports a platform library in this repo's design (see each
# adapter module's docstring), so none of these need an OS or
# dependency-based skip merely to instantiate.
_ADAPTERS: list[tuple[str, Callable[[], HostPlatform]]] = [
    ("windows", _build_windows),
    ("macos", _build_macos),
    ("linux-x11", _build_linux_x11),
    ("linux-wayland", _build_linux_wayland),
]

_ADAPTER_IDS = [adapter_id for adapter_id, _ in _ADAPTERS]


@pytest.mark.parametrize("adapter_id,factory", _ADAPTERS, ids=_ADAPTER_IDS)
def test_adapter_satisfies_the_host_platform_protocol(
    adapter_id: str, factory: Callable[[], HostPlatform]
) -> None:
    adapter = factory()
    assert isinstance(adapter, HostPlatform)


@pytest.mark.parametrize("adapter_id,factory", _ADAPTERS, ids=_ADAPTER_IDS)
def test_locate_game_process_never_raises(
    adapter_id: str, factory: Callable[[], HostPlatform]
) -> None:
    adapter = factory()
    result = adapter.locate_game_process()
    assert result is None or isinstance(result, GameProcess)


def _process_names_for(adapter_id: str) -> tuple[str, ...]:
    """Return the adapter's hard-coded candidate process-name tuple.

    This is what the live Linux adjudication (this wave's audit) turned on: the
    Linux adapter's `_PROCESS_NAMES` held only the Windows executable name until a
    real client proved it wrong, and `locate_game_process()` silently returned
    `None` forever as a result -- the "returns None on every real machine" failure
    mode this suite now guards against structurally, for every platform, since none
    of windows/macos has a machine here to catch the same mistake the way Linux's
    live client did.
    """
    if adapter_id == "windows":
        from civsim_harness.host.windows.adapter import _PROCESS_NAMES
    elif adapter_id == "macos":
        from civsim_harness.host.macos.adapter import _PROCESS_NAMES
    else:  # "linux-x11", "linux-wayland" -- one module, one constant
        from civsim_harness.host.linux.adapter import _PROCESS_NAMES
    return _PROCESS_NAMES


class _FakePsutilProcess:
    """A minimal stand-in for `psutil.Process`, scripted with a fixed name/pid.

    Only the surface `locate_process_by_names` (host/_shared.py) actually calls:
    `.pid`, `.name()`, `.exe()`.
    """

    def __init__(self, name: str, pid: int) -> None:
        self.pid = pid
        self._name = name

    def name(self) -> str:
        return self._name

    def exe(self) -> str:
        return f"/fake/install/dir/{self._name}"


@pytest.mark.parametrize("adapter_id,factory", _ADAPTERS, ids=_ADAPTER_IDS)
def test_process_name_candidates_are_non_empty(
    adapter_id: str, factory: Callable[[], HostPlatform]
) -> None:
    """Guards the emptiest version of the Linux failure: an adapter with zero
    candidate names could never match a running client under any circumstance."""
    assert len(_process_names_for(adapter_id)) > 0


@pytest.mark.parametrize("adapter_id,factory", _ADAPTERS, ids=_ADAPTER_IDS)
def test_locate_game_process_matches_every_candidate_name(
    adapter_id: str, factory: Callable[[], HostPlatform], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripts a fake running process named after each of the adapter's own
    candidate names in turn, and asserts `locate_game_process()` actually matches
    it -- so a wrong or stale candidate name is a test failure, not silence, on
    every platform this suite runs on regardless of which OS is hosting it."""
    adapter = factory()
    for candidate in _process_names_for(adapter_id):
        fake_process = _FakePsutilProcess(name=candidate, pid=54321)
        monkeypatch.setattr(
            psutil, "process_iter", lambda proc=fake_process: iter([proc])
        )
        result = adapter.locate_game_process()
        assert result is not None, (
            f"{adapter_id}: locate_game_process() failed to match its own "
            f"candidate name {candidate!r} -- this is exactly the silent-None "
            "failure mode that killed the Linux adapter before a live client "
            "caught it."
        )
        assert result.pid == 54321
        assert result.name == candidate


@pytest.mark.parametrize("adapter_id,factory", _ADAPTERS, ids=_ADAPTER_IDS)
def test_resolve_game_directories_resolves_and_exists(
    adapter_id: str, factory: Callable[[], HostPlatform], tmp_path: Path
) -> None:
    adapter = factory()
    dirs = adapter.resolve_game_directories(home=tmp_path)
    assert isinstance(dirs, GameDirectories)
    assert dirs.saves_dir.is_absolute()
    assert dirs.app_options_path.is_absolute()
    assert tmp_path in dirs.saves_dir.parents
    assert tmp_path in dirs.app_options_path.parents

    # "directories resolve and exist" (T053), exercised for real by
    # creating the resolved tree under the injected, disposable `home`
    # rather than touching the operator's actual filesystem or requiring
    # Civ VI to be installed on this machine.
    dirs.saves_dir.mkdir(parents=True, exist_ok=True)
    dirs.app_options_path.parent.mkdir(parents=True, exist_ok=True)
    dirs.app_options_path.touch()
    assert dirs.saves_dir.exists()
    assert dirs.app_options_path.exists()


@pytest.mark.parametrize("adapter_id,factory", _ADAPTERS, ids=_ADAPTER_IDS)
def test_free_disk_space_reads(
    adapter_id: str, factory: Callable[[], HostPlatform], tmp_path: Path
) -> None:
    adapter = factory()
    space = adapter.free_disk_space(tmp_path)
    assert isinstance(space, DiskSpace)
    assert space.total_bytes >= space.free_bytes >= 0


@pytest.mark.parametrize("adapter_id,factory", _ADAPTERS, ids=_ADAPTER_IDS)
def test_find_game_window_never_raises_an_opaque_error(
    adapter_id: str, factory: Callable[[], HostPlatform]
) -> None:
    adapter = factory()
    fake_process = GameProcess(pid=999_999, name="not-really-running", executable_path=None)
    try:
        window = adapter.find_game_window(fake_process)
    except PreflightError as exc:
        pytest.skip(
            f"{adapter_id}: window identity needs a platform dependency not installed "
            f"here ({exc.message}); this is the one skip-with-reason this suite takes "
            "(see module docstring) -- every other capability for this adapter is still "
            "exercised elsewhere in this suite."
        )
        return
    assert window is None or isinstance(window, GameWindow)


@pytest.mark.parametrize("adapter_id,factory", _ADAPTERS, ids=_ADAPTER_IDS)
def test_capture_window_reports_unavailable_or_failed_rather_than_raising(
    adapter_id: str, factory: Callable[[], HostPlatform]
) -> None:
    adapter = factory()
    synthetic_window = GameWindow(
        handle=0, title="synthetic-test-window", rect=WindowRect(0, 0, 640, 480), pid=999_999
    )
    result = adapter.capture_window(synthetic_window)  # must never raise
    assert result.status in (CaptureStatus.ok, CaptureStatus.unavailable, CaptureStatus.failed)
    if result.status is CaptureStatus.ok:
        assert result.frame is not None
        assert result.frame.rect == synthetic_window.rect
    else:
        # Guaranteed non-empty by CaptureResult.__post_init__, re-asserted
        # here as the directly load-bearing check for T053's requirement.
        assert result.reason


@pytest.mark.parametrize("adapter_id,factory", _ADAPTERS, ids=_ADAPTER_IDS)
def test_send_input_never_raises(adapter_id: str, factory: Callable[[], HostPlatform]) -> None:
    adapter = factory()
    events = [InputEvent(kind=InputEventKind.key_press, key="enter")]
    result = adapter.send_input(events)  # must never raise
    assert isinstance(result, InputResult)
    assert result.status in (InputStatus.ok, InputStatus.unavailable, InputStatus.failed)
    if result.status is not InputStatus.ok:
        assert result.reason


def test_linux_wayland_reports_synthetic_input_as_unavailable_by_design() -> None:
    """R5/R19/T052: Wayland blocks synthetic input outright, and the adapter must
    report that without even attempting it -- so this holds regardless of whether
    `python-xlib` is installed here, and regardless of the current host's own OS."""
    from civsim_harness.host.linux.adapter import LinuxHostPlatform

    adapter = LinuxHostPlatform(session_type=LinuxSessionType.wayland)
    result = adapter.send_input([InputEvent(kind=InputEventKind.key_press, key="enter")])
    assert result.status is InputStatus.unavailable
    assert result.reason is not None
    assert "wayland" in result.reason.lower()


def test_this_run_reports_which_adapters_executed_live_vs_degraded() -> None:
    """Not a correctness assertion -- a visible record of this run's own honesty
    (this wave's instructions: report what actually executed vs. what was skipped).
    Prints which adapters could construct/import their platform dependency on this
    host, matching the report's "which adapters actually executed" requirement."""
    report: dict[str, str] = {}
    for adapter_id, factory in _ADAPTERS:
        adapter = factory()
        try:
            adapter.find_game_window(GameProcess(pid=999_999, name="probe", executable_path=None))
            report[adapter_id] = "window-identity dependency available on this host"
        except PreflightError as exc:
            report[adapter_id] = f"window-identity dependency missing: {exc.message}"
    print(f"\nHostPlatform adapter execution reality on {sys.platform}: {report}")
    assert len(report) == len(_ADAPTERS)
