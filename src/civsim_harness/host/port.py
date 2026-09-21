"""The `HostPlatform` port (T047).

Covers **exactly six** capabilities the harness needs from an operating
system (research R19): locate the game process, identify its window,
capture that window, resolve game directories (saves + `AppOptions.txt`),
optional synthetic input, and free disk space. One cross-cutting hook rides
on the capture capability rather than being a seventh:
`check_capture_preconditions` (T249), the cheap per-capture hygiene
preflight the capture path consults before ever taking a frame. Nothing
else belongs here -- process liveness beyond "is it running right now",
camera control, and everything else the harness does is portable and lives
outside `host/`.

This module itself imports nothing platform-specific: it is pure `typing`,
`dataclasses`, `enum`, and stdlib `pathlib`. Only `host/windows`,
`host/macos`, and `host/linux` may reach for an OS-specific library, and
only inside the methods that need one (see those packages' adapters).

Two of the six capabilities -- capture and synthetic input -- are the ones
research R6, R5, and R19 document as genuinely conditional per platform and
per session (Wayland blocks input outright; a capture path may not exist or
may not yet be proven clean). Their results are therefore a tagged outcome
(`CaptureResult`, `InputResult`) rather than a plain value, so "this is not
available here" is representable without raising, and the type itself
enforces that a non-`ok` outcome always carries an actionable reason -- see
`CaptureResult.__post_init__` and `InputResult.__post_init__`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GameProcess:
    """The running Civilization VI client process, as located by the host (R12, R19)."""

    pid: int
    name: str
    executable_path: Path | None = None


@dataclass(frozen=True)
class WindowRect:
    """A window's rectangle, in the coordinate space its adapter reports (R7 geometry gate)."""

    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class GameWindow:
    """The game client's own window, identified well enough to capture it (R6, R7).

    `handle` is an opaque, adapter-native identifier -- a Win32 HWND, a
    macOS `CGWindowID`, or an X11 window XID, all of which are representable
    as a plain integer. Callers must never interpret it themselves; it only
    ever flows back into the adapter that produced it.
    """

    handle: int
    title: str
    rect: WindowRect
    pid: int


class CaptureStatus(Enum):
    """The outcome of a single `capture_window` attempt (R6, R19)."""

    ok = "ok"
    """A frame was captured and is trusted to match the requested window."""

    unavailable = "unavailable"
    """This platform/session cannot capture at all right now (R6 rank 4, missing
    permission grant, or a missing optional dependency). Never a raised exception."""

    failed = "failed"
    """A capture path was attempted and errored partway through. Distinct from
    `unavailable` because it names an attempt that broke, not a documented gap."""


@dataclass(frozen=True)
class CaptureFrame:
    """A single captured frame, ready for the parity image-screening gates (R7)."""

    width: int
    height: int
    rect: WindowRect
    image_bytes: bytes
    image_format: str
    """E.g. `"BGRA8"` for a raw framebuffer or `"PNG"` for an encoded frame."""


@dataclass(frozen=True)
class CaptureResult:
    """The tagged outcome of `capture_window` (T053: "reports unavailable, never raises")."""

    status: CaptureStatus
    frame: CaptureFrame | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is CaptureStatus.ok:
            if self.frame is None:
                raise ValueError("CaptureResult.status is 'ok' but no frame was supplied")
        else:
            if self.frame is not None:
                raise ValueError(
                    f"CaptureResult.status is {self.status!r} but a frame was supplied"
                )
            if not self.reason:
                raise ValueError(
                    f"CaptureResult.status is {self.status!r} but no reason was given"
                )


@dataclass(frozen=True)
class CapturePreconditionResult:
    """The tagged outcome of `check_capture_preconditions` (T249).

    `reason` is required in BOTH directions, unlike `CaptureResult`'s: a
    failure must name the unmet condition (it becomes the withheld capture
    record's recorded reason), and a pass must say what was actually
    checked -- or state explicitly that nothing is cheaply checkable on
    this platform today -- so a hard-coded, evidence-free pass is
    unrepresentable. Enforced by `__post_init__`, matching this port's
    existing tagged-outcome types.
    """

    passed: bool
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError(
                "CapturePreconditionResult requires a reason whether it passed or failed: "
                "a pass must name what was checked (or say nothing could be), and a "
                "failure must name the unmet condition"
            )


@dataclass(frozen=True)
class GameDirectories:
    """The platform's Civ VI directories the harness needs (R1, R5): saves and `AppOptions.txt`."""

    saves_dir: Path
    app_options_path: Path


class InputEventKind(Enum):
    """The shapes of synthetic input the bespoke save/load path needs (R5)."""

    key_down = "key_down"
    key_up = "key_up"
    key_press = "key_press"
    text = "text"
    mouse_move = "mouse_move"
    mouse_click = "mouse_click"


@dataclass(frozen=True)
class InputEvent:
    """One synthetic input event. Only the fields relevant to `kind` are populated."""

    kind: InputEventKind
    key: str | None = None
    text: str | None = None
    x: int | None = None
    y: int | None = None
    button: str | None = None


class InputStatus(Enum):
    """The outcome of a `send_input` attempt (R5, R19)."""

    ok = "ok"
    """Every event was dispatched."""

    unavailable = "unavailable"
    """Synthetic input cannot be attempted here at all -- most notably Wayland,
    which blocks it by design (R5, R19) and must never even be tried."""

    failed = "failed"
    """Dispatch was attempted and errored partway through."""


@dataclass(frozen=True)
class InputResult:
    """The tagged outcome of `send_input` (T053: "reports unavailable, never raises")."""

    status: InputStatus
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is not InputStatus.ok and not self.reason:
            raise ValueError(f"InputResult.status is {self.status!r} but no reason was given")


@dataclass(frozen=True)
class DiskSpace:
    """Free/total bytes at a path, for the R17 disk-headroom guard."""

    path: Path
    free_bytes: int
    total_bytes: int


# --------------------------------------------------------------------------
# The port
# --------------------------------------------------------------------------


@runtime_checkable
class HostPlatform(Protocol):
    """The one seam through which the harness ever touches an operating system.

    One method per capability research R19 names, plus the capture
    capability's own precondition preflight (`check_capture_preconditions`,
    T249). Later waves
    (observe/, saves/, resilience/, run/ preflight) bind to this Protocol
    and must never import `host.windows`, `host.macos`, or `host.linux`
    directly -- construct an adapter once, through
    `civsim_harness.host.factory.get_host_platform`, and pass it down as
    this type.
    """

    def locate_game_process(self) -> GameProcess | None:
        """Find the running Civ VI client process, or `None` if it is not running."""
        ...

    def find_game_window(self, process: GameProcess) -> GameWindow | None:
        """Find that process's own game window, or `None` if it has none (yet).

        May raise a `civsim_harness.errors.PreflightError` when window
        identity cannot be resolved *at all* on this host -- e.g. a required
        optional platform dependency is not installed. That is a setup
        problem, not "the window does not exist right now", which is why it
        is distinguished from the plain `None` return.
        """
        ...

    def capture_window(self, window: GameWindow) -> CaptureResult:
        """Capture that window's own content (R6). Never raises for an unavailable
        or failed capture path -- see `CaptureResult`."""
        ...

    def check_capture_preconditions(self) -> CapturePreconditionResult:
        """Cheap per-capture preflight of this platform's capture-hygiene preconditions (T249).

        Called by the capture path (`observe/capture.py`) BEFORE every
        `capture_window` attempt. A non-passing result means this
        platform/session cannot currently produce a window-scoped,
        hygiene-eligible frame: the caller must not take a frame at all --
        not take-and-discard one -- and must record the step's capture as
        withheld with `reason`, through exactly the same degradation path
        any other host capture failure takes. Never raises: like
        `CaptureResult` and `InputResult`, the outcome is a tagged value.

        **Passing NEVER substitutes for the R6 capture-hygiene spike.**
        T099's rule stands untouched: an image may reach the agent only on
        a run whose `host_support_tier` is VALIDATED -- earned by this
        platform-and-session's own recorded R6 spike at preparation and
        enforced at the moment of attachment (`run/decision_loop.py`). This
        method is the cheaper, per-capture check layered under that gate:
        it can only take a capture away, never grant one, so a platform
        whose preconditions pass but whose spike has not passed still
        attaches nothing.

        An adapter with nothing cheaply checkable today must say so rather
        than fabricate a check: return a passing result whose `reason`
        records exactly that (see `CapturePreconditionResult` -- the reason
        is structurally required in both directions). An honest no-op is
        acceptable; a silent hard-coded pass is not.
        """
        ...

    def resolve_game_directories(self, *, home: Path | None = None) -> GameDirectories:
        """Resolve the platform's save directory and `AppOptions.txt` path (R1, R5).

        `home` overrides the resolved home directory and exists purely for
        deterministic testing (T053); real callers never pass it.
        """
        ...

    def send_input(self, events: Sequence[InputEvent]) -> InputResult:
        """Dispatch synthetic input for the bespoke save/load dialog path (R5).

        Never raises for an unavailable or failed dispatch -- see
        `InputResult`. On Wayland this must report `unavailable` without
        even attempting to post an event (R19).
        """
        ...

    def focus_window(self, window: GameWindow) -> InputResult:
        """Give `window` the input focus, so `send_input` reaches the game (T248).

        Synthetic input has **no window targeting**: X11's XTest, and the
        equivalent on other platforms, delivers to whatever holds focus at
        that moment. A caller that dispatches a keystroke without first
        taking focus is not sending it to the game -- it is sending it to
        whatever the operator happens to be looking at, which on a live
        host may be their browser.

        So every `send_input` consumer must call this first and honour a
        non-`ok` result by not pressing. That is the whole reason this
        operation is on the port rather than left as a documented
        precondition: a precondition that is merely written down is the
        "works in isolation, targets nothing in production" defect class
        this project keeps finding, and a port method is checkable.

        Never raises: like `send_input`, the outcome is a tagged
        `InputResult`. Platforms that cannot focus a window report
        `unavailable` with a reason rather than silently doing nothing --
        a no-op that returns `ok` would make the caller believe the
        keystroke is aimed at the game.
        """
        ...

    def free_disk_space(self, path: Path) -> DiskSpace:
        """Report free/total bytes at `path`, for the R17 disk-headroom guard."""
        ...
