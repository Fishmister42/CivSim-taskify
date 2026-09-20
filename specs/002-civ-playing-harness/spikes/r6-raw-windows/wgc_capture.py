"""R6/T099 Windows spike: window-scoped capture via Windows.Graphics.Capture.

This is the rank-1 capture path research R6 names for Windows, and the only
one T099 states a *specific* hygiene requirement for: the WGC capture border
must not appear in the frame (``GraphicsCaptureSession.IsBorderRequired``
must be false).

Built standalone under the spike directory on purpose. `host/windows/adapter.py`
is being edited concurrently by another agent, so proving this path produces a
real frame from a real window first -- and keeping the evidence beside the
spike that justifies it -- means the adapter only ever receives code that has
already worked.

**Window-scoped only.** There is deliberately no screen/root fallback anywhere
in this file (Principle I): a root grab captures occluding windows and leaks
content the agent must never see. `capture_window` takes an HWND and there is
no code path that widens that.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass

import winsdk._winrt as _winrt
from winsdk.windows.graphics import SizeInt32
from winsdk.windows.graphics.capture import Direct3D11CaptureFramePool, GraphicsCaptureItem
from winsdk.windows.graphics.directx import DirectXPixelFormat
from winsdk.windows.graphics.directx.direct3d11 import IDirect3DDevice

_D3D_DRIVER_TYPE_HARDWARE = 1
_D3D11_SDK_VERSION = 7
_D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20

# {54ec77fa-1377-44e6-8c32-88fd5f44c84c}
_IID_IDXGIDEVICE = (ctypes.c_byte * 16)(
    0xFA, 0x77, 0xEC, 0x54, 0x77, 0x13, 0xE6, 0x44,
    0x8C, 0x32, 0x88, 0xFD, 0x5F, 0x44, 0xC8, 0x4C,
)


def create_direct3d_device() -> IDirect3DDevice:
    """Build the `IDirect3DDevice` WGC needs.

    `winsdk` projects the WinRT surface but cannot manufacture this type: it is
    only obtainable by handing a classic-COM `IDXGIDevice` to
    `CreateDirect3D11DeviceFromDXGIDevice`, a plain C export in d3d11.dll that
    returns an `IInspectable`. Hence ctypes for the three C calls, then
    `IDirect3DDevice._from` to bring the pointer back into the projection.

    The adapter's standing note says the `IGraphicsCaptureItemInterop` factory
    "could not be confirmed a winsdk binding for" and returns `None` because of
    it. Two things are now established against real hardware: the interop
    factory *is* bound (`winsdk.windows.graphics.capture.interop.create_for_window`),
    and it is not even needed -- `GraphicsCaptureItem.try_create_from_window_id`
    reaches the same object through the pure WinRT surface.
    """
    d3d11 = ctypes.WinDLL("d3d11")
    device = ctypes.c_void_p()
    context = ctypes.c_void_p()
    level = ctypes.c_uint()
    hr = d3d11.D3D11CreateDevice(
        None, _D3D_DRIVER_TYPE_HARDWARE, None,
        _D3D11_CREATE_DEVICE_BGRA_SUPPORT, None, 0, _D3D11_SDK_VERSION,
        ctypes.byref(device), ctypes.byref(level), ctypes.byref(context),
    )
    if hr < 0:
        raise OSError(f"D3D11CreateDevice failed 0x{hr & 0xFFFFFFFF:08X}")

    vtable = ctypes.cast(device, ctypes.POINTER(ctypes.c_void_p))[0]
    query_interface = ctypes.cast(
        ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[0],
        ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        ),
    )
    dxgi = ctypes.c_void_p()
    hr = query_interface(device, ctypes.byref(_IID_IDXGIDEVICE), ctypes.byref(dxgi))
    if hr < 0:
        raise OSError(f"QueryInterface(IDXGIDevice) failed 0x{hr & 0xFFFFFFFF:08X}")

    d3d11.CreateDirect3D11DeviceFromDXGIDevice.restype = ctypes.c_long
    inspectable = ctypes.c_void_p()
    hr = d3d11.CreateDirect3D11DeviceFromDXGIDevice(dxgi, ctypes.byref(inspectable))
    if hr < 0:
        raise OSError(f"CreateDirect3D11DeviceFromDXGIDevice failed 0x{hr & 0xFFFFFFFF:08X}")

    return IDirect3DDevice._from(_winrt.Object(inspectable.value))


@dataclass(frozen=True)
class WgcFrame:
    width: int
    height: int
    bgra: bytes
    border_required: bool
    """What `IsBorderRequired` **read back** after we set it false -- never what
    we asked for. T099's requirement is about the property the system agreed
    to, not the one we requested. Asserting on our own setter's argument would
    be exactly the "assert on what our side returned" mistake the Linux peer's
    rule exists to prevent."""

    border_settable: bool
    """False when this Windows build predates the `IsBorderRequired` property
    (it arrived in Windows 11 build 22000). Reported rather than swallowed: a
    host that cannot turn the border off is a different capability case, not a
    pass."""


def capture_window(hwnd: int, *, timeout_s: float = 4.0) -> WgcFrame:
    """Capture exactly the window `hwnd` owns, and nothing else on screen."""
    try:
        _winrt.init_apartment(_winrt.MTA)
    except OSError:
        pass  # already initialised on this thread; harmless

    item = GraphicsCaptureItem.try_create_from_window_id(_window_id(hwnd))
    if item is None:
        raise RuntimeError(f"WGC refused to create a capture item for hwnd {hwnd}")

    device = create_direct3d_device()
    size: SizeInt32 = item.size
    pool = Direct3D11CaptureFramePool.create_free_threaded(
        device, DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED, 2, size
    )
    session = pool.create_capture_session(item)

    border_settable = True
    try:
        session.is_border_required = False
    except Exception:  # noqa: BLE001 - pre-22000 Windows has no such property
        border_settable = False
    try:
        session.is_cursor_capture_enabled = False
    except Exception:  # noqa: BLE001 - same vintage story; cosmetic either way
        pass

    session.start_capture()
    try:
        deadline = time.monotonic() + timeout_s
        frame = None
        while time.monotonic() < deadline:
            frame = pool.try_get_next_frame()
            if frame is not None:
                break
            time.sleep(0.05)
        if frame is None:
            raise TimeoutError(f"no WGC frame arrived for hwnd {hwnd} within {timeout_s}s")

        bgra = _surface_to_bgra(frame.surface)
        border_required = True
        try:
            border_required = bool(session.is_border_required)
        except Exception:  # noqa: BLE001
            border_required = False if not border_settable else True
        return WgcFrame(
            width=frame.content_size.width,
            height=frame.content_size.height,
            bgra=bgra,
            border_required=border_required,
            border_settable=border_settable,
        )
    finally:
        session.close()
        pool.close()


def _window_id(hwnd: int):
    from winsdk.windows.ui import WindowId

    return WindowId(value=hwnd)


def _surface_to_bgra(surface) -> bytes:
    """GPU surface -> packed BGRA8 bytes, via WinRT's own copy.

    `SoftwareBitmap.create_copy_from_surface_async` does the GPU->CPU staging
    copy that would otherwise need a hand-rolled D3D11 staging texture and a
    row-pitch-aware map. Using it removes the single most error-prone piece of
    this path -- the one where a wrong stride yields an image that looks right
    at some widths and skews at others.
    """
    import asyncio

    from winsdk.windows.graphics.imaging import SoftwareBitmap
    from winsdk.windows.storage.streams import Buffer

    async def _go() -> bytes:
        bitmap = await SoftwareBitmap.create_copy_from_surface_async(surface)
        buffer = Buffer(bitmap.pixel_width * bitmap.pixel_height * 4)
        bitmap.copy_to_buffer(buffer)
        return bytes(buffer)

    return asyncio.run(_go())


def save_png(frame: WgcFrame, path: str) -> None:
    from PIL import Image

    img = Image.frombuffer(
        "RGBA", (frame.width, frame.height), frame.bgra, "raw", "BGRA", 0, 1
    )
    img.save(path)
