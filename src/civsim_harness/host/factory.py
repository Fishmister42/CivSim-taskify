"""Construct the `HostPlatform` adapter for the current host.

Every branch below imports its adapter module **lazily, inside this
function body** -- never at this module's top level -- so that importing
`civsim_harness.host` (or this module) never pulls a platform library on
the wrong OS. That is what lets `tests/contract/test_host_platform_port.py`
import all three adapter *modules* on any single CI machine: the modules
themselves only reach for `pywin32`/`Quartz`/`Xlib` inside the specific
methods that need them (see each adapter), and this factory only reaches
for a given adapter *module* when the caller actually asks for that
platform.
"""

from __future__ import annotations

from civsim_harness.errors import PreflightError
from civsim_harness.host.detect import HostInfo, LinuxSessionType, OperatingSystem, detect_host_info
from civsim_harness.host.port import HostPlatform


def get_host_platform(host_info: HostInfo | None = None) -> HostPlatform:
    """Return the adapter for `host_info` (or the detected current host).

    Raises `PreflightError` for an operating system the harness does not
    support (research R1) -- this is a setup problem, never something a run
    should try to paper over.
    """
    info = host_info if host_info is not None else detect_host_info()

    if info.os is OperatingSystem.windows:
        from civsim_harness.host.windows.adapter import WindowsHostPlatform

        return WindowsHostPlatform()

    if info.os is OperatingSystem.macos:
        from civsim_harness.host.macos.adapter import MacOSHostPlatform

        return MacOSHostPlatform()

    if info.os is OperatingSystem.linux:
        from civsim_harness.host.linux.adapter import LinuxHostPlatform

        return LinuxHostPlatform(session_type=info.session_type or LinuxSessionType.unknown)

    raise PreflightError(  # pragma: no cover - detect_host_info() cannot produce this today
        f"No host adapter for operating system {info.os!r}.",
        detail={"os": info.os.value},
    )
