"""Windows host adapter (T050).

- **Window identity**: `pywin32` (`win32gui`, `win32process`).
- **Capture**: Windows.Graphics.Capture (`winsdk`) is rank 1 per research
  R6, attempted first; `PrintWindow` with `PW_RENDERFULLCONTENT` (rank 3)
  is the implemented fallback. Rank 2 (DXGI Desktop Duplication) is not
  implemented -- it is occlusion-*sensitive* per R6, so it is only worth
  building once the hygiene spike shows rank 1 fails and rank 2 is actually
  needed.
- **Directories**: **two roots, not one.** Saves live under
  `%USERPROFILE%\\Documents\\My Games\\<profile>\\Saves\\Single\\`;
  `AppOptions.txt` lives under
  `%LOCALAPPDATA%\\Firaxis Games\\<profile>\\`. T050 specified a single
  Documents base for both, which is wrong for current retail builds --
  see the `_DOCUMENTS_PARTS` note below for the live measurements and for
  the older all-under-Documents layout that also still exists.
- **Input**: `pydirectinput`, a `SendInput` wrapper (research R5).

All optional-dependency imports (`win32gui`, `win32process`, `winsdk`,
`pydirectinput`) are lazy and scoped to the method that needs them, never
at module import time -- constructing `WindowsHostPlatform()` never
imports any of them, so this module loads cleanly even when the `windows`
extra is not installed (research R19's honest-limitation framing: a
missing optional dependency degrades a capability, it does not break
construction).
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from civsim_harness.errors import PreflightError
from civsim_harness.host._shared import locate_process_by_names, read_disk_space
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

# UNVERIFIED: exact Windows executable name/casing shipped by the Steam
# release, and whether `psutil.Process.name()` reports it with or without
# the ".exe" suffix on this host (that has varied historically by how a
# process was spawned). Civ VI has historically offered a DirectX-version
# launch choice with its own executable, so both the default and "_DX12"
# binaries are listed, each with and without the suffix -- the same
# "match a set of plausible candidates, not one confident guess" fix that
# corrected the Linux adapter's process-name bug (that binary turned out
# to be "Civ6", not the Windows name "CivilizationVI" this constant was
# once copied from). `locate_process_by_names` (host/_shared.py) also
# does a case-insensitive prefix match as a second line of defence, so
# this list is deliberately redundant with that rather than relying on it
# alone.
_PROCESS_NAMES = (
    "CivilizationVI.exe",
    "CivilizationVI_DX12.exe",
    "CivilizationVI",
    "CivilizationVI_DX12",
)

# VERIFIED against a live Windows client, 2026-09-20 (build 1.0.12.68
# (1023995), Steam) -- see
# specs/002-civ-playing-harness/spikes/r5-save-path-windows.md.
#
# T050 specified a *single* base directory, `%USERPROFILE%\Documents\My
# Games\Sid Meier's Civilization VI\`, and hung both the saves directory
# and `AppOptions.txt` off it. That is half right, and the half that is
# wrong made `debug_menu_preflight` / `turn_timer_preflight` read a file
# that does not exist on this platform. **Current retail Civilization VI
# on Windows splits its user data across two roots:**
#
#   saves   -> %USERPROFILE%\Documents\My Games\<profile>\Saves\Single\
#   options -> %LOCALAPPDATA%\Firaxis Games\<profile>\AppOptions.txt
#
# Measured on the live host: `Saves\Single\auto\AutoSave_0008.Civ6Save`
# was written 2026-09-16 16:50 under Documents, while `AppOptions.txt`
# (the one carrying `EnableTuner 1`) was written 2026-09-16 15:56 under
# `%LOCALAPPDATA%\Firaxis Games\`. There is **no** `AppOptions.txt`
# anywhere under `Documents\My Games\Sid Meier's Civilization VI\`.
#
# A second, older layout also exists on this machine and must not be
# assumed away: the **Epic** profile directory
# `Documents\My Games\Sid Meier's Civilization VI (Epic)\` keeps
# `AppOptions.txt` *and* `Saves\` together under Documents. That install
# is stale here (last played 2021-05-30) but it is a legitimate layout,
# so both are probed rather than one being hard-coded.
_DOCUMENTS_PARTS = ("Documents", "My Games")
_LOCAL_APPDATA_PARTS = ("AppData", "Local", "Firaxis Games")

#: Profile directory names, in preference order when nothing on disk
#: discriminates. The unsuffixed name is the Steam/retail default.
_PROFILE_NAMES = ("Sid Meier's Civilization VI", "Sid Meier's Civilization VI (Epic)")

#: Escape hatch for a host whose profile this probe guesses wrong (e.g. a
#: storefront variant with a directory name not listed above). Set it to a
#: profile *directory name*, not a full path -- the two roots are still
#: resolved the platform's way around it.
_PROFILE_ENV_VAR = "CIVSIM_CIV6_PROFILE"

# Long-documented, stable Win32 constant (PW_RENDERFULLCONTENT).
_PW_RENDERFULLCONTENT = 0x00000002

# --------------------------------------------------------------------------
# T224 -- GDI pixel extraction (research R6 rank 3's remaining half)
# --------------------------------------------------------------------------

#: `BITMAPINFOHEADER.biCompression` for an uncompressed, packed-pixel DIB.
_BI_RGB = 0

#: `GetDIBits`' `usage` argument: the colour table (unused at 32bpp) holds literal RGB values
#: rather than palette indices.
_DIB_RGB_COLORS = 0

#: What this adapter asks `GetDIBits` for, and what `CaptureFrame.image_format` then names.
#: 32 bits per pixel is chosen over 24 for one concrete reason beyond convenience: a DIB's scan
#: lines are padded to a 4-byte boundary, so at 24bpp a width not divisible by 4 produces a row
#: stride wider than `width * 3` and every row after the first is offset by the accumulated
#: padding -- the classic way this code is got subtly wrong, and invisible on the 1920-wide
#: windows anyone would test with. At 32bpp the stride is `width * 4`, which is inherently
#: 4-byte aligned for every possible width, so the padding case cannot arise at all.
_BITS_PER_PIXEL = 32


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


class _BITMAPINFO(ctypes.Structure):
    _fields_ = (
        ("bmiHeader", _BITMAPINFOHEADER),
        # Unused at 32bpp (there is no palette), but the struct `GetDIBits` writes through is
        # `BITMAPINFO`, not `BITMAPINFOHEADER` -- declaring the trailing colour array keeps the
        # allocation the size the API expects rather than relying on it never touching it.
        ("bmiColors", wintypes.DWORD * 3),
    )


@dataclass(frozen=True)
class _Gdi:
    """The three Win32 libraries this adapter calls, with argument and return types declared.

    **Declaring them is not decoration.** `ctypes` defaults an undeclared function's `restype` to
    `c_int` and passes Python ints as 32-bit -- which silently truncates every HDC, HBITMAP and
    HWND on 64-bit Windows, where handles are 64-bit values. The failure mode is not a clean
    error: it is a call against a handle that is *almost* the right one. Every entry point used
    below is therefore declared, once, here.
    """

    user32: Any
    gdi32: Any


def _gdi() -> _Gdi:
    """Resolve and type-declare the Win32 entry points this adapter uses (cached per process)."""
    global _GDI_CACHE
    if _GDI_CACHE is not None:
        return _GDI_CACHE

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    user32.GetWindowDC.argtypes = (wintypes.HWND,)
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
    user32.PrintWindow.restype = wintypes.BOOL

    gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteDC.argtypes = (wintypes.HDC,)
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = (
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    )
    gdi32.GetDIBits.restype = ctypes.c_int

    _GDI_CACHE = _Gdi(user32=user32, gdi32=gdi32)
    return _GDI_CACHE


_GDI_CACHE: _Gdi | None = None


def extract_bgra8(
    mem_dc: int, bitmap: int, *, width: int, height: int
) -> bytes | None:
    """Read *bitmap*'s pixels out of *mem_dc* as top-down `BGRA8` bytes (T224, FR-050, R6).

    This is the step every Windows capture previously stopped one call short of: `PrintWindow`
    put pixels into a GDI bitmap and nothing ever read them back, so **every** capture on this
    platform returned `failed` and every step was recorded `visually_degraded`.

    Two details carry the whole correctness of this function, and both are the kind that produce
    a plausible-looking-but-wrong image rather than an error:

    **Row order.** A DIB is bottom-up by default -- scan line 0 is the *bottom* of the image. A
    caller that ignores this gets a vertically mirrored frame, which no automated check would
    flag and which would quietly corrupt every downstream parity screening gate that reasons
    about where things are on screen. Passing a **negative** `biHeight` is the documented way to
    request top-down rows, and top-down is what
    `civsim_harness.parity.screening._decode_frame` assumes (it calls `Image.frombuffer(...,
    "raw", raw_mode, 0, 1)`, whose trailing `1` is top-down orientation). The negative height
    here and that `1` there are the same decision, stated at both ends.

    **Stride.** See :data:`_BITS_PER_PIXEL`: asking for 32bpp makes the row stride exactly
    `width * 4`, which is 4-byte aligned for every width, so the scan-line padding a DIB would
    otherwise apply cannot arise. The returned buffer is therefore densely packed and
    `width * height * 4` bytes long, which is exactly what `_decode_frame` expects.

    Returns `None` when `GetDIBits` reports it copied no scan lines -- reported by the caller as
    a failed capture rather than raised, matching `host/port.py`'s "reports unavailable, never
    raises" contract for this path.

    The alpha channel is forced opaque before returning. `PrintWindow` into a 32-bit DIB leaves
    the alpha byte undefined (in practice zero), and while every consumer in this codebase
    converts to RGB and discards it, a buffer labelled `BGRA8` whose alpha says "fully
    transparent" is a misdescription waiting to be believed by the next consumer. One slice
    assignment makes the label true.
    """
    gdi = _gdi()

    info = _BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    # Negative: top-down rows. See this function's docstring.
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = _BITS_PER_PIXEL
    info.bmiHeader.biCompression = _BI_RGB
    info.bmiHeader.biSizeImage = 0
    info.bmiHeader.biXPelsPerMeter = 0
    info.bmiHeader.biYPelsPerMeter = 0
    info.bmiHeader.biClrUsed = 0
    info.bmiHeader.biClrImportant = 0

    stride = width * (_BITS_PER_PIXEL // 8)
    buffer = ctypes.create_string_buffer(stride * height)

    scan_lines = gdi.gdi32.GetDIBits(
        mem_dc,
        bitmap,
        0,
        height,
        ctypes.cast(buffer, ctypes.c_void_p),
        ctypes.byref(info),
        _DIB_RGB_COLORS,
    )
    if scan_lines <= 0:
        return None

    pixels = bytearray(buffer.raw[: stride * height])
    pixels[3::4] = b"\xff" * (width * height)
    return bytes(pixels)


class WindowsHostPlatform:
    """`HostPlatform` adapter for native Windows (research R19, R6, R5)."""

    def locate_game_process(self) -> GameProcess | None:
        return locate_process_by_names(_PROCESS_NAMES)

    def find_game_window(self, process: GameProcess) -> GameWindow | None:
        try:
            import win32gui
            import win32process
        except ImportError as exc:
            raise PreflightError(
                "pywin32 is not installed; install the 'windows' extra "
                "(`uv sync --extra windows`) to resolve window identity on Windows.",
                detail={"missing_dependency": "pywin32"},
            ) from exc

        found_handles: list[int] = []

        def _collect(hwnd: int, _extra: object) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _thread_id, owner_pid = win32process.GetWindowThreadProcessId(hwnd)
            if owner_pid == process.pid and win32gui.GetWindowText(hwnd):
                found_handles.append(hwnd)
            return True

        win32gui.EnumWindows(_collect, None)
        if not found_handles:
            return None

        hwnd = found_handles[0]
        # `GetClientRect` returns the client rectangle in CLIENT coordinates
        # by definition -- left/top are always 0, regardless of where the
        # window actually sits on screen. width/height from it are correct
        # (they are just a size), but left/top are not a screen position at
        # all yet. `ClientToScreen` maps the client-area origin (0, 0) into
        # screen space; that is the standard Win32 idiom for "client rect,
        # in screen coordinates" -- GetClientRect for the size,
        # ClientToScreen for the position -- and is used deliberately
        # instead of `GetWindowRect`, which returns the *window* rect
        # (including the title bar and frame), a different rectangle that
        # would desync from the client-area frames the capture path
        # produces and break the geometry screening gate that compares
        # against the client rect.
        #
        # UNVERIFIED: corrected by reasoning and Win32 documentation
        # precedent (and by analogy with the identical bug shape just fixed
        # and verified against `xwininfo` on the Linux adapter), not
        # verified against a real Civilization VI window -- no such client
        # exists on this machine to confirm against.
        left_rel, top_rel, right_rel, bottom_rel = win32gui.GetClientRect(hwnd)
        width = int(right_rel - left_rel)
        height = int(bottom_rel - top_rel)
        screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left_rel, top_rel))
        rect = WindowRect(
            left=int(screen_left), top=int(screen_top), width=width, height=height
        )
        title = str(win32gui.GetWindowText(hwnd))
        return GameWindow(handle=int(hwnd), title=title, rect=rect, pid=process.pid)

    def check_capture_preconditions(self) -> CapturePreconditionResult:
        """What can actually be checked cheaply on Windows today, and no more (T249).

        The one implemented capture path is `PrintWindow` + `GetDIBits` via
        ctypes (`_capture_via_print_window`), and the only per-session
        precondition this adapter can probe for it without a window handle
        is that the user32/gdi32 entry points resolve -- so that is what is
        checked, through the same `_gdi()` the capture path itself uses.
        There is no Windows analogue of the Linux compositor probe: whether
        `PrintWindow` frames are hygiene-clean (occlusion, borders, overlay
        chrome) is exactly the R6 spike's question, answered by the recorded
        spike and carried on the run's support tier, not answerable
        per-capture from here. A pass therefore means "the capture path is
        callable", never "the frames are clean" -- per the port contract,
        passing never substitutes for the R6 spike.
        """
        try:
            _gdi()
        except Exception as exc:
            return CapturePreconditionResult(
                passed=False,
                reason=(
                    "user32/gdi32 entry points could not be resolved, so the PrintWindow "
                    f"capture path cannot run at all: {exc}"
                ),
            )
        return CapturePreconditionResult(
            passed=True,
            reason=(
                "user32/gdi32 entry points resolved; the PrintWindow (R6 rank 3) path is "
                "callable. No cheaper per-session hygiene precondition exists to probe on "
                "Windows today -- frame cleanliness is the R6 spike's question, answered "
                "by the run's support tier, not per-capture."
            ),
        )

    def capture_window(self, window: GameWindow) -> CaptureResult:
        try:
            frame = self._capture_via_windows_graphics_capture(window)
        except Exception as exc:  # attempted rank 1 broke; fall through, never raise
            return CaptureResult(
                status=CaptureStatus.failed,
                reason=f"Windows.Graphics.Capture attempt raised: {exc}",
            )
        if frame is not None:
            return CaptureResult(status=CaptureStatus.ok, frame=frame)

        try:
            return self._capture_via_print_window(window)
        except Exception as exc:  # rank 3 also broke; report unavailable, never raise
            return CaptureResult(
                status=CaptureStatus.unavailable,
                reason=f"No working Windows capture path; PrintWindow also failed: {exc}",
            )

    def _capture_via_windows_graphics_capture(self, window: GameWindow) -> CaptureFrame | None:
        # Rank 1 (research R6): Windows.Graphics.Capture via
        # `IGraphicsCaptureItemInterop::CreateForWindow`.
        #
        # UNVERIFIED: the `winsdk` PyPI package projects WinRT APIs
        # automatically, but does not obviously expose the classic-COM
        # `IGraphicsCaptureItemInterop` interop factory needed to build a
        # `GraphicsCaptureItem` from a bare HWND -- that call normally
        # needs an `IActivationFactory::ActivateInstance` +
        # `QueryInterface` sequence this repo could not confirm a `winsdk`
        # binding for without running it on real hardware. Returning
        # `None` here defers to the PrintWindow fallback rather than
        # fabricating an interop call that cannot be verified from this
        # host; the occlusion/border hygiene spike (R6) must pass before
        # this path may be trusted regardless of how it is implemented.
        try:
            import winsdk.windows.graphics.capture  # noqa: F401 - availability probe only
        except ImportError:
            return None
        return None

    def _capture_via_print_window(self, window: GameWindow) -> CaptureResult:
        width, height = window.rect.width, window.rect.height
        if width <= 0 or height <= 0:
            return CaptureResult(
                status=CaptureStatus.unavailable,
                reason="window has a zero-sized client rect",
            )

        gdi = _gdi()
        window_dc = gdi.user32.GetWindowDC(window.handle)
        if not window_dc:
            return CaptureResult(
                status=CaptureStatus.unavailable, reason="GetWindowDC returned null"
            )

        mem_dc = None
        bitmap = None
        previous = None
        try:
            mem_dc = gdi.gdi32.CreateCompatibleDC(window_dc)
            if not mem_dc:
                return CaptureResult(
                    status=CaptureStatus.unavailable, reason="CreateCompatibleDC returned null"
                )
            bitmap = gdi.gdi32.CreateCompatibleBitmap(window_dc, width, height)
            if not bitmap:
                return CaptureResult(
                    status=CaptureStatus.unavailable,
                    reason="CreateCompatibleBitmap returned null",
                )
            previous = gdi.gdi32.SelectObject(mem_dc, bitmap)
            captured = gdi.user32.PrintWindow(window.handle, mem_dc, _PW_RENDERFULLCONTENT)
            if not captured:
                return CaptureResult(
                    status=CaptureStatus.failed, reason="PrintWindow returned false"
                )

            # T224: the pixel extraction itself. See `extract_bgra8` for why the row order and
            # stride are handled the way they are -- those are the two things this path was
            # previously stopped short of, and the two that are easiest to get silently wrong.
            image_bytes = extract_bgra8(mem_dc, bitmap, width=width, height=height)
            if image_bytes is None:
                return CaptureResult(
                    status=CaptureStatus.failed,
                    reason="PrintWindow succeeded but GetDIBits returned no scan lines",
                )
            return CaptureResult(
                status=CaptureStatus.ok,
                frame=CaptureFrame(
                    width=width,
                    height=height,
                    rect=window.rect,
                    image_bytes=image_bytes,
                    image_format="BGRA8",
                ),
            )
        finally:
            # Every GDI object this method created is released on every path, including the
            # error ones. The bitmap in particular was previously never deleted -- a per-capture
            # GDI handle leak, which on a run taking one capture per decision step (hundreds per
            # turn) would exhaust the process's 10,000-object GDI quota within a single game.
            if mem_dc:
                if previous:
                    gdi.gdi32.SelectObject(mem_dc, previous)
                gdi.gdi32.DeleteDC(mem_dc)
            if bitmap:
                gdi.gdi32.DeleteObject(bitmap)
            gdi.user32.ReleaseDC(window.handle, window_dc)

    def _candidate_directories(self, home: Path | None) -> list[GameDirectories]:
        """Every (saves, options) layout this platform is known to use, in preference order.

        Two roots, two profile names, two layouts -- see the module-level
        `_DOCUMENTS_PARTS` note for the measurements behind each. `home`
        overrides the user-profile root for deterministic testing; because
        `%LOCALAPPDATA%` is by definition `%USERPROFILE%\\AppData\\Local`,
        rooting *both* halves at `home` keeps the injected case a faithful
        model of the real one rather than a special case.
        """
        if home is not None:
            profile_root = home
            local_appdata = home.joinpath(*_LOCAL_APPDATA_PARTS)
        else:
            user_profile = os.environ.get("USERPROFILE")
            profile_root = Path(user_profile) if user_profile else Path.home()
            local_appdata_env = os.environ.get("LOCALAPPDATA")
            local_appdata = (
                Path(local_appdata_env) / "Firaxis Games"
                if local_appdata_env
                else profile_root.joinpath(*_LOCAL_APPDATA_PARTS)
            )

        override = os.environ.get(_PROFILE_ENV_VAR)
        profiles = (override,) if override else _PROFILE_NAMES

        candidates: list[GameDirectories] = []
        for profile in profiles:
            documents_base = profile_root.joinpath(*_DOCUMENTS_PARTS, profile)
            saves_dir = documents_base / "Saves" / "Single"
            # Layout A -- current retail: saves under Documents, options
            # under %LOCALAPPDATA%\Firaxis Games. This is what the live
            # Steam client on this host actually uses.
            candidates.append(
                GameDirectories(
                    saves_dir=saves_dir,
                    app_options_path=local_appdata / profile / "AppOptions.txt",
                )
            )
            # Layout B -- the older all-under-Documents layout, still
            # present here for the Epic profile.
            candidates.append(
                GameDirectories(
                    saves_dir=saves_dir,
                    app_options_path=documents_base / "AppOptions.txt",
                )
            )
        return candidates

    def resolve_game_directories(self, *, home: Path | None = None) -> GameDirectories:
        """Resolve this host's saves directory and `AppOptions.txt` (T050, research R5).

        **Chooses on evidence, not on a hard-coded guess.** Windows has more
        than one live layout (module docstring), and this machine has two
        Civ VI profiles on disk at once, so the candidate whose
        `AppOptions.txt` actually exists wins, most-recently-written first.
        A profile the operator last touched in 2021 must not outrank the one
        they played this week just because it sorts earlier.

        **`AppOptions.txt` is the only discriminator, deliberately.** The R5
        spike's live Linux finding was that `Saves/Single/` *does not exist
        until the first save is written*, so requiring it to pre-exist
        false-negatives on a fresh install. `AppOptions.txt`, by contrast, is
        written by the client on its first run and is the file that actually
        differs between the two layouts -- so it discriminates without
        reintroducing the existence check R5 warned against. The returned
        `saves_dir` is still pure path construction and is never required to
        exist.

        Falls back to the first candidate (current-retail layout, unsuffixed
        profile) when nothing on disk discriminates -- a fresh install, or an
        injected `home` under test. That fallback is a resolution, not a
        verification: `probe_host_support` credits the quicksave path from
        the recorded R5 spike, never from this method returning a path.
        """
        candidates = self._candidate_directories(home)

        best: GameDirectories | None = None
        best_mtime = float("-inf")
        for candidate in candidates:
            try:
                mtime = candidate.app_options_path.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = candidate, mtime
        return best if best is not None else candidates[0]

    def send_input(self, events: Sequence[InputEvent]) -> InputResult:
        try:
            import pydirectinput
        except ImportError as exc:
            return InputResult(
                status=InputStatus.unavailable,
                reason=(
                    "pydirectinput is not installed; install the 'windows' extra "
                    f"(`uv sync --extra windows`) to enable synthetic input: {exc}"
                ),
            )

        try:
            for event in events:
                # UNVERIFIED: pydirectinput mirrors pyautogui's key-name
                # vocabulary (e.g. "enter", "esc", single characters);
                # `event.key` values are assumed to already be in that
                # vocabulary rather than translated here.
                if event.kind is InputEventKind.key_press and event.key:
                    pydirectinput.press(event.key)
                elif event.kind is InputEventKind.key_down and event.key:
                    pydirectinput.keyDown(event.key)
                elif event.kind is InputEventKind.key_up and event.key:
                    pydirectinput.keyUp(event.key)
                elif event.kind is InputEventKind.text and event.text:
                    pydirectinput.write(event.text)
                elif (
                    event.kind is InputEventKind.mouse_move
                    and event.x is not None
                    and event.y is not None
                ):
                    pydirectinput.moveTo(event.x, event.y)
                elif (
                    event.kind is InputEventKind.mouse_click
                    and event.x is not None
                    and event.y is not None
                ):
                    pydirectinput.click(event.x, event.y, button=event.button or "left")
        except Exception as exc:  # never raise opaquely; report as a failed dispatch
            return InputResult(
                status=InputStatus.failed, reason=f"SendInput dispatch failed: {exc}"
            )
        return InputResult(status=InputStatus.ok)

    def free_disk_space(self, path: Path) -> DiskSpace:
        return read_disk_space(path)
