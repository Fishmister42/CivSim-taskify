"""Platform, session, and support-tier detection (T048, T049, T212).

Three jobs live here:

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
3. **Actually running that probe** (T212, FR-054) -- `probe_host_support`
   below. Until it existed, nothing in `src/` ever constructed anything but
   `UNPROBED`, so `evaluate_host_gate` refused *every* run on *every* host,
   including the Linux one that had already passed live validation. See that
   function's own docstring for what it does and does not establish.

Nothing in this module imports a platform library: OS/session detection
uses only `sys`, `os.environ`, and `platform`, which are stdlib and
identical everywhere -- the *branching* on their results is what is
platform-aware, not the imports themselves. `host.port` is imported for the
`HostPlatform` Protocol the probe calls through; that module is pure
`typing`/`dataclasses`/`pathlib` and pulls in no OS-specific library either.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from civsim_harness.errors import HarnessError, PreflightError
from civsim_harness.host.port import HostPlatform

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


# --------------------------------------------------------------------------
# T212 -- the R19 per-platform support probe (FR-054)
# --------------------------------------------------------------------------

#: The two live spike documents this probe reports. Both are real, executed results in this
#: repository, each recorded against the one platform it actually ran on -- never generalised.
R5_SAVE_PATH_SPIKE: Final[str] = "specs/002-civ-playing-harness/spikes/r5-save-path.md"
R6_CAPTURE_HYGIENE_SPIKE: Final[str] = (
    "specs/002-civ-playing-harness/spikes/r6-capture-hygiene-linux.md"
)


@dataclass(frozen=True)
class SpikeEvidence:
    """One recorded per-platform spike outcome: did it pass, where is it written down, and why.

    Deliberately carries a ``spike_ref`` rather than a bare boolean. R19's whole design is that a
    tier is "resolved from probe evidence rather than declared", so a `True` that cannot name the
    document it came from is exactly the kind of assertion this type exists to make impossible to
    write by accident.
    """

    passed: bool
    spike_ref: str | None
    note: str


#: R5/T077's save-path result, **per platform**. The spike ran against a live Linux (Aspyr) client
#: and proved `Network.SaveGame` writes a real, size-stable `.Civ6Save` (4 calls, 4 files, 0
#: failures). Its own "Not yet verified" item 5 is explicit that this is a Linux result and that
#: "per R19 each platform confirms its own" -- so Windows and macOS are recorded here as *unprobed
#: for the quicksave path*, which is a statement about the evidence, not a claim that the API
#: differs there (it very likely does not; nobody has shown that it does not).
_QUICKSAVE_EVIDENCE: Final[Mapping[OperatingSystem, SpikeEvidence]] = {
    OperatingSystem.linux: SpikeEvidence(
        passed=True,
        spike_ref=R5_SAVE_PATH_SPIKE,
        note=(
            "R5/T077 ran against a live Linux client and confirmed the FireTuner Lua save path: "
            "Network.SaveGame in the InGame context wrote four consecutive size-stable "
            ".Civ6Save files, and the Linux save directory "
            "(~/.local/share/aspyr-media/.../Saves/Single/) was confirmed"
        ),
    ),
    OperatingSystem.windows: SpikeEvidence(
        passed=False,
        spike_ref=None,
        note=(
            "no R5 save-path spike has been run on Windows. R5/T077's verified result is a Linux "
            "one, and that spike's own 'Not yet verified' item 5 states that Windows and macOS "
            "each confirm their own (research R19). The Lua API is unlikely to differ, but "
            "'unlikely to differ' is not the evidence FR-054 asks for -- run T077's probe "
            "against a live Windows client to clear this"
        ),
    ),
    OperatingSystem.macos: SpikeEvidence(
        passed=False,
        spike_ref=None,
        note=(
            "no R5 save-path spike has been run on macOS. R5/T077's verified result is a Linux "
            "one, and that spike's own 'Not yet verified' item 5 states that Windows and macOS "
            "each confirm their own (research R19) -- the macOS save directory layout in "
            "particular is a documented R1-vs-R5 discrepancy this repo has never resolved live"
        ),
    ),
}

#: The exact text `SupportProbeResult.reason` carries when the host adapter could not even resolve
#: a save directory to address a quicksave into. Distinct from "this platform was never probed":
#: this one says the platform's *evidence* is fine but this particular machine's adapter failed.
_UNRESOLVABLE_SAVES_DIR = "the host adapter could not resolve a Civ VI save directory on this host"


def _capture_hygiene_evidence(
    host_info: HostInfo, *, compositing_verified: bool | None
) -> SpikeEvidence:
    """R6/T099's capture-hygiene result for *host_info*'s platform **and session**.

    Keyed on the session too, not only the OS, because the R6 spike is explicit that a Linux/X11
    pass "must not be recorded as full Linux coverage" -- its Wayland branch (portal ScreenCast,
    `find_game_window` returning `None`, input reported unavailable) is unexecuted code.

    **The X11 pass is conditional, and this function does not assume the condition.** That spike's
    own "Mechanism, and the dependency it creates" section establishes that its occlusion immunity
    is a property of a *compositing* window manager redirecting windows to offscreen pixmaps --
    "without compositing, X11 does not store obscured window contents, and the same call would
    return the occluding window's pixels", turning the pass silently into a failure. It closes with
    a direct instruction: "verify compositing at preflight rather than infer it from
    `XDG_SESSION_TYPE=x11`, and treat 'X11 without a compositor' as a distinct capability case --
    not `VALIDATED`. This is a concrete requirement for `host/linux` (T052) and is **not**
    currently implemented."

    It is still not implemented, so *compositing_verified* defaults to ``None`` ("nobody checked")
    and this function reports the X11 pass as **not** established in that case. That costs nothing
    a run needs: `capture_hygiene_spike_passed` only separates `VALIDATED` from `SUPPORTED`, and
    `evaluate_host_gate` refuses neither -- a `SUPPORTED` host starts and is recorded
    `visually_degraded` under FR-050, which is the truthful state today anyway, since pixel
    extraction is stubbed in all three host adapters (`observe/capture_paths.py`'s own docstring).
    Once T052 lands a real compositor check, pass its result in and the recorded X11 pass is
    honoured.
    """
    if host_info.os is not OperatingSystem.linux:
        return SpikeEvidence(
            passed=False,
            spike_ref=None,
            note=(
                f"no R6 capture-hygiene spike has been run on {host_info.os.value}; R6 is "
                "explicit that a spike passing on one platform says nothing about another"
            ),
        )

    if host_info.session_type is LinuxSessionType.wayland:
        return SpikeEvidence(
            passed=False,
            spike_ref=R6_CAPTURE_HYGIENE_SPIKE,
            note=(
                "the R6 spike ran on Linux/X11 and states outright that it does not validate "
                "Wayland: the portal ScreenCast path, the by-design `find_game_window` -> None, "
                "and the report-input-unavailable requirement are all unexecuted code there"
            ),
        )

    if host_info.session_type is not LinuxSessionType.x11:
        return SpikeEvidence(
            passed=False,
            spike_ref=R6_CAPTURE_HYGIENE_SPIKE,
            note=(
                "the R6 spike's pass is a Linux/X11 result, and this host's session type could "
                "not be resolved to x11 or wayland at all -- not inferred either way"
            ),
        )

    if compositing_verified is not True:
        return SpikeEvidence(
            passed=False,
            spike_ref=R6_CAPTURE_HYGIENE_SPIKE,
            note=(
                "the R6 capture-hygiene spike PASSED on Linux/X11, but that pass is a property "
                "of a compositing window manager redirecting windows offscreen -- without one "
                "the same window-scoped capture returns the occluding window's pixels. The spike "
                "requires that compositing be verified at preflight rather than inferred from "
                "XDG_SESSION_TYPE=x11 (a concrete requirement for host/linux, T052, still "
                "unimplemented), so the pass is recorded as not-yet-established on this host "
                "rather than assumed. This downgrades the tier to SUPPORTED; it refuses nothing"
            ),
        )

    return SpikeEvidence(
        passed=True,
        spike_ref=R6_CAPTURE_HYGIENE_SPIKE,
        note=(
            "the R6 capture-hygiene spike PASSED on Linux/X11 (window-scoped capture excluded an "
            "occluding dialog, unrelated windows, and desktop chrome), and this host's "
            "compositing window manager was verified by its caller"
        ),
    )


def probe_host_support(
    host: HostPlatform,
    host_info: HostInfo | None = None,
    *,
    home: Path | None = None,
    compositing_verified: bool | None = None,
) -> SupportProbeResult:
    """Resolve this host's real :class:`SupportProbeResult` (T212, research R19, FR-054).

    **What this probe is, precisely.** It answers two questions, and refuses to answer either by
    generalising from another platform:

    1. ``quicksave_path_verified`` -- has a quicksave path been *shown* to work on this platform
       (:data:`_QUICKSAVE_EVIDENCE`, read from the recorded R5/T077 spike), **and** can this
       particular machine's host adapter resolve a save directory to address the resulting file
       into? The second conjunct is the weaker of the two and is named as such below; the first is
       what actually gates.
    2. ``capture_hygiene_spike_passed`` -- did this platform *and session*'s own R6/T099 spike
       pass, with every precondition that spike itself flagged actually checked
       (:func:`_capture_hygiene_evidence`).

    **What it deliberately does not do.** It does not take a save, and it does not capture a
    frame. Doing either would require a live client mid-game and would be a *runtime* verification,
    not a preflight one -- `saves/verify.py` already owns the former (the `.Civ6Save` must exist
    with a size stable across two reads before a turn proceeds) and `observe/capture.py`'s four
    screening gates own the latter. This function reports evidence; it does not manufacture it.

    **Why the save-directory check is weak, and why it is still here.** R5's own live finding is
    that ``Saves/Single/`` **does not exist until the first save is written** ("a host adapter that
    probes for the directory's existence during preflight will get a false negative on a fresh
    install; it should resolve the path without requiring it to already exist"), and every adapter
    honours that by doing no existence check at all. So this conjunct really only catches an
    adapter that cannot resolve a path for this host at all. It is kept because the
    ``SupportProbeResult.quicksave_path_verified`` field's own wording is "on this host", not "on
    this platform" -- a probe that never touched the host in front of it would not be answering
    the question the field asks.

    Never raises: a host adapter that fails is reported as an unverified quicksave path with the
    failure in ``reason``, because "this host cannot be probed" and "this host has no quicksave
    path" must both refuse the run, and neither may crash preflight.
    """
    resolved_host_info = host_info if host_info is not None else detect_host_info()

    quicksave_evidence = _QUICKSAVE_EVIDENCE.get(
        resolved_host_info.os,
        SpikeEvidence(
            passed=False,
            spike_ref=None,
            note="no quicksave-path evidence is recorded for this operating system at all",
        ),
    )
    capture_evidence = _capture_hygiene_evidence(
        resolved_host_info, compositing_verified=compositing_verified
    )

    saves_dir: Path | None = None
    saves_dir_detail: str | None = None
    try:
        saves_dir = host.resolve_game_directories(home=home).saves_dir
    except (HarnessError, OSError, KeyError) as exc:
        saves_dir_detail = f"{_UNRESOLVABLE_SAVES_DIR}: {exc}"

    quicksave_verified = quicksave_evidence.passed and saves_dir is not None

    reason_parts = [f"{resolved_host_info.os.value}:"]
    if quicksave_verified:
        reason_parts.append(f"quicksave path VERIFIED -- {quicksave_evidence.note}")
        reason_parts.append(f"(evidence: {quicksave_evidence.spike_ref})")
        reason_parts.append(f"saves resolve to {saves_dir}.")
    elif saves_dir_detail is not None:
        reason_parts.append(f"quicksave path NOT verified -- {saves_dir_detail}.")
    else:
        reason_parts.append(f"quicksave path NOT verified -- {quicksave_evidence.note}.")

    capture_label = "PASSED" if capture_evidence.passed else "not established"
    reason_parts.append(f"capture hygiene {capture_label} -- {capture_evidence.note}.")

    return SupportProbeResult(
        quicksave_path_verified=quicksave_verified,
        capture_hygiene_spike_passed=capture_evidence.passed,
        reason=" ".join(reason_parts),
    )
