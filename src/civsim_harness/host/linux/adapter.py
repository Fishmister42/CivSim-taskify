"""Linux host adapter (T052).

Session-type-dependent, per research R19/R5:

- **X11**: window identity via EWMH (`_NET_CLIENT_LIST`/`_NET_WM_PID`) and
  `XComposite` redirected-pixmap capture (research R6) through
  `python-xlib`; input via `XTest`.
- **Wayland**: window identity is not resolvable generically (there is no
  compositor-independent enumeration API and this repo depends on none of
  the compositor-specific protocols that would provide one). Capture
  attempts `xdg-desktop-portal` ScreenCast. **Synthetic input is reported
  `unavailable` without ever being attempted** -- the compositor blocks it
  by design (T052, research R5, R19), so there is nothing to try.

Directories: `~/.local/share/aspyr-media/Sid Meier's Civilization VI/...`
(research R1, R5 -- consistent between the two, unlike macOS).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from civsim_harness.errors import PreflightError
from civsim_harness.host._shared import locate_process_by_names, read_disk_space
from civsim_harness.host.detect import LinuxSessionType
from civsim_harness.host.port import (
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

# VERIFIED on a live Aspyr client (Steam app 289070, build 1.0.12.9): the
# process is `Civ6`. `CivilizationVI` is the Windows name and matches nothing
# here, so process detection found no client at all before this was corrected.
_PROCESS_NAMES = ("Civ6",)

# UNVERIFIED: X keysyms below cover only the keys the bespoke save dialog
# needs (R5: Escape, Enter, Tab); `xtest.fake_input` itself is a stable,
# well-documented python-xlib API, but this repo could not exercise the
# X11 protocol on real hardware.
_KEYSYMS: dict[str, int] = {
    "enter": 0xFF0D,
    "return": 0xFF0D,
    "esc": 0xFF1B,
    "escape": 0xFF1B,
    "tab": 0xFF09,
}

_WAYLAND_INPUT_UNAVAILABLE_REASON = (
    "Synthetic input is not attempted on Wayland: the compositor blocks one client "
    "from synthesising input into another by design (research R5, R19). An X11 "
    "session is the documented workaround."
)


class LinuxHostPlatform:
    """`HostPlatform` adapter for native Linux, X11 or Wayland (research R19, R6, R5)."""

    def __init__(self, *, session_type: LinuxSessionType) -> None:
        self._session_type = session_type

    def locate_game_process(self) -> GameProcess | None:
        return locate_process_by_names(_PROCESS_NAMES)

    def find_game_window(self, process: GameProcess) -> GameWindow | None:
        if self._session_type is LinuxSessionType.wayland:
            # UNVERIFIED / documented gap: there is no portable,
            # dependency-free way to enumerate another application's
            # top-level windows on Wayland from outside it; that needs a
            # compositor-specific protocol (e.g.
            # wlr-foreign-toplevel-management), which is not a declared
            # dependency of this repo (pyproject.toml's `linux` extra has
            # only python-xlib and dbus-python). Reporting "not found"
            # here rather than fabricating a lookup that cannot work.
            return None

        try:
            from Xlib import X
            from Xlib.display import Display
        except ImportError as exc:
            raise PreflightError(
                "python-xlib is not installed; install the 'linux' extra "
                "(`uv sync --extra linux`) to resolve window identity on X11.",
                detail={"missing_dependency": "python-xlib"},
            ) from exc

        # VERIFIED on real hardware (X11/Cinnamon, Mutter/Muffin): reading
        # `_NET_CLIENT_LIST` off the root and matching `_NET_WM_PID` resolves
        # the live client correctly, and the python-xlib calls below are right
        # as written.
        display = Display()
        try:
            root = display.screen().root
            net_client_list = display.intern_atom("_NET_CLIENT_LIST")
            net_wm_pid = display.intern_atom("_NET_WM_PID")
            net_wm_name = display.intern_atom("_NET_WM_NAME")
            client_list_prop = root.get_full_property(net_client_list, X.AnyPropertyType)
            if client_list_prop is None:
                return None
            for window_id in client_list_prop.value:
                candidate = display.create_resource_object("window", window_id)
                pid_prop = candidate.get_full_property(net_wm_pid, X.AnyPropertyType)
                if pid_prop is None or not pid_prop.value or pid_prop.value[0] != process.pid:
                    continue
                geometry = candidate.get_geometry()
                name_prop = candidate.get_full_property(net_wm_name, X.AnyPropertyType)
                title = bytes(name_prop.value).decode("utf-8", "replace") if name_prop else ""
                # A reparenting WM makes get_geometry() report coordinates
                # relative to the WM frame, which is (0, 0) for a managed
                # window -- not the screen position. Translating the window
                # origin into root space is what yields absolute coordinates,
                # and on a multi-monitor desktop the difference is the whole
                # offset of the monitor the client is on.
                left, top = int(geometry.x), int(geometry.y)
                # python-xlib's translate_coords is invoked on the DESTINATION
                # window: root.translate_coords(src, x, y) maps src's (x, y)
                # into root space. Calling it the other way round returns the
                # offset negated, which looks plausible and is wrong.
                try:
                    origin = root.translate_coords(candidate, 0, 0)
                    left, top = int(origin.x), int(origin.y)
                except Exception:  # pragma: no cover - server-dependent
                    pass
                rect = WindowRect(
                    left=left,
                    top=top,
                    width=int(geometry.width),
                    height=int(geometry.height),
                )
                return GameWindow(handle=int(window_id), title=title, rect=rect, pid=process.pid)
            return None
        finally:
            display.close()

    def capture_window(self, window: GameWindow) -> CaptureResult:
        if self._session_type is LinuxSessionType.wayland:
            try:
                return self._capture_via_portal(window)
            except Exception as exc:
                return CaptureResult(
                    status=CaptureStatus.unavailable,
                    reason=(
                        "xdg-desktop-portal ScreenCast is unavailable or was not "
                        f"interactively granted for this session: {exc}"
                    ),
                )

        try:
            return self._capture_via_xcomposite(window)
        except ImportError as exc:
            return CaptureResult(
                status=CaptureStatus.unavailable,
                reason=f"python-xlib is not installed; install the 'linux' extra: {exc}",
            )
        except Exception as exc:
            return CaptureResult(
                status=CaptureStatus.failed, reason=f"XComposite capture failed: {exc}"
            )

    def _capture_via_xcomposite(self, window: GameWindow) -> CaptureResult:
        # UNVERIFIED in full: python-xlib's `Xlib.ext.composite` module
        # exposes the Composite extension's requests, but this repo could
        # not confirm on real hardware whether its coverage extends to
        # `NameWindowPixmap` (needed to get a pixmap handle for the
        # redirected window) and the subsequent `XGetImage`-on-pixmap +
        # pixel-format handling needed to produce `CaptureFrame` bytes.
        # What is implemented below is the redirect request itself and the
        # extension-presence check; pixmap readback is the documented gap.
        from Xlib.display import Display
        from Xlib.ext import composite

        display = Display()
        try:
            if not display.has_extension("Composite"):
                return CaptureResult(
                    status=CaptureStatus.unavailable,
                    reason="X server has no Composite extension; XComposite capture needs it",
                )
            xwindow = display.create_resource_object("window", window.handle)
            composite.redirect_window(xwindow, composite.RedirectAutomatic)
            return CaptureResult(
                status=CaptureStatus.failed,
                reason=(
                    "Window redirected via XComposite but pixmap readback "
                    "(NameWindowPixmap + XGetImage) is not implemented"
                ),
            )
        finally:
            display.close()

    def _capture_via_portal(self, window: GameWindow) -> CaptureResult:
        # UNVERIFIED in full: driving `org.freedesktop.portal.ScreenCast`
        # (CreateSession / SelectSources / Start over D-Bus, then reading
        # frames from the PipeWire node it hands back) needs an
        # interactive permission grant per research R6 -- "hostile to
        # unattended runs" -- and this repo could not exercise the exact
        # `dbus-python` call sequence on real hardware. `dbus-python`'s
        # classic main-loop integration is also a poor fit for this
        # project's asyncio event loop (R1), which is a design question
        # for whoever completes this path, not just an API-detail one.
        import dbus  # noqa: F401

        raise NotImplementedError(
            "xdg-desktop-portal ScreenCast is not implemented: it needs an interactive "
            "grant and a dbus-python<->asyncio integration strategy this repo has not "
            "settled (research R6)."
        )

    def resolve_game_directories(self, *, home: Path | None = None) -> GameDirectories:
        root = (
            (home if home is not None else Path.home())
            / ".local"
            / "share"
            / "aspyr-media"
            / "Sid Meier's Civilization VI"
        )
        return GameDirectories(
            saves_dir=root / "Saves" / "Single", app_options_path=root / "AppOptions.txt"
        )

    def send_input(self, events: Sequence[InputEvent]) -> InputResult:
        if self._session_type is LinuxSessionType.wayland:
            # Reported unavailable without being attempted at all (T052):
            # the compositor blocks synthetic input by design.
            return InputResult(
                status=InputStatus.unavailable, reason=_WAYLAND_INPUT_UNAVAILABLE_REASON
            )

        try:
            from Xlib import X
            from Xlib.display import Display
            from Xlib.ext import xtest
        except ImportError as exc:
            return InputResult(
                status=InputStatus.unavailable,
                reason=f"python-xlib is not installed; install the 'linux' extra: {exc}",
            )

        display = Display()
        try:
            if not display.has_extension("XTEST"):
                return InputResult(
                    status=InputStatus.unavailable, reason="X server has no XTEST extension"
                )
            key_kinds = (InputEventKind.key_press, InputEventKind.key_down, InputEventKind.key_up)
            for event in events:
                has_xy = event.x is not None and event.y is not None
                if event.kind in key_kinds and event.key:
                    keysym = _KEYSYMS.get(event.key.lower())
                    if keysym is None:
                        continue
                    keycode = display.keysym_to_keycode(keysym)
                    if event.kind in (InputEventKind.key_press, InputEventKind.key_down):
                        xtest.fake_input(display, X.KeyPress, keycode)
                    if event.kind in (InputEventKind.key_press, InputEventKind.key_up):
                        xtest.fake_input(display, X.KeyRelease, keycode)
                elif event.kind is InputEventKind.mouse_move and has_xy:
                    xtest.fake_input(display, X.MotionNotify, 0, x=event.x, y=event.y)
                elif event.kind is InputEventKind.mouse_click and has_xy:
                    xtest.fake_input(display, X.MotionNotify, 0, x=event.x, y=event.y)
                    xtest.fake_input(display, X.ButtonPress, 1)
                    xtest.fake_input(display, X.ButtonRelease, 1)
            display.sync()
        except Exception as exc:
            return InputResult(status=InputStatus.failed, reason=f"XTest dispatch failed: {exc}")
        finally:
            display.close()
        return InputResult(status=InputStatus.ok)

    def free_disk_space(self, path: Path) -> DiskSpace:
        return read_disk_space(path)
