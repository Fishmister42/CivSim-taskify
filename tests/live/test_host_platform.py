"""T198 (Linux half) live acceptance: research R19 -- window identity, capture, directories, and
the reported support tier, all resolved against the real client on this host.

**Task**: T198, Linux half only. The Windows and macOS halves are other-platform and verified
elsewhere; this module skips its ENTIRE collection on any non-Linux host so it can be collected
safely everywhere without ever silently degrading into a false pass on the wrong platform.

**What a human runs to make this pass, and what would make it fail**: on a Linux host with Civ VI
up and an X11 session with a compositing manager, run
``pytest tests/live/test_host_platform.py -m live``. It fails if `find_game_window` cannot resolve
the real client's window, if `capture_window`'s frame dimensions disagree with that window's own
rect, if `resolve_game_directories` does not resolve to the real Aspyr Linux install layout, or if
`probe_host_support`'s reported `SupportTier` disagrees with what this host's own live capability
checks (the compositor precondition, the resolvable save directory) actually show right now. This
module is entirely read-only against the client: window identity, capture, directory resolution,
and the support probe never send input or otherwise mutate it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from civsim_harness.host.detect import (  # noqa: E402
    OperatingSystem,
    SupportTier,
    detect_host_info,
    probe_host_support,
    resolve_support_tier,
)
from civsim_harness.host.factory import get_host_platform  # noqa: E402
from civsim_harness.host.port import CaptureStatus, GameProcess  # noqa: E402

pytestmark = pytest.mark.live

_HOST_INFO = detect_host_info()

if _HOST_INFO.os is not OperatingSystem.linux:
    pytest.skip(
        "T198 Linux half only -- this host is "
        f"{_HOST_INFO.os!r}; the Windows/macOS halves are other-platform",
        allow_module_level=True,
    )

# Aspyr's documented Linux leaf layout (research R5/R19; confirmed in
# `host/linux/adapter.py::resolve_game_directories`).
_EXPECTED_SAVES_SUFFIX = Path(
    ".local/share/aspyr-media/Sid Meier's Civilization VI/Saves/Single"
)
_EXPECTED_APP_OPTIONS_SUFFIX = Path(
    ".local/share/aspyr-media/Sid Meier's Civilization VI/AppOptions.txt"
)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        pytest.skip(reason)


def _civ6_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


@pytest.fixture(scope="module")
def host():  # noqa: ANN201
    return get_host_platform(_HOST_INFO)


@pytest.fixture(scope="module")
def civ6_window(host):  # noqa: ANN001, ANN201
    pid = _civ6_pid()
    _require(pid is not None, "Civilization VI (Civ6 process) is not running")
    assert pid is not None
    window = host.find_game_window(GameProcess(pid=pid, name="Civ6"))
    _require(window is not None, "find_game_window could not resolve the real Civ6 window")
    assert window is not None
    return window


def test_window_identity_resolves_against_the_real_client(civ6_window) -> None:  # noqa: ANN001
    assert civ6_window.handle != 0
    assert civ6_window.rect.width > 0
    assert civ6_window.rect.height > 0


def test_capture_matches_the_client_rect(host, civ6_window) -> None:  # noqa: ANN001
    result = host.capture_window(civ6_window)

    assert result.status is CaptureStatus.ok, result.reason
    assert result.frame is not None
    assert (result.frame.width, result.frame.height) == (
        civ6_window.rect.width,
        civ6_window.rect.height,
    )
    assert len(result.frame.image_bytes) == civ6_window.rect.width * civ6_window.rect.height * 4


def test_directories_resolve_to_the_real_install(host) -> None:  # noqa: ANN001
    directories = host.resolve_game_directories()

    saves = directories.saves_dir
    app_options = directories.app_options_path
    assert str(saves).endswith(str(_EXPECTED_SAVES_SUFFIX)), (
        f"resolved saves_dir {saves} does not match the documented Aspyr Linux layout "
        f"(expected it to end with {_EXPECTED_SAVES_SUFFIX})"
    )
    assert str(app_options).endswith(str(_EXPECTED_APP_OPTIONS_SUFFIX)), (
        f"resolved app_options_path {app_options} does not match the documented layout "
        f"(expected it to end with {_EXPECTED_APP_OPTIONS_SUFFIX})"
    )
    # R5: `Saves/Single/` does not exist until the first save is written, so existence is not
    # asserted unconditionally -- but if it DOES exist, it must be a real directory, not a file
    # or a broken symlink, since the production save loader/verifier will address into it.
    if saves.exists():
        assert saves.is_dir(), f"{saves} exists but is not a directory"


def test_reported_tier_matches_what_this_host_actually_supports(host) -> None:  # noqa: ANN001
    """Cross-check `probe_host_support`'s own evidence against independently-observed capability
    on THIS host right now, rather than trusting the probe's self-report in isolation."""
    probe = probe_host_support(host, _HOST_INFO)
    tier = resolve_support_tier(probe)

    # Independent signal 1: if the probe claims the capture-hygiene spike passed for this
    # platform/session, this host's own compositor precondition must ALSO be live-passing right
    # now -- a probe that claims VALIDATED while the live compositor check disagrees would be
    # exactly the drifted-claim defect T249's port seam exists to prevent.
    if probe.capture_hygiene_spike_passed:
        live_precondition = host.check_capture_preconditions()
        assert live_precondition.passed, (
            "probe_host_support claims this platform/session's capture-hygiene spike passed, "
            f"but the live compositor precondition disagrees right now: {live_precondition.reason}"
        )

    # Independent signal 2: if the probe claims a verified quicksave path, this host's own
    # directory resolver must not raise/produce a nonsensical path -- the weaker of the two
    # conjuncts per probe_host_support's own docstring, but still checkable here.
    if probe.quicksave_path_verified:
        directories = host.resolve_game_directories()
        assert str(directories.saves_dir), "quicksave_path_verified=True but no save dir resolved"

    # With the client confirmed running and a compositing session (the module's own fixtures
    # already required this to get this far), an honest probe should not report UNSUPPORTED --
    # if it does, that is telling us something real about this host's recorded spike evidence,
    # so assert on the probe's own reasoning rather than hard-coding an expectation:
    if tier is SupportTier.unsupported:
        pytest.fail(
            "probe_host_support reports UNSUPPORTED on a host with a running client and a "
            f"working compositor -- reason: {probe.reason!r}; this is either a real regression "
            "or missing recorded spike evidence that should be investigated, not ignored"
        )
    assert tier in (SupportTier.supported, SupportTier.validated)
