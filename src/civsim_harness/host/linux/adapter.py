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

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from civsim_harness.errors import PreflightError
from civsim_harness.host._shared import locate_process_by_names, read_disk_space
from civsim_harness.host.detect import LinuxSessionType
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
    InputEventKind,
    InputResult,
    InputStatus,
    WindowRect,
)

# VERIFIED on a live Aspyr client (Steam app 289070, build 1.0.12.9): the
# process is `Civ6`. `CivilizationVI` is the Windows name and matches nothing
# here, so process detection found no client at all before this was corrected.
_PROCESS_NAMES = ("Civ6",)

# VERIFIED against a live XTest server: `xtest.fake_input` through python-xlib
# delivers these as events with `synthetic NO` -- i.e. they arrive as real
# device input, not as `XSendEvent` fakes that an application is free to
# filter out. That distinction matters for a game client.
#
# The original table held only enter/return/esc/tab (R5's save-dialog minimum)
# and every other key name was silently dropped while `send_input` still
# returned `ok`. It is widened here because a dialog needs more than three
# keys, and because "unknown key" is now a reported failure rather than a
# no-op -- a narrow table would turn ordinary requests into hard errors.
_KEYSYMS: dict[str, int] = {
    "enter": 0xFF0D,
    "return": 0xFF0D,
    "esc": 0xFF1B,
    "escape": 0xFF1B,
    "tab": 0xFF09,
    "space": 0x0020,
    "backspace": 0xFF08,
    "delete": 0xFFFF,
    "home": 0xFF50,
    "end": 0xFF57,
    "pageup": 0xFF55,
    "pagedown": 0xFF56,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "shift": 0xFFE1,
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "alt": 0xFFE9,
    **{f"f{n}": 0xFFBE + (n - 1) for n in range(1, 13)},
}

# XTest button numbers. `button` was previously ignored entirely, so every
# click dispatched as button 1 regardless of what was asked for.
_BUTTONS: dict[str, int] = {
    "left": 1,
    "middle": 2,
    "right": 3,
    "scroll_up": 4,
    "scroll_down": 5,
}

_SHIFT_KEYSYM = 0xFFE1

_WAYLAND_INPUT_UNAVAILABLE_REASON = (
    "Synthetic input is not attempted on Wayland: the compositor blocks one client "
    "from synthesising input into another by design (research R5, R19). An X11 "
    "session is the documented workaround."
)

# Compositors offer to *unredirect* a fullscreen window -- handing it the
# scanout buffer directly for performance. That is exactly the case this
# capture path cannot survive: an unredirected window has no maintained
# off-screen pixmap, so NameWindowPixmap either fails or yields a frame that
# silently stops updating. The schema differs per desktop, so each candidate
# is asked in turn and the first that answers wins.
_UNREDIRECT_SCHEMA_KEYS: tuple[tuple[str, str], ...] = (
    ("org.cinnamon.muffin", "unredirect-fullscreen-windows"),
    ("org.gnome.mutter", "unredirect-fullscreen-windows"),
)


def _read_unredirect_fullscreen_windows() -> bool | None:
    """Return the compositor's unredirect-fullscreen setting, or `None` if unknown.

    `None` is a real answer, not a failure: a desktop that does not publish
    this key has not been shown to be safe *or* unsafe, and preflight should
    say so rather than assume the benign value.
    """
    if shutil.which("gsettings") is None:
        return None
    for schema, key in _UNREDIRECT_SCHEMA_KEYS:
        probe = subprocess.run(  # noqa: S603
            ["gsettings", "get", schema, key],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            answer = probe.stdout.strip().lower()
            if answer in ("true", "false"):
                return answer == "true"
    return None


def _input_rejected(index: int, event: InputEvent, problem: str) -> InputResult:
    """Fail the batch loudly rather than dropping an event and reporting `ok`.

    `send_input` previously fell through several paths that did nothing at all
    and still returned `ok`. Silent input loss is worse than a failure: the
    harness records that it acted, the game never saw the keystroke, and the
    resulting divergence gets attributed to the client.
    """
    return InputResult(
        status=InputStatus.failed,
        reason=(
            f"Event {index} ({event.kind.value}) {problem}. No further events in this batch "
            f"were dispatched; events before it were."
        ),
    )


def _type_text(display: object, xtest: object, x_const: object, text: str) -> str | None:
    """Type `text` as real keystrokes. Returns `None` on success, else a reason.

    Each character is resolved to a keysym, then to a keycode, and the server's
    own keyboard mapping decides whether Shift is required -- rather than
    assuming a US layout, where an operator on a different layout would get
    silently wrong characters typed into a save dialog.
    """
    for position, character in enumerate(text):
        keysym = ord(character)
        keycode = display.keysym_to_keycode(keysym)  # type: ignore[attr-defined]
        if keycode == 0:
            return (
                f"character {character!r} (position {position}) has no keycode in this X "
                "server's keyboard mapping"
            )

        # Which shift level carries this keysym on this layout?
        unshifted = display.keycode_to_keysym(keycode, 0)  # type: ignore[attr-defined]
        shifted = display.keycode_to_keysym(keycode, 1)  # type: ignore[attr-defined]
        if unshifted == keysym:
            needs_shift = False
        elif shifted == keysym:
            needs_shift = True
        else:
            return (
                f"character {character!r} (position {position}) resolves to keycode "
                f"{keycode}, which carries neither the unshifted nor the shifted keysym; "
                "this adapter will not guess at a modifier combination"
            )

        shift_code = display.keysym_to_keycode(_SHIFT_KEYSYM)  # type: ignore[attr-defined]
        if needs_shift:
            xtest.fake_input(display, x_const.KeyPress, shift_code)  # type: ignore[attr-defined]
        xtest.fake_input(display, x_const.KeyPress, keycode)  # type: ignore[attr-defined]
        xtest.fake_input(display, x_const.KeyRelease, keycode)  # type: ignore[attr-defined]
        if needs_shift:
            xtest.fake_input(display, x_const.KeyRelease, shift_code)  # type: ignore[attr-defined]
    return None


@dataclass(frozen=True)
class CapturePreconditions:
    """Whether this display can produce window-scoped frames at all (research R6)."""

    composite_extension: bool
    compositing_manager: bool
    unredirect_fullscreen_windows: bool | None
    detail: str | None = None

    @property
    def can_capture(self) -> bool:
        """True only when a window-scoped frame is actually obtainable."""
        return self.composite_extension and self.compositing_manager

    @property
    def warnings(self) -> tuple[str, ...]:
        """Conditions that do not block capture but will corrupt it if they change."""
        issues: list[str] = []
        if self.unredirect_fullscreen_windows is True:
            issues.append(
                "The compositor is configured to unredirect fullscreen windows "
                "(unredirect-fullscreen-windows=true). A fullscreen client will lose its "
                "off-screen pixmap, and capture will fail or silently freeze on a stale "
                "frame. Set it to false, or run the client windowed."
            )
        elif self.unredirect_fullscreen_windows is None:
            issues.append(
                "Could not read unredirect-fullscreen-windows for this desktop, so it is "
                "unknown whether a fullscreen client stays redirected. Verify a real "
                "fullscreen capture before trusting one."
            )
        return tuple(issues)


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

    def check_capture_preconditions(self) -> CapturePreconditionResult:
        """The `HostPlatform` port preflight (T249): `capture_preconditions`' verdict, wired.

        SEAM NOTE -- the Linux peer owns this adapter's live verification.
        This method deliberately adds no probing of its own: it delegates to
        `capture_preconditions`, the live-verified probe (X11 Composite
        extension plus `_NET_WM_CM_Sn` selection ownership, the two
        conditions that actually decide window-scoped capture on Linux), so
        the code that ran against a real compositor is the code the
        production capture path now consults. Non-blocking `warnings` are
        folded into a passing reason rather than dropped. Anything beyond
        this minimal adaptation of the verdict -- including whether the
        Wayland branch should ever pass once the portal path exists --
        belongs to the peer's live half of T249.
        """
        report = self.capture_preconditions()
        if not report.can_capture:
            reason = report.detail or (
                "window-scoped XComposite capture is not obtainable on this display: "
                f"composite_extension={report.composite_extension}, "
                f"compositing_manager={report.compositing_manager}"
            )
            return CapturePreconditionResult(passed=False, reason=reason)
        reason = (
            "Composite extension present and a compositing manager owns this screen's "
            "_NET_WM_CM_Sn selection (the live-verified R6 preconditions)."
        )
        if report.warnings:
            reason += " Warnings: " + " ".join(report.warnings)
        return CapturePreconditionResult(passed=True, reason=reason)

    def capture_preconditions(self) -> CapturePreconditions:
        """Report whether this display can yield window-scoped frames, and why not.

        Since T249 this is the implementation behind the port's
        `check_capture_preconditions` preflight (which adapts the verdict to
        the portable `CapturePreconditionResult`); it remains directly
        callable because the live tests and spikes assert on its full
        per-condition report, and the two conditions below are the ones
        that actually decide window-scoped capture on Linux.

        Deliberately does NOT consult `XDG_SESSION_TYPE`. That variable says
        which session protocol is in use, not whether a compositor is running,
        and "X11 with no compositor" is precisely the case where the only
        thing that would still produce an image is a root/screen grab -- the
        FR-025 parity breach this path must never fall back to.
        """
        if self._session_type is LinuxSessionType.wayland:
            return CapturePreconditions(
                composite_extension=False,
                compositing_manager=False,
                unredirect_fullscreen_windows=None,
                detail="Wayland session: capture goes via xdg-desktop-portal, not XComposite.",
            )

        try:
            from Xlib import X
            from Xlib.display import Display
        except ImportError as exc:
            return CapturePreconditions(
                composite_extension=False,
                compositing_manager=False,
                unredirect_fullscreen_windows=None,
                detail=f"python-xlib is not installed; install the 'linux' extra: {exc}",
            )

        display = Display()
        try:
            has_composite = bool(display.has_extension("Composite"))
            screen_number = display.get_default_screen()
            selection = display.intern_atom(f"_NET_WM_CM_S{screen_number}")
            has_manager = display.get_selection_owner(selection) != X.NONE
        finally:
            display.close()

        return CapturePreconditions(
            composite_extension=has_composite,
            compositing_manager=has_manager,
            unredirect_fullscreen_windows=_read_unredirect_fullscreen_windows(),
            detail=None,
        )

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
        # VERIFIED on real hardware (X11/Cinnamon, Muffin compositing, Composite
        # 0.4, depth-24 TrueColor): the full redirect -> NameWindowPixmap ->
        # GetImage -> BGRA8 path below was measured end to end against live
        # windows, including a 1920x1200 one at ~79 ms / 9.2 MB per frame.
        #
        # Three findings from that measurement drive the shape of this code:
        #
        # 1. `NameWindowPixmap` fails with **BadMatch unless this client has
        #    itself redirected the window**. A running compositing manager is
        #    NOT sufficient: Muffin redirects root's subwindows, and that does
        #    not satisfy the per-window redirect NameWindowPixmap requires.
        #    Measured directly -- naming without redirecting first is BadMatch,
        #    and the identical call after `redirect_window` succeeds.
        # 2. python-xlib delivers X protocol errors **asynchronously**. Both
        #    calls "return" an object even when the server rejects them, and
        #    the failure only surfaces later (as a BadDrawable on the bogus
        #    pixmap id, or as a print from the default error handler). Every
        #    request therefore carries an explicit `CatchError` + `sync()`;
        #    without that this function reports a confident, wrong success.
        # 3. A window gets a **new** off-screen pixmap on every map and every
        #    resize, so the pixmap is named fresh per capture and freed after
        #    rather than cached.
        #
        # FR-025 / Principle I: there is deliberately **no root- or
        # screen-grab fallback** on any branch below. A root-scoped grab of
        # the same region leaks whatever occludes the client (evidenced in
        # `spikes/r6-evidence/root-scoped-same-region-LEAKS.png`), which is a
        # human-parity breach. Failing to capture is the correct outcome; a
        # contaminated frame is not.
        from Xlib import X, error
        from Xlib.display import Display
        from Xlib.ext import composite

        display = Display()
        try:
            if not display.has_extension("Composite"):
                return CaptureResult(
                    status=CaptureStatus.unavailable,
                    reason="X server has no Composite extension; XComposite capture needs it",
                )

            # The correct compositing check is selection ownership of
            # _NET_WM_CM_Sn, NOT `XDG_SESSION_TYPE=x11`: the session can be
            # X11 with no compositor running, in which case a window's
            # off-screen storage is not maintained and the only thing that
            # would still "work" is a screen grab -- the parity breach above.
            screen_number = display.get_default_screen()
            cm_selection = display.intern_atom(f"_NET_WM_CM_S{screen_number}")
            if display.get_selection_owner(cm_selection) == X.NONE:
                return CaptureResult(
                    status=CaptureStatus.unavailable,
                    reason=(
                        f"No compositing manager owns _NET_WM_CM_S{screen_number}. X11 without "
                        "a compositor cannot yield a window-scoped frame, and a root/screen "
                        "grab is not an acceptable substitute (FR-025 human parity)."
                    ),
                )

            xwindow = display.create_resource_object("window", window.handle)
            geometry = xwindow.get_geometry()
            width, height = int(geometry.width), int(geometry.height)
            if width <= 0 or height <= 0:
                return CaptureResult(
                    status=CaptureStatus.failed,
                    reason=f"Window 0x{window.handle:08x} reports a degenerate "
                    f"geometry {width}x{height}",
                )

            redirect_error = error.CatchError()
            composite.redirect_window(xwindow, composite.RedirectAutomatic, onerror=redirect_error)
            display.sync()
            caught = redirect_error.get_error()
            # BadAccess means another client already holds a *manual* redirect
            # on this window. That client's redirect still keeps the off-screen
            # pixmap alive, so naming it is worth attempting rather than
            # failing here.
            if caught is not None and not isinstance(caught, error.BadAccess):
                return CaptureResult(
                    status=CaptureStatus.failed,
                    reason=(
                        "XComposite redirect of window "
                        f"0x{window.handle:08x} was rejected: {type(caught).__name__}"
                    ),
                )

            name_error = error.CatchError()
            pixmap = composite.name_window_pixmap(xwindow, onerror=name_error)
            display.sync()
            caught = name_error.get_error()
            if caught is not None:
                return CaptureResult(
                    status=CaptureStatus.failed,
                    reason=(
                        "NameWindowPixmap was rejected "
                        f"({type(caught).__name__}) for window 0x{window.handle:08x}. "
                        "BadMatch here means the window is unmapped (minimised, or on "
                        "another workspace) or is not redirected, so no off-screen "
                        "pixmap exists to read."
                    ),
                )

            try:
                image = pixmap.get_image(0, 0, width, height, X.ZPixmap, 0xFFFFFFFF)
                data = bytes(image.data)
            finally:
                free_error = error.CatchError()
                pixmap.free(onerror=free_error)
                display.sync()

            pixel_count = width * height
            if len(data) != pixel_count * 4:
                # Anything but 32 bits per pixel (a 16-bit or 15-bit visual)
                # would need a different unpack; report it rather than
                # reinterpreting the bytes and silently producing wrong colour.
                return CaptureResult(
                    status=CaptureStatus.failed,
                    reason=(
                        f"Expected {pixel_count * 4} bytes for a 32-bit {width}x{height} "
                        f"frame but GetImage returned {len(data)}; this visual "
                        f"(depth {image.depth}) is not a supported pixel layout."
                    ),
                )

            # Byte order decides the channel layout, and getting it wrong
            # silently swaps red and blue in every frame the agent ever sees --
            # so it is checked rather than assumed. On LSBFirst (verified here)
            # a depth-24 ZPixmap pixel is B,G,R,pad, which is `BGRA8` to the
            # screening gates; the fourth byte is X padding, not meaningful
            # alpha, and the gates drop it on their conversion to RGB.
            if display.display.info.image_byte_order != 0:
                return CaptureResult(
                    status=CaptureStatus.failed,
                    reason=(
                        "X server reports MSBFirst image byte order; this adapter has only "
                        "been verified against LSBFirst (B,G,R,pad) and will not guess at "
                        "the channel layout."
                    ),
                )

            return CaptureResult(
                status=CaptureStatus.ok,
                frame=CaptureFrame(
                    width=width,
                    height=height,
                    rect=WindowRect(
                        left=window.rect.left,
                        top=window.rect.top,
                        width=width,
                        height=height,
                    ),
                    image_bytes=data,
                    image_format="BGRA8",
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
            for index, event in enumerate(events):
                has_xy = event.x is not None and event.y is not None

                if event.kind in key_kinds:
                    if not event.key:
                        return _input_rejected(index, event, "carries no `key`")
                    keysym = _KEYSYMS.get(event.key.lower())
                    if keysym is None:
                        # Previously this did `continue`: an unmapped key was
                        # silently dropped and the whole batch still reported
                        # `ok`. Measured against a live XTest server -- sending
                        # `F5` delivered zero events and returned success. A
                        # save dialog that never received its keystroke while
                        # the harness recorded "input sent" is precisely the
                        # kind of failure that gets blamed on the game.
                        return _input_rejected(
                            index,
                            event,
                            f"names key {event.key!r}, which this adapter has no keysym for. "
                            f"Known keys: {', '.join(sorted(_KEYSYMS))}",
                        )
                    keycode = display.keysym_to_keycode(keysym)
                    if keycode == 0:
                        return _input_rejected(
                            index,
                            event,
                            f"keysym for {event.key!r} maps to no keycode on this X server",
                        )
                    if event.kind in (InputEventKind.key_press, InputEventKind.key_down):
                        xtest.fake_input(display, X.KeyPress, keycode)
                    if event.kind in (InputEventKind.key_press, InputEventKind.key_up):
                        xtest.fake_input(display, X.KeyRelease, keycode)

                elif event.kind is InputEventKind.text:
                    # `InputEventKind.text` had no branch here at all, so a
                    # text event was dropped and reported `ok` -- verified by
                    # sending "civsim" and observing zero events arrive. It is
                    # the event the bespoke save path most needs, since a save
                    # dialog wants a filename typed into it.
                    if not event.text:
                        return _input_rejected(index, event, "carries no `text`")
                    failure = _type_text(display, xtest, X, event.text)
                    if failure is not None:
                        return _input_rejected(index, event, failure)

                elif event.kind is InputEventKind.mouse_move and has_xy:
                    xtest.fake_input(display, X.MotionNotify, 0, x=event.x, y=event.y)

                elif event.kind is InputEventKind.mouse_click and has_xy:
                    # `button` was ignored outright: a right-click request
                    # delivered button 1. Verified by sending button="right"
                    # and reading `button 1` back off the wire.
                    button = _BUTTONS.get((event.button or "left").lower())
                    if button is None:
                        return _input_rejected(
                            index,
                            event,
                            f"names button {event.button!r}; known buttons: "
                            f"{', '.join(sorted(_BUTTONS))}",
                        )
                    xtest.fake_input(display, X.MotionNotify, 0, x=event.x, y=event.y)
                    xtest.fake_input(display, X.ButtonPress, button)
                    xtest.fake_input(display, X.ButtonRelease, button)

                else:
                    # Falling through used to mean "silently do nothing, report
                    # ok". A mouse event missing its coordinates lands here.
                    return _input_rejected(
                        index, event, "is not a dispatchable combination of kind and fields"
                    )
            display.sync()
        except Exception as exc:
            return InputResult(status=InputStatus.failed, reason=f"XTest dispatch failed: {exc}")
        finally:
            display.close()
        return InputResult(status=InputStatus.ok)

    def free_disk_space(self, path: Path) -> DiskSpace:
        return read_disk_space(path)
