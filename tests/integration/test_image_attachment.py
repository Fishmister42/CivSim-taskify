"""T238 + T240 -- images the record claims were shown, and the run-level degradation rollup.

T238 (FR-024, FR-015, FR-050, SC-013, SC-019; T134, T099): the production loop must actually
attach a screened-clean capture's bytes to the decision request handed to ``provider.complete``,
and the record -- ``ScreenCapture.shown_to_agent`` and ``Observation.captures`` -- must reflect
*actual* attachment, never intention. Before this wiring existed, ``assemble_context`` was called
without its ``images`` argument (every production request carried ``images=[]``) while
``shown_to_agent`` was set from the screening outcome alone, so the first host to produce a clean
frame would have recorded the agent as shown an image the provider never received. These tests
are far-side by construction: they assert on the ``DecisionRequest``s the provider itself
received, driven through the real ``run_turn_cycle`` orchestration against a real
``SqliteMatchStore``.

T099's rule is also enforced and asserted here: until a platform's own R6 capture-hygiene spike
has passed -- carried on the run record as ``Run.host_support_tier`` (``VALIDATED`` exactly when
it passed) -- no run on that platform may show images to the agent, however clean the frame;
those runs proceed visually degraded under FR-050 with every clean frame stored but none
attached.

T240 (FR-050, SC-013; T157): when captures degrade mid-run on a run that started ``COMPARABLE``,
the *run's* ``comparability_status`` must be downgraded on the record -- through the existing
``MatchStore.update_run`` port surface -- not just the per-step/turn ``visually_degraded`` flags.
Asserted on what the store actually received, via a delegating spy.
"""

from __future__ import annotations

import hashlib
import io
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from PIL import Image as PILImage

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.host.detect import HostInfo, LinuxSessionType, OperatingSystem
from civsim_harness.host.port import (
    CaptureFrame,
    CaptureResult,
    CaptureStatus,
    GameWindow,
    WindowRect,
)
from civsim_harness.models.catalog import (
    CameraMode,
    CameraRequirements,
    CatalogVersion,
    DeclarationKind,
    IntegrationCapability,
    ParityDeclaration,
)
from civsim_harness.models.catalog import CapabilityPath as CatalogCapabilityPath
from civsim_harness.models.common import (
    CapabilityId,
    CapturePath,
    CatalogVersionRef,
    ConfigId,
    DeclarationId,
    LuaContext,
    ModelRef,
    RunId,
    TurnCycleId,
)
from civsim_harness.models.config import ModelConfig, RunConfiguration, TurnReachedStopCondition
from civsim_harness.models.records import RunEventType, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
)
from civsim_harness.models.turn import (
    ScreenCapture,
    ScreeningStatus,
    TurnOutcome,
    WithheldReason,
)
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.port import RawDecision
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.turn_cycle import TurnCycleDependencies, run_turn_cycle
from civsim_harness.store.port import MatchStore
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

TICK_DECLARATION_ID = DeclarationId("test.tick")
GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")
VIEW_DECLARATION_ID = DeclarationId("views.test_world")

#: A small window so the fixture PNG below trivially satisfies the geometry gate; every other
#: identity field is plausible enough for the source gate (pid > 0, non-empty title).
_WINDOW = GameWindow(
    handle=1,
    title="Sid Meier's Civilization VI (FAKE)",
    rect=WindowRect(left=0, top=0, width=8, height=8),
    pid=4_242,
)

#: Matches ``views.test_world``'s declared camera requirements below -- what the real
#: composition would read live through ``camera.read_state``.
_CAMERA_STATE: dict[str, Any] = {"mode": "world", "zoom": 0.5}


def _png_bytes() -> bytes:
    """A uniform 8x8 PNG: decodes cleanly and trips none of the content gate's detectors."""
    buffer = io.BytesIO()
    PILImage.new("RGB", (_WINDOW.rect.width, _WINDOW.rect.height), (12, 44, 92)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


def _clean_capture_result(png: bytes) -> CaptureResult:
    return CaptureResult(
        status=CaptureStatus.ok,
        frame=CaptureFrame(
            width=_WINDOW.rect.width,
            height=_WINDOW.rect.height,
            rect=_WINDOW.rect,
            image_bytes=png,
            image_format="PNG",
        ),
    )


def _build_registry() -> CapabilityRegistry:
    """The test_decision_loop registry shape, plus the view declaration the captures cite --
    so screening's provenance gate and ``select_screened_images``' catalog check both resolve
    against a real ``kind=view`` entry rather than a convenience stub."""
    turn_state = ParityDeclaration(
        declaration_id=GAME_TURN_STATE_DECLARATION_ID,
        kind=DeclarationKind.OBSERVATION,
        summary="Test-only turn state.",
        parity_basis="Look at the turn counter in the top bar.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        output_schema={
            "type": "object",
            "required": ["turn_number"],
            "properties": {"turn_number": {"type": "integer"}},
        },
        introduced_in_version="test",
    )
    tick = ParityDeclaration(
        declaration_id=TICK_DECLARATION_ID,
        kind=DeclarationKind.ACTION,
        summary="Test-only action: always available, always advances the counter by one.",
        parity_basis="Click a UI element that always advances the test counter.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        availability_predicate="true",
        verification_predicate="game.turn_number == observed_turn_number + 1",
        introduced_in_version="test",
    )
    view = ParityDeclaration(
        declaration_id=VIEW_DECLARATION_ID,
        kind=DeclarationKind.VIEW,
        summary="Test-only world view.",
        parity_basis="Look at the world view on screen.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        output_schema={"type": "object"},
        camera_requirements=CameraRequirements(
            mode=CameraMode.WORLD, zoom_range=(0.0, 1.0), target_must_be_revealed=False
        ),
        screening_profile="default",
        introduced_in_version="test",
    )
    capability = IntegrationCapability(
        capability_id=CapabilityId("test.turn_control"),
        path=CatalogCapabilityPath.FIRETUNER,
        implementation_ref="test",
        reads=["turn state"],
        writes=["turn state"],
    )
    declarations = (turn_state, tick, view)
    catalog = Catalog(
        root=Path("."),
        version=CatalogVersion(
            version="test",
            content_hash="test",
            declaration_ids=[d.declaration_id for d in declarations],
        ),
        declarations=MappingProxyType({d.declaration_id: d for d in declarations}),
        capabilities=MappingProxyType({capability.capability_id: capability}),
    )
    return CapabilityRegistry(catalog=catalog)


class _FakeGame:
    def __init__(self) -> None:
        self.turn_number = 1

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        return (
            [
                CapabilityResult(
                    declaration_id=GAME_TURN_STATE_DECLARATION_ID,
                    value={"turn_number": self.turn_number},
                )
            ],
            "world",
        )

    async def execute(
        self, declaration_id: DeclarationId, parameters: Mapping[str, Any], target: Any
    ) -> None:
        if declaration_id == TICK_DECLARATION_ID:
            self.turn_number += 1


class _FakeSaveCapability:
    def __init__(self, host: FakeHostPlatform, home: Path) -> None:
        self._host = host
        self._home = home

    async def save_game(self, save_name: str) -> None:
        saves_dir = self._host.resolve_game_directories(home=self._home).saves_dir
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / f"{save_name}.Civ6Save").write_bytes(b"fake-save-data")


class _FakeSaveLoader:
    async def load(self, save: SavePoint) -> None:  # pragma: no cover - not exercised here
        return None


class _SpyStore:
    """Delegates every ``MatchStore`` call to *inner*, recording ``write_capture`` (record and
    blob) and ``update_run`` calls so a test can assert on exactly what the store received."""

    def __init__(self, inner: MatchStore) -> None:
        self._inner = inner
        self.capture_writes: list[tuple[ScreenCapture, bytes | None]] = []
        self.update_run_calls: list[tuple[RunId, dict[str, Any]]] = []

    def write_capture(self, capture: ScreenCapture, blob: bytes | None) -> Any:
        self.capture_writes.append((capture, blob))
        return self._inner.write_capture(capture, blob)

    def update_run(self, run_id: RunId, **fields: Any) -> None:
        self.update_run_calls.append((run_id, dict(fields)))
        self._inner.update_run(run_id, **fields)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _MidRunFailingCaptureHost(FakeHostPlatform):
    """Serves a fixed number of clean frames, then fails every later capture attempt -- the T240
    scenario: a run whose captures were fine at the start and degrade mid-run.

    *clean_frames* is how many reads happen before the loss, counted from the turn's very first
    read. Since c795039 a turn's first read is the pre-save prompt probe's, one read ahead of
    the first decision step -- so a scenario that wants the *step* to start clean scripts one
    frame for the probe and one for the step.
    """

    def __init__(self, frame: CaptureResult, *, clean_frames: int = 1) -> None:
        super().__init__()
        self._frame = frame
        self._remaining = clean_frames

    def capture_window(self, window: GameWindow) -> CaptureResult:
        self.capture_calls.append(window)
        if self._remaining > 0:
            self._remaining -= 1
            return self._frame
        return CaptureResult(
            status=CaptureStatus.failed, reason="scripted mid-run capture loss"
        )


def _build_run_and_config(
    run_id: RunId, *, tier: HostSupportTier, comparability: ComparabilityStatus
) -> tuple[Run, RunConfiguration]:
    now = datetime.now(UTC)
    config = RunConfiguration(
        config_id=ConfigId("cfg-1"),
        map_seed="seed",
        civilization="civ",
        leader="leader",
        ruleset="ruleset",
        difficulty="prince",
        stop_condition=TurnReachedStopCondition(turn=50),
        agent_model_config=ModelConfig(primary=ModelRef(provider="test", model="test-model")),
        no_progress_step_limit=5,
        recovery_attempt_limit=3,
        min_free_disk_gb=1.0,
        created_at=now,
    )
    run = Run(
        run_id=run_id,
        config_id=config.config_id,
        lifecycle_state=LifecycleState.PLAYING,
        record_completeness_status=RecordCompletenessStatus.COMPLETE,
        comparability_status=comparability,
        observation_catalog_version=CatalogVersionRef(version="test", content_hash="test"),
        action_catalog_version=CatalogVersionRef(version="test", content_hash="test"),
        game_build="test/1.0",
        host_support_tier=tier,
        capture_path=CapturePath.XCOMPOSITE,
    )
    return run, config


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


@pytest.fixture
def text_evidence_plumbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply the text evidence ``run/decision_loop.py`` does not yet gather.

    **This fixture is a marker for a known production gap, not a convenience.** The content gate
    screens most reject categories (``firetuner_window``, ``developer_console``,
    ``harness_owned_ui``, the per-platform panel/taskbar/dock ids) with exactly one technique --
    matching the category's tokens against text observed on the desktop -- and that technique
    cannot run unless a caller enumerates window titles and passes the result. The production
    call site (``run/decision_loop.py``'s ``capture_for_step(...)``) passes nothing, so on the
    real harness every capture is now withheld: see
    ``test_the_unplumbed_production_path_withholds_every_frame`` below, which asserts exactly
    that, deliberately without this fixture.

    The tests that use this fixture are about *attachment* -- that a clean frame's bytes reach
    the provider and the records agree -- not about whether the loop gathers evidence. Patching
    the evidence in keeps that coverage alive and localises the gap to one named place. **Delete
    this fixture the moment the loop supplies ``detected_text_tokens`` itself**; the tests should
    then pass untouched.
    """
    import civsim_harness.run.decision_loop as decision_loop

    real_capture_for_step = decision_loop.capture_for_step

    def _with_text_evidence(**kwargs: Any) -> Any:
        kwargs.setdefault("detected_text_tokens", frozenset())
        return real_capture_for_step(**kwargs)

    monkeypatch.setattr(decision_loop, "capture_for_step", _with_text_evidence)


def _make_deps(
    *,
    tmp_path: Path,
    run_id: RunId,
    store: MatchStore,
    game: _FakeGame,
    provider: FakeModelProvider,
    host: FakeHostPlatform,
) -> TurnCycleDependencies:
    registry = _build_registry()
    host_info = HostInfo(
        os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
    )

    def build_loop_context(turn_cycle_id: TurnCycleId) -> DecisionLoopContext:
        return DecisionLoopContext(
            run_id=run_id,
            turn_number=1,
            turn_cycle_id=turn_cycle_id,
            registry=registry,
            catalog_version=CatalogVersionRef(version="test", content_hash="test"),
            model=ModelRef(provider="test", model="test-model"),
            guidance=None,
            provider=provider,
            no_progress_step_limit=5,
            read_observation_inputs=game.read,
            execute_action=game.execute,
            host=host,
            host_info=host_info,
            view_declaration_id=VIEW_DECLARATION_ID,
            screening_profiles=load_screening_profiles(),
            store=store,
            window_provider=lambda: _WINDOW,
            camera_state_provider=lambda: dict(_CAMERA_STATE),
        )

    return TurnCycleDependencies(
        run_id=run_id,
        turn_number=1,
        store=store,
        save_capability=_FakeSaveCapability(host, tmp_path),
        host=host,
        min_free_disk_gb=1.0,
        disk_check_path=tmp_path,
        build_loop_context=build_loop_context,
        recovery=RecoveryEngine(
            run_id=run_id, store=store, loader=_FakeSaveLoader(), recovery_attempt_limit=3
        ),
        home=tmp_path,
    )


def _tick(*, is_end_turn: bool = False) -> RawDecision:
    return RawDecision(
        action_declaration_id=TICK_DECLARATION_ID,
        reasoning="scripted step",
        parameters={},
        is_end_turn=is_end_turn,
        prompt_type=None,
    )


# --------------------------------------------------------------------------
# T238 -- screened images actually reach the provider, and the record says only what happened
# --------------------------------------------------------------------------


async def test_screened_clean_capture_bytes_reach_the_provider_request(
    tmp_path: Path, text_evidence_plumbed: None
) -> None:
    """T238's forward half, asserted on the far side of the port: the step's screened-clean
    frame's exact bytes arrive in the ``DecisionRequest`` the provider received, and every piece
    of the record -- ``shown_to_agent``, the observation's capture listing, the model call's
    ``image_count`` -- reports that actual attachment. The turn's final fresh read (taken only to
    verify the end-turn effect) never serves a decision, so its equally-clean capture must be
    recorded *not* shown: shown-ness is attachment, not screening."""
    run_id = RunId("run-image-attach")
    run, config = _build_run_and_config(
        run_id, tier=HostSupportTier.VALIDATED, comparability=ComparabilityStatus.COMPARABLE
    )
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)
    spy = _SpyStore(store)

    png = _png_bytes()
    host = FakeHostPlatform()
    host.set_capture_result(_clean_capture_result(png))

    provider = FakeModelProvider()
    provider.queue_decision(_tick(is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=spy, game=_FakeGame(), provider=provider, host=host
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
        assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT

        # Far side of the port: the provider's own received request carries the frame's bytes.
        assert len(provider.calls) == 1
        request = provider.calls[0]
        assert [image.data for image in request.images] == [png]
        assert request.images[0].media_type == "image/png"

        # Four writes over three captures: the pre-save prompt probe's read (c795039: a turn
        # probes the screen before its quicksave; it served no decision here, so recorded not
        # shown), then the decision step's own frame written **twice** -- un-shown before the
        # dispatch and upgraded to shown once `provider.complete` returned (T260) -- and finally
        # the post-end-turn verification read's (clean, but it served no request, so recorded
        # not shown).
        assert len(spy.capture_writes) == 4
        probe_capture, probe_blob = spy.capture_writes[0]
        step_before, step_before_blob = spy.capture_writes[1]
        step_capture, step_blob = spy.capture_writes[2]
        trailing_capture, trailing_blob = spy.capture_writes[3]
        assert probe_capture.screening_status is ScreeningStatus.SCREENED_CLEAN
        assert probe_capture.shown_to_agent is False
        assert probe_blob == png
        # The pre-dispatch write is the same frame, durable, saying only what was true then.
        assert step_before.capture_id == step_capture.capture_id
        assert step_before.shown_to_agent is False
        assert step_before_blob == png
        assert step_capture.screening_status is ScreeningStatus.SCREENED_CLEAN
        assert step_capture.shown_to_agent is True
        assert step_blob == png
        assert step_capture.blob_ref == hashlib.sha256(png).hexdigest()
        assert trailing_capture.screening_status is ScreeningStatus.SCREENED_CLEAN
        assert trailing_capture.shown_to_agent is False
        assert trailing_blob == png

        # The persisted record agrees end to end: the step's observation lists exactly the
        # capture that was attached, the step is not degraded, and the model call accounts for
        # exactly the one image the request carried (P2, FR-039).
        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        assert len(record.steps) == 1
        bundle = record.steps[0]
        assert bundle.observation.captures == [step_capture.capture_id]
        assert bundle.step.visually_degraded is False
        assert bundle.model_call.image_count == 1

        # And the read-back capture record says the same thing the write did.
        stored = store.get_capture(step_capture.capture_id)
        assert stored is not None
        assert stored.shown_to_agent is True
    finally:
        store.close()


async def test_the_unplumbed_production_path_withholds_every_frame(tmp_path: Path) -> None:
    """The Principle I hole, asserted end to end on the real loop -- deliberately *without*
    ``text_evidence_plumbed``.

    ``run/decision_loop.py`` does not enumerate window titles, so the content gate has no
    technique for most of its own reject categories and cannot certify any frame. Every capture
    must therefore come back withheld and no image may reach the provider -- on a VALIDATED host
    serving a frame that is, pixel for pixel, the same one the test above delivers. This is the
    intended consequence of the gate failing closed, and this test is what will go red when the
    loop finally supplies the evidence (delete it then, together with the fixture).
    """
    run_id = RunId("run-image-unplumbed")
    run, config = _build_run_and_config(
        run_id, tier=HostSupportTier.VALIDATED, comparability=ComparabilityStatus.COMPARABLE
    )
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)
    spy = _SpyStore(store)

    host = FakeHostPlatform()
    host.set_capture_result(_clean_capture_result(_png_bytes()))

    provider = FakeModelProvider()
    provider.queue_decision(_tick(is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=spy, game=_FakeGame(), provider=provider, host=host
    )

    try:
        await run_turn_cycle(deps, run=run)

        assert provider.calls
        assert all(call.images == [] for call in provider.calls)
        assert spy.capture_writes
        for capture, blob in spy.capture_writes:
            assert capture.screening_status is ScreeningStatus.WITHHELD
            assert capture.withheld_reason is WithheldReason.NON_PLAYER_UI
            assert capture.shown_to_agent is False
            assert capture.blob_ref is None
            assert blob is None
    finally:
        store.close()


async def test_no_image_reaches_the_agent_on_a_platform_without_a_passed_r6_spike(
    tmp_path: Path, text_evidence_plumbed: None
) -> None:
    """T099's rule, enforced in production: ``Run.host_support_tier`` is ``VALIDATED`` exactly
    when this platform's own R6 capture-hygiene spike passed, and on any other tier no image may
    reach the agent -- however clean the frame. The clean frame is still screened and durably
    stored (FR-030 governs stored captures), but the provider receives no image and every record
    says none was shown."""
    run_id = RunId("run-r6-gate")
    # A SUPPORTED host's run is created VISUALLY_DEGRADED by the host gate (FR-050/FR-054).
    run, config = _build_run_and_config(
        run_id,
        tier=HostSupportTier.SUPPORTED,
        comparability=ComparabilityStatus.VISUALLY_DEGRADED,
    )
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)
    spy = _SpyStore(store)

    png = _png_bytes()
    host = FakeHostPlatform()
    host.set_capture_result(_clean_capture_result(png))

    provider = FakeModelProvider()
    provider.queue_decision(_tick(is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=spy, game=_FakeGame(), provider=provider, host=host
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
        assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT

        # No image reached the provider...
        assert len(provider.calls) == 1
        assert provider.calls[0].images == []

        # ...and the record says none did, while the clean frames themselves are still stored
        # as evidence, blob and all -- the pre-save prompt probe's read (c795039), the decision
        # step's own, and the trailing end-turn verification read's.
        assert len(spy.capture_writes) == 3
        for capture, blob in spy.capture_writes:
            assert capture.screening_status is ScreeningStatus.SCREENED_CLEAN
            assert capture.shown_to_agent is False
            assert blob == png

        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.steps[0].observation.captures == []
        assert record.steps[0].model_call.image_count == 0

        # Clean captures on an already-degraded run change nothing about comparability either:
        # the store received no run update at all.
        assert spy.update_run_calls == []
    finally:
        store.close()


# --------------------------------------------------------------------------
# T240 -- mid-run capture degradation downgrades Run.comparability_status
# --------------------------------------------------------------------------


async def test_mid_run_capture_degradation_downgrades_run_comparability(
    tmp_path: Path, text_evidence_plumbed: None
) -> None:
    """A run that starts ``COMPARABLE`` on a validated host and loses its images mid-run must
    serve a downgraded ``comparability_status`` from the record -- not just per-step
    ``visually_degraded`` flags. Asserted on what the store received: exactly one
    ``update_run(comparability_status=VISUALLY_DEGRADED)``, applied when the first degraded
    capture appeared and not repeated for later ones (the downgrade is COMPARABLE-only, and
    nothing ever moves the status back up)."""
    run_id = RunId("run-degrade-midrun")
    run, config = _build_run_and_config(
        run_id, tier=HostSupportTier.VALIDATED, comparability=ComparabilityStatus.COMPARABLE
    )
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)
    spy = _SpyStore(store)

    png = _png_bytes()
    # Two clean frames: one for the pre-save prompt probe's read (c795039 -- it precedes the
    # turn's first decision step and would otherwise consume the run's only good frame), one for
    # decision step 1, and the loss starts at step 2 exactly as this scenario intends.
    host = _MidRunFailingCaptureHost(_clean_capture_result(png), clean_frames=2)

    provider = FakeModelProvider()
    provider.queue_decision(_tick())
    provider.queue_decision(_tick(is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=spy, game=_FakeGame(), provider=provider, host=host
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
        assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT

        # The run's record was downgraded, exactly once, through the existing port surface.
        assert spy.update_run_calls == [
            (run_id, {"comparability_status": ComparabilityStatus.VISUALLY_DEGRADED})
        ]
        updated = store.get_run(run_id)
        assert updated is not None
        assert updated.comparability_status is ComparabilityStatus.VISUALLY_DEGRADED

        # The step-level story matches: step 1 had its image (the run really did start with
        # working captures -- this is a mid-run loss, not a from-birth degradation), step 2 lost
        # it, and the turn rolls the flag up (T157's already-real half).
        assert len(provider.calls) == 2
        assert [image.data for image in provider.calls[0].images] == [png]
        assert provider.calls[1].images == []

        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.steps[0].step.visually_degraded is False
        assert record.steps[1].step.visually_degraded is True
        assert record.turn_cycle.visually_degraded is True

        # And the degradation is itself on the timeline, naming the step that lost its image.
        capture_failures = store.list_run_events(
            run_id, event_types=[RunEventType.CAPTURE_FAILED]
        )
        assert [event.step_index for event in capture_failures] == [2, 3]
    finally:
        store.close()


async def test_an_already_degraded_run_is_never_rewritten_on_further_degradation(
    tmp_path: Path,
) -> None:
    """The downgrade's guard, from the other side: a run that never was ``COMPARABLE`` (a
    SUPPORTED host, degraded from birth under FR-050) keeps losing captures without the store
    ever receiving a run update -- there is nothing truthful for ``update_run`` to say."""
    run_id = RunId("run-degraded-from-birth")
    run, config = _build_run_and_config(
        run_id,
        tier=HostSupportTier.SUPPORTED,
        comparability=ComparabilityStatus.VISUALLY_DEGRADED,
    )
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)
    spy = _SpyStore(store)

    host = FakeHostPlatform()
    host.set_capture_failed("scripted: no frame ever comes back")

    provider = FakeModelProvider()
    provider.queue_decision(_tick(is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=spy, game=_FakeGame(), provider=provider, host=host
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
        assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT

        assert spy.update_run_calls == []
        unchanged = store.get_run(run_id)
        assert unchanged is not None
        assert unchanged.comparability_status is ComparabilityStatus.VISUALLY_DEGRADED
    finally:
        store.close()


# --------------------------------------------------------------------------
# T260 -- the shown flag is written after the dispatch, never before it
# --------------------------------------------------------------------------


class _DispatchInterrupted(RuntimeError):
    """The provider call died in flight, after the request left the harness."""


class _ProviderInterruptedMidDispatch(FakeModelProvider):
    """Accepts the request -- images and all -- and then never returns a response.

    This is the exact shape of the run that produced the defect: the frame was handed to the
    provider and the step never got any further. A transport failure, a killed process and an
    operator pause all land here.
    """

    def complete(self, request: Any) -> Any:
        self.calls.append(request)
        raise _DispatchInterrupted("scripted: the provider call died in flight")


async def test_a_dispatch_that_never_returns_leaves_the_capture_recorded_not_shown(
    tmp_path: Path, text_evidence_plumbed: None
) -> None:
    """T260, FR-015, SC-019 -- the case that produced every bad row on the live store, and the
    one the suite had no test for.

    ``shown_to_agent`` is a durable claim that the agent was shown this frame, and until T260 the
    loop wrote it **before** calling ``provider.complete``: one line early, and therefore an
    intention, not an outcome -- while ``decision_loop``'s own comment and ``_FreshObservation``'s
    docstring both asserted the opposite ("reports what happened, never an intention").

    MEASURED on the live store, 2026-09-22: **13 captures carry ``shown_to_agent=True`` for
    decision steps that have no ``model_calls`` row and no ``decision_steps`` row at all** --
    ``run-4c0b8fb4`` (2), ``run-a06e8e68`` (10), ``run-1091122b`` (1), every one of them
    ``lifecycle_state=paused`` / ``record_completeness_status=has_gaps``. The reverse direction
    was clean (0 model calls with ``image_count > 0`` and no shown capture), which is the
    signature of a flag written ahead of its own outcome: a successful ``ModelCall`` rides along
    in the end-of-turn ``TurnCycleRecord``, so a turn interrupted inside ``complete()`` left the
    "the agent saw this" claim behind with every piece of its evidence gone.

    So: the request goes out carrying the image (asserted here on the provider's own call log),
    the call dies, and the record must say **not shown** -- the only thing that is settled. It
    now agrees with the absence of a model call instead of contradicting it, and the frame itself
    is still durable, because the un-shown write happens *before* the dispatch (FR-051, D5).
    """
    run_id = RunId("run-dispatch-interrupted")
    run, config = _build_run_and_config(
        run_id, tier=HostSupportTier.VALIDATED, comparability=ComparabilityStatus.COMPARABLE
    )
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)
    spy = _SpyStore(store)

    png = _png_bytes()
    host = FakeHostPlatform()
    host.set_capture_result(_clean_capture_result(png))

    provider = _ProviderInterruptedMidDispatch()
    provider.queue_decision(_tick(is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=spy, game=_FakeGame(), provider=provider, host=host
    )

    try:
        with pytest.raises(_DispatchInterrupted):
            await run_turn_cycle(deps, run=run)

        # The image really did leave the harness -- this is not a test of a request that was
        # never built. The frame was attached; what never happened is the call returning.
        assert len(provider.calls) == 1
        assert [image.data for image in provider.calls[0].images] == [png]

        # Not one capture the store received claims to have been shown.
        assert spy.capture_writes
        assert all(capture.shown_to_agent is False for capture, _blob in spy.capture_writes)

        # And the durable record agrees -- including the step's own frame, whose bytes survived
        # the interruption precisely because the un-shown write precedes the dispatch.
        stored = store.list_captures(run_id)
        assert stored
        assert all(capture.shown_to_agent is False for capture in stored)
        step_frames = [
            capture
            for capture in stored
            if capture.screening_status is ScreeningStatus.SCREENED_CLEAN
        ]
        assert step_frames
        assert all(capture.blob_ref == hashlib.sha256(png).hexdigest() for capture in step_frames)

        # The evidence the 13 rows were missing: there is none, and now nothing claims otherwise.
        assert store.list_model_calls(run_id) == []
        assert store.get_turn_cycle(run_id, 1, authoritative_only=False) is None
    finally:
        store.close()
