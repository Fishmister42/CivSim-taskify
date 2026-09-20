"""Windows host adapter (T050).

- **Window identity**: `pywin32` (`win32gui`, `win32process`).
- **Capture**: Windows.Graphics.Capture (`winsdk`) is rank 1 per research
  R6, attempted first; `PrintWindow` with `PW_RENDERFULLCONTENT` (rank 3)
  is the implemented fallback. Rank 2 (DXGI Desktop Duplication) is not
  implemented -- it is occlusion-*sensitive* per R6, so it is only worth
  building once the hygiene spike shows rank 1 fails and rank 2 is actually
  needed.
- **Directories**: `%USERPROFILE%\\Documents\\My Games\\Sid Meier's
  Civilization VI\\` (research R5; the `AppOptions.txt` leaf is inferred by
  analogy, see the `# UNVERIFIED` note below).
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

import os
from collections.abc import Sequence
from pathlib import Path

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

# UNVERIFIED: exact Windows executable name/casing shipped by the Steam
# release. Civ VI has historically offered a DirectX-version launch choice
# with its own executable; both plausible candidates are listed
# defensively rather than guessing a single one.
_PROCESS_NAMES = ("CivilizationVI.exe", "CivilizationVI_DX12.exe")

# UNVERIFIED: research R5 states the Saves directory explicitly
# (".../My Games/Sid Meier's Civilization VI/Saves/Single/") but only
# gives the in-game menu toggle for enabling the tuner on Windows, not an
# `AppOptions.txt` path -- unlike macOS and Linux, where the file edit
# *is* the enabling mechanism. The path below is inferred by analogy with
# every other Firaxis title's "My Games" layout, not confirmed by a spike.
_BASE_DIR_PARTS = ("Documents", "My Games", "Sid Meier's Civilization VI")

# Long-documented, stable Win32 constant (PW_RENDERFULLCONTENT).
_PW_RENDERFULLCONTENT = 0x00000002


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
        # UNVERIFIED: `GetClientRect` returns client-area-relative
        # coordinates (left/top are always 0); translating to screen
        # coordinates for capture cropping would additionally need
        # `ClientToScreen`. Left as a follow-up for whichever capture path
        # the R6 spike settles on, since rank 1 (WGC) and rank 3
        # (PrintWindow) consume the rect differently.
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        rect = WindowRect(
            left=int(left), top=int(top), width=int(right - left), height=int(bottom - top)
        )
        title = str(win32gui.GetWindowText(hwnd))
        return GameWindow(handle=int(hwnd), title=title, rect=rect, pid=process.pid)

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
        import ctypes

        width, height = window.rect.width, window.rect.height
        if width <= 0 or height <= 0:
            return CaptureResult(
                status=CaptureStatus.unavailable,
                reason="window has a zero-sized client rect",
            )

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        window_dc = user32.GetWindowDC(window.handle)
        if not window_dc:
            return CaptureResult(
                status=CaptureStatus.unavailable, reason="GetWindowDC returned null"
            )
        try:
            mem_dc = gdi32.CreateCompatibleDC(window_dc)
            bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
            gdi32.SelectObject(mem_dc, bitmap)
            captured = user32.PrintWindow(window.handle, mem_dc, _PW_RENDERFULLCONTENT)
            if not captured:
                return CaptureResult(
                    status=CaptureStatus.failed, reason="PrintWindow returned false"
                )

            # UNVERIFIED / documented gap: turning `bitmap` into
            # `CaptureFrame.image_bytes` needs `GetDIBits` with a correctly
            # packed `BITMAPINFOHEADER` and row-stride/padding handling --
            # exactly the kind of ctypes code that is easy to get subtly
            # wrong (row order, padding) without a way to visually verify
            # the output from this host. What is implemented above (window
            # DC capture + the `PrintWindow` call itself) is the
            # verifiable-by-inspection part of the rank-3 path; pixel
            # extraction is the remaining gap for whoever runs the R6
            # spike.
            return CaptureResult(
                status=CaptureStatus.failed,
                reason="PrintWindow succeeded but pixel extraction (GetDIBits) is not implemented",
            )
        finally:
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(window.handle, window_dc)

    def resolve_game_directories(self, *, home: Path | None = None) -> GameDirectories:
        if home is not None:
            base = home.joinpath(*_BASE_DIR_PARTS)
        else:
            user_profile = os.environ.get("USERPROFILE")
            root = Path(user_profile) if user_profile else Path.home()
            base = root.joinpath(*_BASE_DIR_PARTS)
        return GameDirectories(
            saves_dir=base / "Saves" / "Single",
            app_options_path=base / "AppOptions.txt",
        )

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
