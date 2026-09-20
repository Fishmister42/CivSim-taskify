"""macOS host adapter (T051).

- **Window identity**: Quartz's window list (`CGWindowListCopyWindowInfo`).
- **Capture**: ScreenCaptureKit with an `SCContentFilter` scoped to the
  single game window is rank 1 per research R6; the legacy
  `CGWindowListCreateImage` is the documented fallback. Missing **Screen
  Recording** is detected with `CGPreflightScreenCaptureAccess` *before*
  either path is attempted and reported as an actionable `CaptureResult`
  (T051), never as a raw exception from the capture call.
- **Input**: `CGEvent` via Quartz Event Services. Missing **Accessibility**
  is detected with `AXIsProcessTrusted` before posting anything, reported
  the same way through `InputResult` (T051).
- **Directories**: `~/Library/Application Support/Sid Meier's Civilization
  VI/...` (research R1, R5 -- see the discrepancy note in
  `resolve_game_directories`).

**Known gap, reported rather than papered over**: `pyproject.toml`'s
`macos` optional-dependency group declares `pyobjc-framework-Quartz` and
`pyobjc-framework-Cocoa`, but not `pyobjc-framework-ScreenCaptureKit`. The
rank-1 capture path below therefore always falls through to the legacy
fallback today, everywhere, not only on this development host -- adding
that dependency is outside this file's ownership (`host/**` only) and is
called out in this wave's report instead of edited in here.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from civsim_harness.errors import PreflightError
from civsim_harness.host._shared import locate_process_by_names, read_disk_space
from civsim_harness.host.port import (
    CaptureFrame,
    CaptureResult,
    CaptureStatus,
    DiskSpace,
    GameDirectories,
    GameProcess,
    GameWindow,
    InputEvent,
    InputEventKind,
    InputResult,
    InputStatus,
    WindowRect,
)

# UNVERIFIED: exact process name as it appears for the binary inside the
# .app bundle's Contents/MacOS/ -- psutil reports that binary's own name
# (CFBundleExecutable), which need not match the bundle's display name
# ("Sid Meier's Civilization VI.app") or the Windows executable name at
# all. "Civ6" is listed alongside "CivilizationVI" because the Linux
# adapter's process name was live-corrected from "CivilizationVI" (the
# Windows name, copied in by mistake) to "Civ6", the name Aspyr's own
# published Linux binary actually uses; since Aspyr also publishes the
# macOS port, "Civ6" is at least as plausible here as the Windows-style
# name, and no machine in this repo can execute this adapter to confirm
# either one. Both are kept rather than guessing a single name (the same
# fix class as the Linux correction). There is no bundle-identifier-based
# matching in this adapter -- only this process-name list, consumed by
# psutil -- so there is nothing else to audit on that axis; if a
# bundle-identifier lookup is added later it needs the same treatment.
_PROCESS_NAMES = ("CivilizationVI", "Civ6")

# UNVERIFIED: virtual keycodes below cover only the keys the bespoke save
# dialog needs (R5: Escape, Enter, Tab); values are the long-documented
# Carbon `kVK_*` constants, not exercised on real hardware from this repo.
_VIRTUAL_KEYCODES: dict[str, int] = {
    "enter": 0x24,
    "return": 0x24,
    "esc": 0x35,
    "escape": 0x35,
    "tab": 0x30,
}


class MacOSHostPlatform:
    """`HostPlatform` adapter for native macOS (research R19, R6, R1, R5)."""

    def locate_game_process(self) -> GameProcess | None:
        return locate_process_by_names(_PROCESS_NAMES)

    def find_game_window(self, process: GameProcess) -> GameWindow | None:
        try:
            import Quartz
        except ImportError as exc:
            raise PreflightError(
                "pyobjc-framework-Quartz is not installed; install the 'macos' extra "
                "(`uv sync --extra macos`) to resolve window identity on macOS.",
                detail={"missing_dependency": "pyobjc-framework-Quartz"},
            ) from exc

        # UNVERIFIED: constant names and the window-info dict's key shapes
        # are as documented for `CGWindowListCopyWindowInfo`, but this call
        # has not been exercised on real hardware from this repo.
        window_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        for info in window_list:
            owner_pid = info.get("kCGWindowOwnerPID")
            if owner_pid != process.pid:
                continue
            bounds = info.get("kCGWindowBounds")
            if not bounds:
                continue
            title = str(info.get("kCGWindowName") or info.get("kCGWindowOwnerName") or "")
            rect = WindowRect(
                left=int(bounds["X"]),
                top=int(bounds["Y"]),
                width=int(bounds["Width"]),
                height=int(bounds["Height"]),
            )
            window_number = int(info.get("kCGWindowNumber", 0))
            return GameWindow(handle=window_number, title=title, rect=rect, pid=process.pid)
        return None

    def capture_window(self, window: GameWindow) -> CaptureResult:
        preflight_failure = self._preflight_screen_recording()
        if preflight_failure is not None:
            return preflight_failure

        try:
            frame = self._capture_via_screencapturekit(window)
        except ImportError:
            frame = None
        except Exception as exc:
            return CaptureResult(
                status=CaptureStatus.failed, reason=f"ScreenCaptureKit attempt raised: {exc}"
            )
        if frame is not None:
            return CaptureResult(status=CaptureStatus.ok, frame=frame)

        try:
            return self._capture_via_legacy_window_image(window)
        except ImportError as exc:
            return CaptureResult(
                status=CaptureStatus.unavailable, reason=f"No macOS capture path available: {exc}"
            )
        except Exception as exc:
            return CaptureResult(
                status=CaptureStatus.failed, reason=f"CGWindowListCreateImage failed: {exc}"
            )

    def _preflight_screen_recording(self) -> CaptureResult | None:
        try:
            import Quartz
        except ImportError:
            return CaptureResult(
                status=CaptureStatus.unavailable,
                reason=(
                    "pyobjc-framework-Quartz is not installed; cannot even check Screen "
                    "Recording permission. Install the 'macos' extra (`uv sync --extra macos`)."
                ),
            )
        # UNVERIFIED: `CGPreflightScreenCaptureAccess` is a CoreGraphics
        # function available on macOS 10.15+, used here as a
        # non-prompting check; the exact pyobjc binding surface for it has
        # not been confirmed on real hardware from this repo (T051).
        preflight_fn = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight_fn is not None and not preflight_fn():
            return CaptureResult(
                status=CaptureStatus.unavailable,
                reason=(
                    "Screen Recording permission has not been granted to this process. "
                    "Grant it in System Settings -> Privacy & Security -> Screen Recording, "
                    "then restart the harness (T051 actionable preflight failure)."
                ),
            )
        return None

    def _capture_via_screencapturekit(self, window: GameWindow) -> CaptureFrame | None:
        # UNVERIFIED in full, and currently unreachable: see this module's
        # docstring for the missing `pyobjc-framework-ScreenCaptureKit`
        # dependency. ScreenCaptureKit's own capture APIs are also
        # completion-handler-based Objective-C, which needs care to call
        # synchronously from Python; a real implementation would bridge
        # `SCContentFilter`'s `initWithDesktopIndependentWindow:` selector
        # (pyobjc convention: `initWithDesktopIndependentWindow_`) and
        # `SCScreenshotManager`'s capture call, neither of which this repo
        # could confirm without running on macOS.
        import ScreenCaptureKit  # noqa: F401

        raise NotImplementedError(
            "ScreenCaptureKit bridging is not implemented pending the R6 capture-hygiene "
            "spike and the missing pyobjc-framework-ScreenCaptureKit dependency."
        )

    def _capture_via_legacy_window_image(self, window: GameWindow) -> CaptureResult:
        # Rank fallback named directly in research R6: "Legacy
        # CGWindowListCreateImage is the fallback."
        import Quartz

        # UNVERIFIED: exact constant names
        # (`kCGWindowImageBoundsIgnoreFraming`) and the resulting
        # `CGImageRef` -> raw-bytes extraction
        # (`CGImageGetDataProvider`/`CGDataProviderCopyData`) have not been
        # exercised on real hardware from this repo.
        image_ref = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow,
            window.handle,
            Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
        if image_ref is None:
            return CaptureResult(
                status=CaptureStatus.unavailable, reason="CGWindowListCreateImage returned null"
            )
        return CaptureResult(
            status=CaptureStatus.failed,
            reason=(
                "CGWindowListCreateImage produced an image but pixel extraction "
                "(CGImageGetDataProvider) is not implemented"
            ),
        )

    def send_input(self, events: Sequence[InputEvent]) -> InputResult:
        try:
            import Quartz
        except ImportError as exc:
            return InputResult(
                status=InputStatus.unavailable,
                reason=(
                    "pyobjc-framework-Quartz is not installed; install the 'macos' extra "
                    f"to enable CGEvent input: {exc}"
                ),
            )

        # UNVERIFIED: `AXIsProcessTrusted` lives in ApplicationServices,
        # exposed through pyobjc as `Quartz.AXIsProcessTrusted` on recent
        # pyobjc versions -- this repo could not confirm that binding path
        # without running on macOS.
        is_trusted_fn = getattr(Quartz, "AXIsProcessTrusted", None)
        if is_trusted_fn is not None and not is_trusted_fn():
            return InputResult(
                status=InputStatus.unavailable,
                reason=(
                    "Accessibility permission has not been granted to this process. "
                    "Grant it in System Settings -> Privacy & Security -> Accessibility, "
                    "then restart the harness (T051 actionable preflight failure)."
                ),
            )

        try:
            for event in events:
                self._post_one(Quartz, event)
        except Exception as exc:
            return InputResult(status=InputStatus.failed, reason=f"CGEvent post failed: {exc}")
        return InputResult(status=InputStatus.ok)

    @staticmethod
    def _post_one(quartz: Any, event: InputEvent) -> None:
        if event.kind in (
            InputEventKind.key_press,
            InputEventKind.key_down,
            InputEventKind.key_up,
        ) and event.key:
            code = _VIRTUAL_KEYCODES.get(event.key.lower())
            if code is None:
                # UNVERIFIED / documented gap: single-character virtual
                # keycode lookup is not implemented (it depends on the
                # active keyboard layout via `UCKeyTranslate`); only the
                # named keys above are supported.
                return
            if event.kind in (InputEventKind.key_press, InputEventKind.key_down):
                key_down = quartz.CGEventCreateKeyboardEvent(None, code, True)
                quartz.CGEventPost(quartz.kCGHIDEventTap, key_down)
            if event.kind in (InputEventKind.key_press, InputEventKind.key_up):
                key_up = quartz.CGEventCreateKeyboardEvent(None, code, False)
                quartz.CGEventPost(quartz.kCGHIDEventTap, key_up)
        elif (
            event.kind is InputEventKind.mouse_move and event.x is not None and event.y is not None
        ):
            point = quartz.CGPointMake(event.x, event.y)
            button = quartz.kCGMouseButtonLeft
            move = quartz.CGEventCreateMouseEvent(None, quartz.kCGEventMouseMoved, point, button)
            quartz.CGEventPost(quartz.kCGHIDEventTap, move)
        elif (
            event.kind is InputEventKind.mouse_click and event.x is not None and event.y is not None
        ):
            point = quartz.CGPointMake(event.x, event.y)
            button = quartz.kCGMouseButtonLeft
            down = quartz.CGEventCreateMouseEvent(None, quartz.kCGEventLeftMouseDown, point, button)
            up = quartz.CGEventCreateMouseEvent(None, quartz.kCGEventLeftMouseUp, point, button)
            quartz.CGEventPost(quartz.kCGHIDEventTap, down)
            quartz.CGEventPost(quartz.kCGHIDEventTap, up)

    def resolve_game_directories(self, *, home: Path | None = None) -> GameDirectories:
        # Deliberately no existence check anywhere in this method (T077/T078
        # audit): the R5 spike confirmed on Linux that `Saves/Single/` does
        # not exist until the first save creates it, and a preflight probe
        # that required it to pre-exist would false-negative on a fresh
        # install. Pure path construction below has the same property here.
        root = (home if home is not None else Path.home()) / "Library" / "Application Support"
        # NOTE / discrepancy, reported rather than silently reconciled:
        # research.md's own R1 and R5 disagree on this directory's shape.
        # R1 (AppOptions.txt, for enabling the tuner) nests an extra
        # "Firaxis Games" segment; R5 (the Saves directory) does not. Both
        # are implemented exactly as their respective research finding
        # states -- flagged here for whoever runs the R5/R19 spike to
        # confirm on real hardware.
        app_options_path = (
            root
            / "Sid Meier's Civilization VI"
            / "Firaxis Games"
            / "Sid Meier's Civilization VI"
            / "AppOptions.txt"
        )
        game_root = root / "Sid Meier's Civilization VI"
        saves_dir = game_root / "Sid Meier's Civilization VI" / "Saves" / "Single"
        return GameDirectories(saves_dir=saves_dir, app_options_path=app_options_path)

    def free_disk_space(self, path: Path) -> DiskSpace:
        return read_disk_space(path)
