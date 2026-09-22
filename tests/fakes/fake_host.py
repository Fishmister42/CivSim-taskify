"""Fake `HostPlatform` test double (T055).

Implements `civsim_harness.host.port.HostPlatform` with every capability
scriptable, so the turn cycle, the preflight tier gate, and the degradation
paths (FR-050/FR-054) can be exercised deterministically on any CI platform
-- no OS-specific library, no real Civ VI window, no real disk probe.

Two scripting layers:

1. **Support tier** (`set_tier` / constructor `tier=`): presents as
   `SupportTier.validated`, `.supported`, or `.unsupported` on demand
   (research R19). `probe_result` derives a `SupportProbeResult` consistent
   with the scripted tier, so `resolve_support_tier(fake.probe_result) ==
   fake.tier` always holds -- a later wave's preflight tier gate can drive
   this fake exactly as it would a probed real adapter.
2. **Per-capability overrides** (`set_process`, `set_window`,
   `set_find_window_error`, `set_capture_result` / `set_capture_unavailable`
   / `set_capture_failed`, `set_capture_preconditions` /
   `set_capture_preconditions_failed` (T249), `set_input_result` /
   `set_input_unavailable` / `set_input_failed`, `set_directories`,
   `set_disk_space`): make any single
   capability behave independently of the overall tier, e.g. a `validated`
   host whose capture path has gone `unavailable` mid-run.

`CaptureResult`/`InputResult` require a reason on any non-`ok` status
(`host/port.py`'s `__post_init__`); every unavailable/failed helper below
takes `reason` as a required argument so that invariant cannot be
accidentally violated from this fake.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from civsim_harness.errors import PreflightError
from civsim_harness.host.detect import SupportProbeResult, SupportTier
from civsim_harness.host.port import (
    CaptureFrame,
    CapturePreconditionResult,
    CaptureResult,
    CaptureStatus,
    DiskSpace,
    GameDirectories,
    GameProcess,
    GameWindow,
    InputEvent,
    InputResult,
    InputStatus,
    WindowRect,
    WindowTitleListing,
)

# --------------------------------------------------------------------------
# Defaults -- a plausible, deterministic "everything is fine" host
# --------------------------------------------------------------------------

DEFAULT_PROCESS = GameProcess(
    pid=42_424, name="CivilizationVI_FAKE", executable_path=None
)
"""SYNTHETIC: a plausible-looking process, not sampled from a real machine."""

DEFAULT_WINDOW = GameWindow(
    handle=1,
    title="Sid Meier's Civilization VI (FAKE)",
    rect=WindowRect(left=0, top=0, width=1920, height=1080),
    pid=DEFAULT_PROCESS.pid,
)
"""SYNTHETIC: a plausible-looking window, not sampled from a real machine."""

DEFAULT_WINDOW_TITLES = (DEFAULT_WINDOW.title,)
"""SYNTHETIC: the desktop this fake presents to `list_window_titles` (T265) -- the game's own
window and nothing else.

Deliberately a desktop that trips **no** reject category in
`catalogs/screening_profiles.yaml`, so the default fake is a host on which a clean frame is
genuinely deliverable. That default matters: with `available=False` the content gate has no
technique for eight of its ten reject categories and withholds every frame, so a fake that
defaulted to "cannot enumerate" would quietly make the entire image-delivery path
unreachable from tests while every assertion about withholding still passed.
"""

_DEFAULT_SAVES_SUBDIR = ("FakeCivSaves", "Saves")
_DEFAULT_APP_OPTIONS_SUBDIR = ("FakeCivSaves",)
_DEFAULT_APP_OPTIONS_NAME = "AppOptions.txt"

_DEFAULT_FREE_BYTES = 500 * 1024**3
_DEFAULT_TOTAL_BYTES = 1000 * 1024**3


class FakeHostPlatform:
    """Scriptable `HostPlatform` implementation (T055).

    Every method below satisfies `civsim_harness.host.port.HostPlatform`
    structurally (it is a `Protocol`, so no explicit inheritance is
    needed) -- `isinstance(FakeHostPlatform(), HostPlatform)` holds, as
    verified by this module's own smoke test.
    """

    def __init__(self, *, tier: SupportTier = SupportTier.validated) -> None:
        self._tier = tier

        self._process: GameProcess | None = DEFAULT_PROCESS
        self._window: GameWindow | None = DEFAULT_WINDOW
        self._find_window_error: PreflightError | None = None
        self._capture_override: CaptureResult | None = None
        self._capture_preconditions_override: CapturePreconditionResult | None = None
        self._window_titles: WindowTitleListing = WindowTitleListing(
            available=True,
            reason="fake host: scripted desktop (T055/T265 default)",
            titles=DEFAULT_WINDOW_TITLES,
        )
        self._input_override: InputResult | None = None
        self._directories_override: GameDirectories | None = None
        self._disk_space_override: DiskSpace | None = None

        # Call logs, so a scripted scenario can also assert on *what* the
        # harness asked for, not just what it got back.
        self.capture_calls: list[GameWindow] = []
        self.capture_precondition_calls = 0
        self.input_calls: list[list[InputEvent]] = []
        self.focus_calls: list[GameWindow] = []
        self._focus_override: InputResult | None = None
        self.locate_process_calls = 0
        self.window_title_calls = 0
        self.find_window_calls: list[GameProcess] = []
        self.resolve_directories_calls: list[Path | None] = []
        self.free_disk_space_calls: list[Path] = []

    # -- tier scripting ----------------------------------------------------

    @property
    def tier(self) -> SupportTier:
        return self._tier

    def set_tier(self, tier: SupportTier) -> None:
        """Present this host as `VALIDATED`, `SUPPORTED`, or `UNSUPPORTED` on demand.

        Only changes the tier itself and the *default* capture behaviour
        derived from it (see `capture_window`); any explicit per-capability
        override set via `set_capture_result` etc. still wins.
        """
        self._tier = tier

    @property
    def probe_result(self) -> SupportProbeResult:
        """A `SupportProbeResult` consistent with the scripted tier.

        `civsim_harness.host.detect.resolve_support_tier(fake.probe_result)
        == fake.tier` always holds -- this is the seam a preflight tier gate
        is expected to drive, mirroring how a real adapter's probe evidence
        would be assembled (research R19).
        """
        if self._tier is SupportTier.unsupported:
            return SupportProbeResult(
                quicksave_path_verified=False,
                capture_hygiene_spike_passed=False,
                reason="fake host scripted UNSUPPORTED: no verified quicksave path",
            )
        if self._tier is SupportTier.supported:
            return SupportProbeResult(
                quicksave_path_verified=True,
                capture_hygiene_spike_passed=False,
                reason=(
                    "fake host scripted SUPPORTED: quicksave path verified, "
                    "no passing capture-hygiene spike (visually degraded)"
                ),
            )
        return SupportProbeResult(
            quicksave_path_verified=True,
            capture_hygiene_spike_passed=True,
            reason=(
                "fake host scripted VALIDATED: quicksave verified and "
                "capture-hygiene spike passed"
            ),
        )

    # -- per-capability scripting -------------------------------------------

    def set_process(self, process: GameProcess | None) -> None:
        """Script `locate_game_process()`'s return value; `None` = not running."""
        self._process = process

    def set_window(self, window: GameWindow | None) -> None:
        """Script `find_game_window()`'s return value; `None` = no window yet."""
        self._window = window
        self._find_window_error = None

    def set_find_window_error(self, error: PreflightError | None) -> None:
        """Script `find_game_window()` to raise `error` instead of returning.

        Mirrors the one documented raising path: window identity cannot be
        resolved *at all* on this host (`HostPlatform.find_game_window`'s
        docstring; e.g. a required optional platform dependency missing).
        """
        self._find_window_error = error

    def set_capture_result(self, result: CaptureResult | None) -> None:
        """Script `capture_window()`'s return value directly. `None` restores
        the tier-derived default (see `capture_window`)."""
        self._capture_override = result

    def set_capture_unavailable(self, reason: str) -> None:
        """Make capture report `unavailable` (a documented gap, never a raise)."""
        self.set_capture_result(CaptureResult(status=CaptureStatus.unavailable, reason=reason))

    def set_capture_failed(self, reason: str) -> None:
        """Make capture report `failed` (an attempt that broke partway through)."""
        self.set_capture_result(CaptureResult(status=CaptureStatus.failed, reason=reason))

    def set_capture_preconditions(self, result: CapturePreconditionResult | None) -> None:
        """Script `check_capture_preconditions()`'s return value directly (T249). `None`
        restores the default: a pass whose reason says it was scripted, independent of the
        tier -- the preflight can only take capture away, never grant it, so a passing
        default never widens what any tier permits."""
        self._capture_preconditions_override = result

    def set_capture_preconditions_failed(self, reason: str) -> None:
        """Make the T249 capture-precondition preflight fail with `reason` -- the capture
        path must then withhold the step without ever calling `capture_window` (assert via
        `capture_calls`)."""
        self.set_capture_preconditions(CapturePreconditionResult(passed=False, reason=reason))

    def set_input_result(self, result: InputResult | None) -> None:
        """Script `send_input()`'s return value directly. `None` restores the default (`ok`)."""
        self._input_override = result

    def set_input_unavailable(self, reason: str) -> None:
        """Make synthetic input report `unavailable` (e.g. simulating Wayland, R5/R19)."""
        self.set_input_result(InputResult(status=InputStatus.unavailable, reason=reason))

    def set_input_failed(self, reason: str) -> None:
        """Make synthetic input report `failed` (dispatch attempted and errored)."""
        self.set_input_result(InputResult(status=InputStatus.failed, reason=reason))

    def set_directories(self, directories: GameDirectories) -> None:
        """Script `resolve_game_directories()`'s return value."""
        self._directories_override = directories

    def set_window_titles(self, titles: Sequence[str]) -> None:
        """Script the desktop `list_window_titles()` reports (T265).

        This is the content gate's only text evidence, so it is how a scenario puts
        non-player chrome on the desktop: a title of `"Developer Console"` makes the gate
        match the `developer_console` reject id and withhold the frame.
        """
        self._window_titles = WindowTitleListing(
            available=True,
            reason=f"fake host: scripted desktop of {len(titles)} window(s)",
            titles=tuple(titles),
        )

    def set_window_titles_unavailable(self, reason: str) -> None:
        """Present a host that cannot enumerate window titles at all -- the real state of the
        Windows and macOS adapters, and of any Wayland session (T265).

        The content gate then has no technique for most of its reject categories and must
        withhold every frame: this is how a test drives "image delivery is closed on this
        platform" through the production loop rather than asserting it about a docstring.
        """
        self._window_titles = WindowTitleListing(available=False, reason=reason)

    def set_disk_space(self, disk_space: DiskSpace) -> None:
        """Script `free_disk_space()`'s return value (R17 disk-headroom guard tests)."""
        self._disk_space_override = disk_space

    # -- HostPlatform protocol -----------------------------------------------

    def locate_game_process(self) -> GameProcess | None:
        self.locate_process_calls += 1
        return self._process

    def find_game_window(self, process: GameProcess) -> GameWindow | None:
        self.find_window_calls.append(process)
        if self._find_window_error is not None:
            raise self._find_window_error
        return self._window

    def check_capture_preconditions(self) -> CapturePreconditionResult:
        self.capture_precondition_calls += 1
        if self._capture_preconditions_override is not None:
            return self._capture_preconditions_override
        return CapturePreconditionResult(
            passed=True,
            reason="fake host: capture preconditions scripted to pass (T055 default)",
        )

    def list_window_titles(self) -> WindowTitleListing:
        self.window_title_calls += 1
        return self._window_titles

    def capture_window(self, window: GameWindow) -> CaptureResult:
        self.capture_calls.append(window)
        if self._capture_override is not None:
            return self._capture_override
        if self._tier is SupportTier.unsupported:
            return CaptureResult(
                status=CaptureStatus.unavailable,
                reason="fake host scripted UNSUPPORTED: no capture path available",
            )
        # `supported` and `validated` both capture successfully -- the tier
        # difference (FR-050/FR-054's "visually degraded") is that
        # `supported` has no *passing capture-hygiene spike*, which is
        # carried in `probe_result`, not in whether a frame comes back.
        frame_bytes = bytes(max(window.rect.width, 0) * max(window.rect.height, 0) * 4)
        return CaptureResult(
            status=CaptureStatus.ok,
            frame=CaptureFrame(
                width=window.rect.width,
                height=window.rect.height,
                rect=window.rect,
                image_bytes=frame_bytes,
                image_format="BGRA8",
            ),
        )

    def resolve_game_directories(self, *, home: Path | None = None) -> GameDirectories:
        self.resolve_directories_calls.append(home)
        if self._directories_override is not None:
            return self._directories_override
        base = home if home is not None else Path.home()
        saves_dir = base.joinpath(*_DEFAULT_SAVES_SUBDIR)
        app_options_path = base.joinpath(*_DEFAULT_APP_OPTIONS_SUBDIR, _DEFAULT_APP_OPTIONS_NAME)
        return GameDirectories(saves_dir=saves_dir, app_options_path=app_options_path)

    def send_input(self, events: Sequence[InputEvent]) -> InputResult:
        self.input_calls.append(list(events))
        if self._input_override is not None:
            return self._input_override
        return InputResult(status=InputStatus.ok)

    def set_focus_result(self, result: InputResult | None) -> None:
        """Script `focus_window()`'s return value directly (T248). `None` restores the
        default (`ok`). A non-`ok` result is how a scenario proves the loader refuses to
        press a key at a window it could not bring to the front."""
        self._focus_override = result

    def focus_window(self, window: GameWindow) -> InputResult:
        self.focus_calls.append(window)
        if self._focus_override is not None:
            return self._focus_override
        return InputResult(status=InputStatus.ok)

    def free_disk_space(self, path: Path) -> DiskSpace:
        self.free_disk_space_calls.append(path)
        if self._disk_space_override is not None:
            return self._disk_space_override
        return DiskSpace(
            path=path, free_bytes=_DEFAULT_FREE_BYTES, total_bytes=_DEFAULT_TOTAL_BYTES
        )
