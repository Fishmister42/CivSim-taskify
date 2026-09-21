"""Phase 10 convergence: the host support probe and the composition root's value resolvers.

Covers T212 (`host.detect.probe_host_support`), T216 (`_interpret_game_outcome`), T220
(`_resolve_capture_path`), and T221 (`_to_camera_state`) -- the four places Phase 10 replaced a
hard-coded placeholder with a resolved value -- plus T249's per-adapter halves of the
`check_capture_preconditions` port preflight (the bottom section; the capture-path call site is
pinned in `tests/unit/test_capture_for_step.py` and far-side in
`tests/integration/test_capture_preflight.py`).

**Why these four and not the run-scoped seams.** T214's terminal-state close, T219's guidance
resolution, and T222's live `connection_health` all live inside closures over one run's own
`_RunContext`, which only exists after a real `prepare_run` against a live (or faked) tuner --
they are exercised by `tests/integration/test_end_to_end_wiring.py`, which drives the real
composition root end to end, not from here.

The probe tests are deliberately written against **recorded spike evidence**, which is what R19
says a tier is resolved from. They assert that evidence is never generalised across platforms --
the one failure mode that would silently hand a host a capability nothing ever demonstrated on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from civsim_harness.errors import PreflightError
from civsim_harness.host.detect import (
    HostInfo,
    LinuxSessionType,
    OperatingSystem,
    SupportTier,
    probe_host_support,
    resolve_support_tier,
)
from civsim_harness.host.linux.adapter import CapturePreconditions, LinuxHostPlatform
from civsim_harness.host.macos.adapter import MacOSHostPlatform
from civsim_harness.host.port import (
    CaptureFrame,
    CapturePreconditionResult,
    CaptureResult,
    CaptureStatus,
    GameDirectories,
    GameWindow,
    WindowRect,
)
from civsim_harness.host.windows import adapter as windows_adapter
from civsim_harness.host.windows.adapter import WindowsHostPlatform
from civsim_harness.models.common import CapturePath, DeclarationId
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.run.composition import (
    GAME_OUTCOME_DECLARATION_ID,
    _interpret_game_outcome,
    _OutcomeTrackingObservationReader,
    _resolve_capture_path,
    _RunContext,
    _to_camera_state,
)
from civsim_harness.run.stop import GameOutcome

# --------------------------------------------------------------------------
# Minimal host stubs -- only the two methods these four resolvers actually call.
# --------------------------------------------------------------------------


class _DirectoryOnlyHost:
    """Resolves game directories (what the probe's live half calls) and nothing else."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises

    def resolve_game_directories(self, *, home: Path | None = None) -> GameDirectories:
        if self._raises is not None:
            raise self._raises
        root = (home or Path("/nonexistent-home")) / "civ6"
        return GameDirectories(
            saves_dir=root / "Saves" / "Single", app_options_path=root / "AppOptions.txt"
        )


class _ScriptedReader:
    """Stands in for `observe.reader.ObservationReader` -- one canned sweep, no Lua."""

    def __init__(self, results: list[CapabilityResult]) -> None:
        self._results = results

    async def __call__(self) -> tuple[list[CapabilityResult], str]:
        return self._results, "test-screen"


class _CapturingHost:
    """Returns one scripted `CaptureResult` from `capture_window`."""

    def __init__(self, result: CaptureResult) -> None:
        self._result = result

    def capture_window(self, window: GameWindow) -> CaptureResult:
        return self._result


_WINDOW = GameWindow(
    handle=1, title="Sid Meier's Civilization VI", rect=WindowRect(0, 0, 1920, 1200), pid=42
)

_LINUX_X11 = HostInfo(
    os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
)
_LINUX_WAYLAND = HostInfo(
    os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.wayland
)
_WINDOWS = HostInfo(os=OperatingSystem.windows, os_version="test", session_type=None)
_MACOS = HostInfo(os=OperatingSystem.macos, os_version="test", session_type=None)


# --------------------------------------------------------------------------
# T212 -- the R19 per-platform probe
# --------------------------------------------------------------------------


def test_linux_earns_a_verified_quicksave_path_from_the_recorded_r5_spike() -> None:
    """R5/T077 ran against a live Linux client and proved `Network.SaveGame` writes a real save.

    This is the whole point of T212: before it, `evaluate_host_gate` refused this exact host --
    the one that had already passed live validation -- because nothing ever constructed a probe
    result other than `UNPROBED`.
    """
    probe = probe_host_support(_DirectoryOnlyHost(), _LINUX_X11)

    assert probe.quicksave_path_verified is True
    assert resolve_support_tier(probe) is not SupportTier.unsupported
    assert "r5-save-path.md" in probe.reason


@pytest.mark.parametrize("host_info", [_MACOS], ids=["macos"])
def test_a_platform_with_no_recorded_save_spike_is_never_credited_with_linuxs(
    host_info: HostInfo,
) -> None:
    """R5's own "Not yet verified" item 5: Windows and macOS each confirm their own (R19).

    The Lua API is very unlikely to differ -- and that is exactly the reasoning this assertion
    exists to forbid. `resolve_support_tier` must land on `unsupported`, and the reason must name
    the platform rather than saying "not yet probed", which reads as host-specific and is not.

    Windows left this parametrization on 2026-09-20, when its own R5 spike ran against a live
    Steam client and passed -- see the test below, which holds Windows to the sharper rule.
    """
    probe = probe_host_support(_DirectoryOnlyHost(), host_info)

    assert probe.quicksave_path_verified is False
    assert resolve_support_tier(probe) is SupportTier.unsupported
    assert probe.reason.startswith(f"{host_info.os.value}:")
    assert "not yet probed" not in probe.reason


def test_windows_is_credited_with_its_own_spike_never_linuxs() -> None:
    """Windows R5 passed live (2026-09-20), so Windows is credited -- but the evidence must cite
    the *Windows* spike file. Crediting it via `r5-save-path.md` (the Linux record) would be the
    exact cross-platform generalisation R19 forbids, laundered through a passing tier.

    Capture hygiene is asserted separately un-credited: the Windows R6 spike passed occlusion but
    FAILED on in-frame overlay chrome, so a SUPPORTED-but-degraded tier is the honest one.
    """
    probe = probe_host_support(_DirectoryOnlyHost(), _WINDOWS)

    assert probe.quicksave_path_verified is True
    assert "r5-save-path-windows.md" in probe.reason
    assert "spikes/r5-save-path.md" not in probe.reason
    assert probe.capture_hygiene_spike_passed is False
    assert resolve_support_tier(probe) is SupportTier.supported


def test_an_unresolvable_save_directory_refuses_even_on_a_spiked_platform() -> None:
    """`quicksave_path_verified` is documented as "on this host", not "on this platform"."""
    probe = probe_host_support(
        _DirectoryOnlyHost(raises=PreflightError("no adapter for this host")), _LINUX_X11
    )

    assert probe.quicksave_path_verified is False
    assert resolve_support_tier(probe) is SupportTier.unsupported
    assert "could not resolve a Civ VI save directory" in probe.reason


def test_x11_capture_hygiene_is_not_credited_until_compositing_is_actually_verified() -> None:
    """The R6 spike's pass is a property of a compositing WM, and it says so in as many words:
    "treat 'X11 without a compositor' as a distinct capability case -- not VALIDATED".

    Nothing in `host/linux` verifies compositing yet (T052), so the pass must not be assumed. The
    cost is a `SUPPORTED` tier rather than `VALIDATED` -- the run still starts, recorded visually
    degraded (FR-050), which is the truthful state anyway while pixel extraction is stubbed.
    """
    unverified = probe_host_support(_DirectoryOnlyHost(), _LINUX_X11)
    verified = probe_host_support(_DirectoryOnlyHost(), _LINUX_X11, compositing_verified=True)

    assert unverified.capture_hygiene_spike_passed is False
    assert resolve_support_tier(unverified) is SupportTier.supported
    assert verified.capture_hygiene_spike_passed is True
    assert resolve_support_tier(verified) is SupportTier.validated


def test_wayland_is_never_credited_with_the_x11_capture_result() -> None:
    """The R6 spike states outright that it does not validate Wayland, even claiming compositing.

    Its portal ScreenCast path, its by-design `find_game_window` -> None, and its
    report-input-unavailable requirement are all unexecuted code on that branch.
    """
    probe = probe_host_support(_DirectoryOnlyHost(), _LINUX_WAYLAND, compositing_verified=True)

    assert probe.capture_hygiene_spike_passed is False
    assert probe.quicksave_path_verified is True  # the save path is not session-dependent
    assert resolve_support_tier(probe) is SupportTier.supported


# --------------------------------------------------------------------------
# T216 -- resolving the game's own outcome
# --------------------------------------------------------------------------


async def test_the_observation_sweep_is_what_records_the_outcome_for_the_stop_evaluator() -> None:
    """`RunnerDependencies.evaluate_stop_facts` is synchronous and runs on the runner's own event
    loop, so it cannot await a Lua read of its own. T216 resolves that by reading the outcome as
    part of each step's ordinary, declared observation sweep and recording it on the run's context
    -- which also keeps the value on the same agent-visible path as everything else (Principle I).
    """
    context = _RunContext(
        config=None,
        nexus_client=None,
        execute=None,
        registry=None,
        catalog_version=None,
        executor=None,
    )
    reader = _OutcomeTrackingObservationReader(
        _ScriptedReader(
            [
                CapabilityResult(
                    declaration_id=GAME_OUTCOME_DECLARATION_ID,
                    value={"is_game_over": True, "outcome": "victory"},
                ),
                CapabilityResult(
                    declaration_id=DeclarationId("game.turn_state"), value={"turn_number": 30}
                ),
            ]
        ),
        context,
    )

    assert context.last_game_outcome is None
    results, screen_identity = await reader()

    assert context.last_game_outcome is GameOutcome.VICTORY
    # The wrapper is transparent: it observes the sweep, it never edits it.
    assert len(results) == 2
    assert screen_identity == "test-screen"


async def test_a_run_that_never_reads_the_outcome_declaration_reports_none_not_no_victory() -> None:
    """A caller may narrow `observation_declaration_ids` past `game.outcome_state`. "We never
    looked" must stay distinguishable from "the game said no outcome has occurred"."""
    context = _RunContext(
        config=None,
        nexus_client=None,
        execute=None,
        registry=None,
        catalog_version=None,
        executor=None,
    )
    reader = _OutcomeTrackingObservationReader(
        _ScriptedReader(
            [CapabilityResult(declaration_id=DeclarationId("game.turn_state"), value={})]
        ),
        context,
    )

    await reader()

    assert context.last_game_outcome is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"is_game_over": True, "outcome": "victory"}, GameOutcome.VICTORY),
        ({"is_game_over": True, "outcome": "defeat"}, GameOutcome.DEFEAT),
        ({"is_game_over": False, "outcome": "none"}, None),
        ({"is_game_over": False, "outcome": "unresolved"}, None),
        ({"is_game_over": True}, None),
        ({"outcome": "Victory"}, None),
        ("not a mapping", None),
        (None, None),
    ],
)
def test_only_an_explicit_recognised_outcome_ever_stops_a_run(
    value: object, expected: GameOutcome | None
) -> None:
    """A malformed or unreadable outcome read must never be recorded as a defeat (FR-005, I10)."""
    assert _interpret_game_outcome(value) is expected


# --------------------------------------------------------------------------
# T221 -- the live camera state behind every capture
# --------------------------------------------------------------------------


def test_camera_state_renames_the_reveal_confirmation_the_provenance_gate_looks_for() -> None:
    """`lua/ingame/camera.lua` reports `target_is_revealed`; `parity.screening` reads
    `target_revealed`. That one rename is the whole reason `_to_camera_state` exists."""
    state = _to_camera_state(
        {"mode": "world", "zoom": 0.4, "target_plot": {"x": 3, "y": 7}, "target_is_revealed": True}
    )

    assert state["mode"] == "world"
    assert state["zoom"] == 0.4
    assert state["target_plot"] == {"x": 3, "y": 7}
    assert state["target_revealed"] is True


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "world", "zoom": 0.4},  # the reveal confirmation is simply absent
        {"mode": "world", "zoom": 0.4, "target_is_revealed": "yes"},  # and a non-bool is not one
    ],
)
def test_an_unconfirmed_revealed_target_fails_closed(value: dict[str, object]) -> None:
    """FR-026 permits no permissive default: "A camera state missing that confirmation is treated
    as *not* revealed" (`parity/screening.py`'s `CaptureAttempt`)."""
    assert _to_camera_state(value)["target_revealed"] is False


def test_an_unreadable_camera_result_becomes_an_empty_state_not_a_partial_one() -> None:
    assert _to_camera_state(None) == {}
    assert _to_camera_state("nope") == {}


# --------------------------------------------------------------------------
# T220 -- what capture mechanism this host actually resolved
# --------------------------------------------------------------------------


def test_capture_path_is_none_when_no_game_window_resolves() -> None:
    path = _resolve_capture_path(
        host=_CapturingHost(CaptureResult(status=CaptureStatus.unavailable, reason="no adapter")),
        host_info=_LINUX_X11,
        window_provider=lambda: None,
    )

    assert path is CapturePath.NONE


def test_capture_path_is_none_when_the_host_could_not_actually_capture() -> None:
    """Research R6 rank 4: "no capture path" is a legitimate recorded operating state. Naming this
    host's highest-ranked *candidate* here would record a mechanism it was never shown to use."""
    path = _resolve_capture_path(
        host=_CapturingHost(
            CaptureResult(status=CaptureStatus.failed, reason="pixel extraction not implemented")
        ),
        host_info=_LINUX_X11,
        window_provider=lambda: _WINDOW,
    )

    assert path is CapturePath.NONE


def test_capture_path_names_the_real_mechanism_once_a_frame_is_actually_produced() -> None:
    path = _resolve_capture_path(
        host=_CapturingHost(
            CaptureResult(
                status=CaptureStatus.ok,
                frame=CaptureFrame(
                    width=1920,
                    height=1200,
                    rect=_WINDOW.rect,
                    image_bytes=b"\x00",
                    image_format="PNG",
                ),
            )
        ),
        host_info=_LINUX_X11,
        window_provider=lambda: _WINDOW,
    )

    assert path is CapturePath.XCOMPOSITE


# --------------------------------------------------------------------------
# T249 -- the per-adapter halves of the `check_capture_preconditions` preflight
# --------------------------------------------------------------------------


def test_a_capture_precondition_result_always_carries_its_reason() -> None:
    """The port type makes an evidence-free verdict unrepresentable in BOTH directions: a pass
    must name what was checked (or say nothing could be) and a failure must name the unmet
    condition -- the structural half of "never a hard-coded pass"."""
    with pytest.raises(ValueError):
        CapturePreconditionResult(passed=True, reason="")
    with pytest.raises(ValueError):
        CapturePreconditionResult(passed=False, reason="   ")
    assert CapturePreconditionResult(passed=True, reason="checked X").passed is True


def test_the_linux_port_preflight_is_the_live_verified_preconditions_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T249's delegation pin: the port method's verdict IS `capture_preconditions`' verdict (the
    peer's live-verified probe), so the two cannot drift apart silently -- if the port method
    stopped consulting the legacy probe, the scripted reports below could not flip its answer.
    The peer owns live verification of the probe itself (seam note in host/linux/adapter.py)."""
    adapter = LinuxHostPlatform(session_type=LinuxSessionType.x11)

    failing = CapturePreconditions(
        composite_extension=True,
        compositing_manager=False,
        unredirect_fullscreen_windows=None,
    )
    monkeypatch.setattr(LinuxHostPlatform, "capture_preconditions", lambda self: failing)
    refused = adapter.check_capture_preconditions()
    assert refused.passed is False
    assert "compositing_manager=False" in refused.reason

    passing = CapturePreconditions(
        composite_extension=True,
        compositing_manager=True,
        unredirect_fullscreen_windows=True,  # the non-blocking warning case
    )
    monkeypatch.setattr(LinuxHostPlatform, "capture_preconditions", lambda self: passing)
    allowed = adapter.check_capture_preconditions()
    assert allowed.passed is True
    assert "unredirect" in allowed.reason  # warnings are folded into the reason, not dropped

    detailed = CapturePreconditions(
        composite_extension=False,
        compositing_manager=False,
        unredirect_fullscreen_windows=None,
        detail="python-xlib is not installed; install the 'linux' extra: boom",
    )
    monkeypatch.setattr(LinuxHostPlatform, "capture_preconditions", lambda self: detailed)
    assert adapter.check_capture_preconditions().reason == detailed.detail


def test_the_wayland_preflight_refuses_through_the_real_delegate() -> None:
    """No monkeypatch: the Wayland branch of the live-verified probe imports nothing, so the
    whole delegation runs for real on any CI host and must refuse -- the portal capture path is
    not implemented, so no window-scoped frame is obtainable on that session today."""
    result = LinuxHostPlatform(
        session_type=LinuxSessionType.wayland
    ).check_capture_preconditions()

    assert result.passed is False
    assert "Wayland" in result.reason


@pytest.mark.skipif(sys.platform != "win32", reason="resolves real user32/gdi32 entry points")
def test_windows_preflight_reports_what_it_actually_resolved() -> None:
    """On a real Windows host the check passes by RESOLVING the PrintWindow path's entry points,
    and the reason says so -- what was checked, and what a pass does not claim (frame hygiene
    stays the R6 spike's question). A bare `passed=True` with an empty story would fail here."""
    result = WindowsHostPlatform().check_capture_preconditions()

    assert result.passed is True
    assert "gdi32" in result.reason
    assert "R6" in result.reason


def test_windows_preflight_fails_closed_when_the_entry_points_cannot_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows check is a real probe, not a constant: take away the thing it checks and the
    verdict flips, carrying the underlying failure. Runs on every platform via the seam."""

    def _boom() -> object:
        raise OSError("no user32 on this host")

    monkeypatch.setattr(windows_adapter, "_gdi", _boom)
    result = WindowsHostPlatform().check_capture_preconditions()

    assert result.passed is False
    assert "no user32 on this host" in result.reason


def test_macos_preflight_is_the_screen_recording_permission_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The macOS half delegates to the same `_preflight_screen_recording` gate `capture_window`
    runs first, so the port preflight and the capture path cannot disagree about the permission
    -- and a denial's actionable reason is carried through verbatim."""
    adapter = MacOSHostPlatform()

    denial = CaptureResult(
        status=CaptureStatus.unavailable,
        reason="Screen Recording permission has not been granted to this process.",
    )
    monkeypatch.setattr(
        MacOSHostPlatform, "_preflight_screen_recording", lambda self: denial
    )
    refused = adapter.check_capture_preconditions()
    assert refused.passed is False
    assert refused.reason == denial.reason

    monkeypatch.setattr(MacOSHostPlatform, "_preflight_screen_recording", lambda self: None)
    allowed = adapter.check_capture_preconditions()
    assert allowed.passed is True
    assert "CGPreflightScreenCaptureAccess" in allowed.reason
    assert "R6" in allowed.reason  # a pass never claims what only the spike can


@pytest.mark.skipif(sys.platform == "darwin", reason="a real Mac may genuinely hold the grant")
def test_macos_preflight_refuses_for_real_where_quartz_is_absent() -> None:
    """No monkeypatch: on any non-macOS host the real delegate runs end to end and must refuse
    with the actionable install reason, never pass by default."""
    result = MacOSHostPlatform().check_capture_preconditions()

    assert result.passed is False
    assert "pyobjc" in result.reason
