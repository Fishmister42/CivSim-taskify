"""T224 (Windows half): GDI pixel extraction actually produces pixels.

Every `capture_window` path on Windows used to stop one call short of a frame: `PrintWindow` put
pixels into a GDI bitmap and nothing ever read them back, so **every** capture returned `failed`
with "pixel extraction (GetDIBits) is not implemented", every decision step was recorded
`visually_degraded`, and every run was comparability-degraded regardless of what `capture_path`
resolution or the camera-state provider reported.

**What is verified here, and what is not -- stated plainly.** There is no Civilization VI client
on the machine these tests were written on, so nothing below captures a Civ VI window. What they
do capture is a GDI bitmap this test fills itself, with known colours in known places, which is
enough to verify the part that was missing and the part that is easy to get silently wrong:

- the `BITMAPINFOHEADER` is packed such that `GetDIBits` accepts it and copies scan lines;
- the buffer comes back **top-down**, matching what `parity/screening.py::_decode_frame` assumes
  (`Image.frombuffer(..., "raw", raw_mode, 0, 1)`) -- a bottom-up buffer would be a vertically
  mirrored frame, which produces no error anywhere and would quietly corrupt every screening gate
  that reasons about where something is on screen;
- the row stride is exactly `width * 4` with no padding, checked at a width deliberately **not**
  divisible by 4 (the case that is invisible on the 1920-wide windows anyone would test with);
- channel order is BGRA, so `CaptureFrame.image_format = "BGRA8"` is a true label.

`_capture_via_print_window` end to end against a real Civ VI window, and the R6 capture-hygiene
verdict for the resulting frame (T099), still need a live client. This is the extraction path,
tested against a window that is not Civilization VI.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="GDI pixel extraction is a Windows-only path"
)


def _fill(gdi, dc, *, left: int, top: int, right: int, bottom: int, colorref: int) -> None:
    """Paint one solid rectangle into *dc* using a temporary solid brush."""
    import ctypes
    from ctypes import wintypes

    class _RECT(ctypes.Structure):
        _fields_ = (
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    user32.FillRect.argtypes = (wintypes.HDC, ctypes.POINTER(_RECT), wintypes.HBRUSH)
    user32.FillRect.restype = ctypes.c_int

    brush = gdi32.CreateSolidBrush(colorref)
    try:
        rect = _RECT(left=left, top=top, right=right, bottom=bottom)
        assert user32.FillRect(dc, ctypes.byref(rect), brush) != 0
    finally:
        gdi.gdi32.DeleteObject(brush)


def _extract_painted_bitmap(width: int, height: int) -> bytes:
    """Create a *width* x *height* bitmap, paint the top half red and the bottom half blue,
    and return what the production extraction path reads back out of it.

    Red and blue specifically: they are the two channels BGRA and RGBA disagree about, so a
    channel-order mistake shows up as "the top half is blue" rather than as a subtle tint.
    """
    from civsim_harness.host.windows.adapter import _gdi, extract_bgra8

    gdi = _gdi()
    screen_dc = gdi.user32.GetWindowDC(None)
    assert screen_dc, "GetWindowDC(NULL) returned null; no screen DC available"

    mem_dc = None
    bitmap = None
    previous = None
    try:
        mem_dc = gdi.gdi32.CreateCompatibleDC(screen_dc)
        assert mem_dc
        bitmap = gdi.gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        assert bitmap
        previous = gdi.gdi32.SelectObject(mem_dc, bitmap)

        half = height // 2
        # COLORREF is 0x00BBGGRR, so these are pure red and pure blue.
        _fill(gdi, mem_dc, left=0, top=0, right=width, bottom=half, colorref=0x000000FF)
        _fill(gdi, mem_dc, left=0, top=half, right=width, bottom=height, colorref=0x00FF0000)

        extracted = extract_bgra8(mem_dc, bitmap, width=width, height=height)
        assert extracted is not None, "GetDIBits copied no scan lines"
        return extracted
    finally:
        if mem_dc:
            if previous:
                gdi.gdi32.SelectObject(mem_dc, previous)
            gdi.gdi32.DeleteDC(mem_dc)
        if bitmap:
            gdi.gdi32.DeleteObject(bitmap)
        gdi.user32.ReleaseDC(None, screen_dc)


# A width deliberately not divisible by 4: at 24bpp this is exactly the width whose scan lines
# would be padded, shifting every row after the first. At the 32bpp this adapter asks for, it
# must not be.
_ODD_WIDTH = 37
_HEIGHT = 16


def test_extraction_returns_a_densely_packed_buffer_of_the_expected_size() -> None:
    """`width * height * 4` bytes exactly -- no scan-line padding at 32bpp, at any width."""
    pixels = _extract_painted_bitmap(_ODD_WIDTH, _HEIGHT)
    assert len(pixels) == _ODD_WIDTH * _HEIGHT * 4


def test_the_first_row_is_the_top_of_the_image_not_the_bottom() -> None:
    """The row-order check, and the reason `biHeight` is negative.

    A DIB is bottom-up by default. Getting this wrong returns a vertically mirrored frame, raises
    nothing anywhere, and would silently corrupt every parity screening gate that reasons about
    screen position. The bitmap is painted red on top and blue on the bottom, so the first row
    coming back red is the assertion that the buffer is top-down.
    """
    pixels = _extract_painted_bitmap(_ODD_WIDTH, _HEIGHT)
    stride = _ODD_WIDTH * 4

    first_pixel = pixels[0:4]
    last_row_pixel = pixels[stride * (_HEIGHT - 1) : stride * (_HEIGHT - 1) + 4]

    # BGRA: red is (0, 0, 255, 255), blue is (255, 0, 0, 255).
    assert first_pixel == bytes((0, 0, 255, 255)), (
        "the first row is not the red top of the image -- the buffer came back bottom-up, which "
        "would mirror every captured frame vertically with no error anywhere (T224)"
    )
    assert last_row_pixel == bytes((255, 0, 0, 255))


def test_every_row_starts_where_the_stride_says_it_does() -> None:
    """Stride correctness across all rows, not just the first.

    A padding mistake is invisible in row 0 and grows by one padding unit per row -- so the
    halfway boundary between the painted colours is where it would first become obvious.
    """
    pixels = _extract_painted_bitmap(_ODD_WIDTH, _HEIGHT)
    stride = _ODD_WIDTH * 4
    half = _HEIGHT // 2

    for row in range(_HEIGHT):
        expected = bytes((0, 0, 255, 255)) if row < half else bytes((255, 0, 0, 255))
        for column in (0, _ODD_WIDTH // 2, _ODD_WIDTH - 1):
            offset = row * stride + column * 4
            assert pixels[offset : offset + 4] == expected, (
                f"row {row}, column {column} is not the colour painted there; the row stride is "
                f"not {stride} bytes (T224)"
            )


def test_the_alpha_channel_is_opaque_so_the_bgra8_label_is_true() -> None:
    """`PrintWindow` into a 32-bit DIB leaves alpha undefined (in practice zero).

    Every consumer in this codebase converts to RGB and discards it, but a buffer labelled
    `BGRA8` whose alpha reads "fully transparent" is a misdescription waiting to be believed by
    the next one.
    """
    pixels = _extract_painted_bitmap(_ODD_WIDTH, _HEIGHT)
    assert set(pixels[3::4]) == {0xFF}


def test_the_extracted_frame_decodes_through_the_real_screening_path() -> None:
    """End-to-end against the consumer that actually reads these bytes.

    `parity/screening.py::_decode_frame` is what turns a `CaptureFrame` into the image the four
    hygiene gates judge. Asserting the buffer decodes *there*, to the colours painted, is what
    makes "BGRA8, top-down, densely packed" a fact about the contract rather than about this
    test's own arithmetic.
    """
    from civsim_harness.host.port import CaptureFrame, WindowRect
    from civsim_harness.parity.screening import _decode_frame

    pixels = _extract_painted_bitmap(_ODD_WIDTH, _HEIGHT)
    frame = CaptureFrame(
        width=_ODD_WIDTH,
        height=_HEIGHT,
        rect=WindowRect(left=0, top=0, width=_ODD_WIDTH, height=_HEIGHT),
        image_bytes=pixels,
        image_format="BGRA8",
    )

    image = _decode_frame(frame)
    assert image is not None, "the extracted frame could not be decoded by the screening path"
    assert image.size == (_ODD_WIDTH, _HEIGHT)
    assert image.getpixel((0, 0)) == (255, 0, 0)
    assert image.getpixel((0, _HEIGHT - 1)) == (0, 0, 255)
