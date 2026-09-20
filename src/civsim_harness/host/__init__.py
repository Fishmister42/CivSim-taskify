"""THE ONLY PLACE AN OS-SPECIFIC IMPORT MAY APPEAR (plan.md Project Structure; research R19).

Everything the harness needs from an operating system -- window identity,
screen capture, game directories, optional synthetic input, process
liveness, and free disk space -- is isolated behind the `HostPlatform`
Protocol defined in `port.py`, with one adapter per platform in the
`windows`, `macos`, and `linux` subpackages. A `ruff` banned-api rule
(TID251) enforces this repo-wide, exempting only this package via
`pyproject.toml`'s `per-file-ignores`; `tests/unit/test_platform_neutrality.
py` enforces the same rule at test time, including the one shape TID251
cannot see (`ctypes.windll` after a bare `import ctypes`).

Importing this package -- or `detect.py`, or `factory.py` -- never pulls a
platform library itself: the OS-specific imports live inside the adapter
modules, and even those are reached lazily, only when `get_host_platform`
actually needs one (see `factory.py`).
"""

from __future__ import annotations

from civsim_harness.host.detect import (
    UNPROBED,
    HostInfo,
    LinuxSessionType,
    OperatingSystem,
    SupportProbeResult,
    SupportTier,
    detect_host_info,
    detect_linux_session_type,
    detect_os,
    resolve_support_tier,
)
from civsim_harness.host.factory import get_host_platform
from civsim_harness.host.port import (
    CaptureFrame,
    CaptureResult,
    CaptureStatus,
    DiskSpace,
    GameDirectories,
    GameProcess,
    GameWindow,
    HostPlatform,
    InputEvent,
    InputEventKind,
    InputResult,
    InputStatus,
    WindowRect,
)

__all__ = [
    "UNPROBED",
    "CaptureFrame",
    "CaptureResult",
    "CaptureStatus",
    "DiskSpace",
    "GameDirectories",
    "GameProcess",
    "GameWindow",
    "HostInfo",
    "HostPlatform",
    "InputEvent",
    "InputEventKind",
    "InputResult",
    "InputStatus",
    "LinuxSessionType",
    "OperatingSystem",
    "SupportProbeResult",
    "SupportTier",
    "WindowRect",
    "detect_host_info",
    "detect_linux_session_type",
    "detect_os",
    "get_host_platform",
    "resolve_support_tier",
]
