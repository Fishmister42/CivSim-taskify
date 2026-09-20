"""Capture-path selection (T098).

Asks the current host's adapter to capture the game window, and names *which* research-R6-ranked
mechanism a successful capture corresponds to, for recording on ``Run.capture_path``.

**No OS-specific code lives here.** ``civsim_harness.host.detect`` (T048/T049) is itself built from
only ``sys``/``os.environ``/``platform`` -- portable everywhere -- and ``civsim_harness.host.port``
is pure ``typing``/``dataclasses``. This module imports only those two, plus the
:class:`~civsim_harness.host.port.HostPlatform` protocol it is handed; it never imports
``pywin32``, ``winsdk``, ``Quartz``, or ``Xlib``, and never calls a platform API directly.

The ranking data itself is necessarily platform-specific *content* (research R6: Windows, macOS,
and Linux each have their own ordered list of candidate mechanisms) even though looking it up is not
platform-specific *code* -- the same distinction ``host.detect``'s own ``HostInfo`` already draws
between "detecting the OS" (portable) and "branching on what was detected" (platform-aware content,
portable code). :data:`CapturePath` only has one enum member for Windows
(``WINDOWS_GRAPHICS_CAPTURE``) because research R6 treats its rank-2/3 fallbacks (DXGI Desktop
Duplication, ``PrintWindow``) as **not** occlusion-immune -- they cannot pass the hygiene bar a
recorded ``capture_path`` implies, so they are not given a recordable identity at all; a capture
that only such a path could produce is, for this harness's purposes, indistinguishable from no
capture path.

**Known limitation, stated plainly**: :class:`~civsim_harness.host.port.CaptureResult` does not
itself say *which* internal candidate an adapter used to produce a successful frame (see e.g.
``WindowsHostPlatform.capture_window``, which tries its own rank-1 path before falling back
internally) -- there is no path-tagged field to read that back from without editing
``host/port.py``, which this wave does not own. This module resolves that by recording this host's
highest-ranked, hygiene-eligible :class:`CapturePath` on any ``ok`` result. Today this is moot in
practice: pixel extraction is stubbed in all three host adapters (see their own module docstrings),
so ``capture_window`` reports ``unavailable``/``failed`` at runtime everywhere, and every selection
below resolves to :data:`~civsim_harness.models.common.CapturePath.NONE` until that lands.
"""

from __future__ import annotations

from dataclasses import dataclass

from civsim_harness.host.detect import HostInfo, LinuxSessionType, OperatingSystem
from civsim_harness.host.port import CaptureResult, CaptureStatus, GameWindow, HostPlatform
from civsim_harness.models.common import CapturePath

#: Research R6's ranked, hygiene-eligible capture path per platform/session, best first. Windows
#: carries only its rank-1 path (see module docstring); macOS carries both its primary and its
#: documented legacy fallback; Linux is split by session type since X11 and Wayland use entirely
#: different mechanisms.
_WINDOWS_RANKING: tuple[CapturePath, ...] = (CapturePath.WINDOWS_GRAPHICS_CAPTURE,)
_MACOS_RANKING: tuple[CapturePath, ...] = (
    CapturePath.SCREEN_CAPTURE_KIT,
    CapturePath.CG_WINDOW_LIST_IMAGE,
)
_LINUX_X11_RANKING: tuple[CapturePath, ...] = (CapturePath.XCOMPOSITE,)
_LINUX_WAYLAND_RANKING: tuple[CapturePath, ...] = (CapturePath.PIPEWIRE_PORTAL,)


def ranked_capture_paths(host_info: HostInfo) -> tuple[CapturePath, ...]:
    """Return *host_info*'s capture paths in research-R6 rank order, best first.

    A pure data lookup keyed on :class:`~civsim_harness.host.detect.HostInfo` (itself produced by
    platform-library-free detection) -- calling this never imports or invokes a platform library,
    so it adds no OS-specific code to this module even though its *content* differs per OS.
    """
    if host_info.os is OperatingSystem.windows:
        return _WINDOWS_RANKING
    if host_info.os is OperatingSystem.macos:
        return _MACOS_RANKING
    if host_info.os is OperatingSystem.linux:
        if host_info.session_type is LinuxSessionType.wayland:
            return _LINUX_WAYLAND_RANKING
        return _LINUX_X11_RANKING
    return ()  # pragma: no cover - host.detect.detect_os() cannot produce an unhandled OS today


@dataclass(frozen=True)
class CapturePathSelection:
    """The outcome of one capture-path selection attempt: the named path (``NONE`` unless the
    attempt succeeded) plus the underlying :class:`~civsim_harness.host.port.CaptureResult` for
    whatever the caller needs beyond the name (a failure reason, the raw frame for the not-yet-built
    screening pipeline, ...).
    """

    capture_path: CapturePath
    capture_result: CaptureResult


def select_capture_path(
    *,
    host: HostPlatform,
    host_info: HostInfo,
    window: GameWindow,
) -> CapturePathSelection:
    """Ask *host* to capture *window* once, and name which R6-ranked path a success represents.

    Makes exactly one :meth:`~civsim_harness.host.port.HostPlatform.capture_window` attempt -- the
    adapter itself already encodes its own internal fallback order (e.g.
    ``WindowsHostPlatform.capture_window`` tries Windows.Graphics.Capture before falling back to
    ``PrintWindow``), so "select the highest available" is realised by making one call and trusting
    the adapter's own ranking, not by this function probing multiple paths itself.

    Resolves to :data:`~civsim_harness.models.common.CapturePath.NONE` for anything other than
    :attr:`~civsim_harness.host.port.CaptureStatus.ok` (research R6 rank 4: "no capture path" is a
    legitimate, recorded operating state, never an error) -- and otherwise to this host's
    highest-ranked entry from :func:`ranked_capture_paths` (see the module docstring's stated
    limitation on why a finer-grained identification is not available from this port).
    """
    result = host.capture_window(window)
    if result.status is not CaptureStatus.ok:
        return CapturePathSelection(capture_path=CapturePath.NONE, capture_result=result)

    ranking = ranked_capture_paths(host_info)
    selected = ranking[0] if ranking else CapturePath.NONE
    return CapturePathSelection(capture_path=selected, capture_result=result)
