"""Platform, session, and support-tier detection (T048, T049).

Two independent jobs live here:

1. **What OS and session are we on** (T048) -- resolving the operating
   system, and on Linux distinguishing X11 from Wayland, since they have
   different capture paths and Wayland blocks synthetic input entirely by
   design (research R19, R5).
2. **What tier does that host earn** (T049) -- `VALIDATED`, `SUPPORTED`, or
   `UNSUPPORTED`, resolved from probe evidence rather than declared. A
   platform is `UNSUPPORTED` until probed: `SupportProbeResult`'s fields all
   default to their unprobed values, so a default-constructed result
   resolves to `UNSUPPORTED` by construction, not by an `else` branch that
   merely happens to run last.

Nothing in this module imports a platform library: OS/session detection
uses only `sys`, `os.environ`, and `platform`, which are stdlib and
identical everywhere -- the *branching* on their results is what is
platform-aware, not the imports themselves.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final

from civsim_harness.errors import PreflightError

# --------------------------------------------------------------------------
# T048 -- OS and session detection
# --------------------------------------------------------------------------


class OperatingSystem(Enum):
    """The three natively-supported target operating systems (R1)."""

    windows = "windows"
    macos = "macos"
    linux = "linux"


class LinuxSessionType(Enum):
    """X11 vs Wayland: different capture paths, and Wayland blocks input (R5, R19)."""

    x11 = "x11"
    wayland = "wayland"
    unknown = "unknown"


@dataclass(frozen=True)
class HostInfo:
    """What `Run.host_platform` records: OS, version, and session where it matters."""

    os: OperatingSystem
    os_version: str
    session_type: LinuxSessionType | None
    """Only meaningful when `os is OperatingSystem.linux`; `None` otherwise."""


def detect_os(*, sys_platform: str | None = None) -> OperatingSystem:
    """Resolve the operating system from `sys.platform` (or an injected value for tests)."""
    value = sys_platform if sys_platform is not None else sys.platform
    if value.startswith("win"):
        return OperatingSystem.windows
    if value == "darwin":
        return OperatingSystem.macos
    if value.startswith("linux"):
        return OperatingSystem.linux
    raise PreflightError(
        f"Unrecognised host operating system {value!r}; the harness supports only "
        "Windows, macOS, and native Linux (research R1).",
        detail={"sys_platform": value},
    )


def detect_linux_session_type(environ: Mapping[str, str] | None = None) -> LinuxSessionType:
    """Distinguish X11 from Wayland from the session environment (research R19, R5).

    `environ` is injectable so this is unit-testable without touching the
    real process environment (and so it can be exercised deterministically
    on any host, including this one).
    """
    env = environ if environ is not None else os.environ
    xdg_session_type = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if xdg_session_type == "wayland":
        return LinuxSessionType.wayland
    if xdg_session_type == "x11":
        return LinuxSessionType.x11
    if env.get("WAYLAND_DISPLAY"):
        return LinuxSessionType.wayland
    if env.get("DISPLAY"):
        return LinuxSessionType.x11
    return LinuxSessionType.unknown


def detect_host_info(
    *,
    sys_platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> HostInfo:
    """Resolve the full `HostInfo` for the current (or injected) host."""
    operating_system = detect_os(sys_platform=sys_platform)
    session_type = (
        detect_linux_session_type(environ) if operating_system is OperatingSystem.linux else None
    )
    return HostInfo(os=operating_system, os_version=platform.version(), session_type=session_type)


# --------------------------------------------------------------------------
# T049 -- support-tier resolution
# --------------------------------------------------------------------------


class SupportTier(Enum):
    """How capable a probed host has been shown to be (research R19).

    Ordered least to most capable so a caller may compare tiers with `<`
    style reasoning if needed, but membership is what matters: only
    `validated` and `supported` may play a run at all (R19; `unsupported`
    has no verified quicksave path, and FR-007 makes a turn without one
    impossible).
    """

    unsupported = "unsupported"
    supported = "supported"
    validated = "validated"


@dataclass(frozen=True)
class SupportProbeResult:
    """Evidence collected by probing a host adapter's capabilities (research R19).

    Every field defaults to its *unprobed* value. That is deliberate: a
    default-constructed `SupportProbeResult()` already resolves to
    `SupportTier.unsupported` through `resolve_support_tier` below, which is
    what makes "unsupported until probed" the literal default rather than a
    fallback branch reached only when other checks fail.
    """

    quicksave_path_verified: bool = False
    """A Lua save path (R5 outcome A) or the bespoke dialog-driving path (R5
    outcome B/C) has been shown to actually produce a verifiable save on this
    host. Without this, FR-007 makes the platform unable to run at all."""

    capture_hygiene_spike_passed: bool = False
    """This platform's own occlusion-immunity and capture-border spike (R6)
    passed. Never inferred from another platform's result (R6: "a spike
    passing on Windows says nothing about macOS")."""

    reason: str = "not yet probed"
    """A human-readable note explaining the evidence above, surfaced by
    `doctor` (R19: "`doctor` prints the tier with the reason")."""


UNPROBED: Final[SupportProbeResult] = SupportProbeResult()
"""The zero-evidence state every platform starts in until R19's per-platform probe runs."""


def resolve_support_tier(probe: SupportProbeResult) -> SupportTier:
    """Assign a `SupportTier` from probe evidence (research R19).

    `UNPROBED` (the all-defaults state) resolves to `unsupported` here
    purely because `quicksave_path_verified` defaults to `False` -- there is
    no separate "not probed yet" branch to fall out of sync with the
    defaults.
    """
    if not probe.quicksave_path_verified:
        return SupportTier.unsupported
    if probe.capture_hygiene_spike_passed:
        return SupportTier.validated
    return SupportTier.supported
