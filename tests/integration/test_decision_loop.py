"""T059 -- the within-turn loop's freshness discipline, end to end.

FR-008 / FR-015 / invariants I13, I14: each decision step gets a freshly assembled observation
(taken *after* the previous step's effect was verified) and a distinct capture, and exactly one
model call serves it -- never a batch, never a reused view. This file drives the full
``run/turn_cycle.py`` orchestration against a real ``SqliteMatchStore`` and asserts against the
persisted record and the provider's own call log, not merely against ``decision_loop``'s in-memory
return value.

Also covers the spec's self-cancellation edge case (data-model.md SS6): an agent that undoes its
own earlier work later in the same turn, having seen the result, produces two steps both recorded
as issued and both ``changed_state`` -- and the turn's recorded yields reflect the *resulting*
state, not the discarded intent.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from civsim_harness.act.camera import CAMERA_SET_VIEW_MODE
from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import ParityViolation
from civsim_harness.host.detect import HostInfo, LinuxSessionType, OperatingSystem
from civsim_harness.host.port import HostPlatform
from civsim_harness.models.catalog import CapabilityPath as CatalogCapabilityPath
from civsim_harness.models.catalog import (
    CatalogVersion,
    DeclarationKind,
    IntegrationCapability,
    ParityDeclaration,
)
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
from civsim_harness.models.decision import RejectionReason
from civsim_harness.models.records import ModelCall, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
)
from civsim_harness.models.turn import ScreenCapture, StepProgress, TurnOutcome
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
UNTICK_DECLARATION_ID = DeclarationId("test.untick")
STUCK_DECLARATION_ID = DeclarationId("test.stuck")
GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")


def _build_registry() -> CapabilityRegistry:
    turn_state = ParityDeclaration(
        declaration_id=GAME_TURN_STATE_DECLARATION_ID,
        kind=DeclarationKind.OBSERVATION,
        summary="Test-only turn state.",
        parity_basis="Look at the turn counter in the top bar.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        output_schema={
            "type": "object",
            "required": ["turn_number", "is_local_player_turn", "is_waiting_for_other_players"],
            "properties": {
                "turn_number": {"type": "integer"},
                "is_local_player_turn": {"type": "boolean"},
                "is_waiting_for_other_players": {"type": "boolean"},
            },
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
    untick = ParityDeclaration(
        declaration_id=UNTICK_DECLARATION_ID,
        kind=DeclarationKind.ACTION,
        summary="Test-only action: always available, always retreats the counter by one -- the "
        "self-cancelling counterpart to test.tick.",
        parity_basis="Click a UI element that always retreats the test counter.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        availability_predicate="true",
        verification_predicate="game.turn_number == observed_turn_number - 1",
        introduced_in_version="test",
    )
    stuck = ParityDeclaration(
        declaration_id=STUCK_DECLARATION_ID,
        kind=DeclarationKind.ACTION,
        summary="Test-only action: always available, never actually changes anything.",
        parity_basis="Click a UI element that always exists but never does anything.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        availability_predicate="true",
        verification_predicate="game.turn_number == observed_turn_number + 1",
        introduced_in_version="test",
    )
    capability = IntegrationCapability(
        capability_id=CapabilityId("test.turn_control"),
        path=CatalogCapabilityPath.FIRETUNER,
        implementation_ref="test",
        reads=["turn state"],
        writes=["turn state"],
    )
    # T230: a real camera declaration, copied verbatim from `catalogs/actions/camera.yaml`, so
    # the parity bound under test is the catalog's own and not a convenience restatement of it.
    set_view_mode = ParityDeclaration(
        declaration_id=CAMERA_SET_VIEW_MODE,
        kind=DeclarationKind.ACTION,
        summary="Toggle between the world view and the strategic (zoomed-out) view.",
        parity_basis=(
            "Press the strategic-view hotkey (or its menu toggle) to switch between world and "
            "strategic view."
        ),
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        availability_predicate='target == "world" or target == "strategic"',
        verification_predicate="camera.mode == target",
        introduced_in_version="test",
    )
    declarations = (turn_state, tick, untick, stuck, set_view_mode)
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
        self.reads: list[int] = []

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        self.reads.append(self.turn_number)
        return (
            [
                CapabilityResult(
                    declaration_id=GAME_TURN_STATE_DECLARATION_ID,
                    value={
                        "turn_number": self.turn_number,
                        "is_local_player_turn": True,
                        "is_waiting_for_other_players": False,
                    },
                )
            ],
            "world_view",
        )

    async def execute(
        self, declaration_id: DeclarationId, parameters: Mapping[str, Any], target: Any
    ) -> None:
        if declaration_id == TICK_DECLARATION_ID:
            self.turn_number += 1
        elif declaration_id == UNTICK_DECLARATION_ID:
            self.turn_number -= 1
        # test.stuck: deliberately does nothing.


class _FakeSaveCapability:
    def __init__(self, host: HostPlatform, home: Path) -> None:
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
    """Delegates every ``MatchStore`` call to *inner*, recording ``write_capture`` and
    ``write_model_call`` calls so a test can assert on exactly what was written and when, without
    reaching into ``SqliteMatchStore``'s private schema."""

    def __init__(self, inner: MatchStore) -> None:
        self._inner = inner
        self.capture_writes: list[ScreenCapture] = []
        self.model_call_writes: list[ModelCall] = []

    def write_capture(self, capture: ScreenCapture, blob: bytes | None) -> Any:
        self.capture_writes.append(capture)
        return self._inner.write_capture(capture, blob)

    def write_model_call(self, call: ModelCall) -> Any:
        self.model_call_writes.append(call)
        return self._inner.write_model_call(call)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _build_run_and_config(run_id: RunId) -> tuple[Run, RunConfiguration]:
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
        comparability_status=ComparabilityStatus.COMPARABLE,
        observation_catalog_version=CatalogVersionRef(version="test", content_hash="test"),
        action_catalog_version=CatalogVersionRef(version="test", content_hash="test"),
        game_build="test/1.0",
        host_support_tier=HostSupportTier.VALIDATED,
        capture_path=CapturePath.NONE,
    )
    return run, config


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _make_deps(
    *,
    tmp_path: Path,
    run_id: RunId,
    turn_number: int,
    store: MatchStore,
    game: _FakeGame,
    provider: FakeModelProvider,
    no_progress_step_limit: int,
    compute_yields: Any = None,
) -> TurnCycleDependencies:
    registry = _build_registry()
    host = FakeHostPlatform()
    host_info = HostInfo(
        os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
    )

    def build_loop_context(turn_cycle_id: TurnCycleId) -> DecisionLoopContext:
        return DecisionLoopContext(
            run_id=run_id,
            turn_number=turn_number,
            turn_cycle_id=turn_cycle_id,
            registry=registry,
            catalog_version=CatalogVersionRef(version="test", content_hash="test"),
            model=ModelRef(provider="test", model="test-model"),
            guidance=None,
            provider=provider,
            no_progress_step_limit=no_progress_step_limit,
            read_observation_inputs=game.read,
            execute_action=game.execute,
            host=host,
            host_info=host_info,
            view_declaration_id=DeclarationId("views.test"),
            screening_profiles=load_screening_profiles(),
            store=store,
        )

    kwargs: dict[str, Any] = dict(
        run_id=run_id,
        turn_number=turn_number,
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
    if compute_yields is not None:
        kwargs["compute_yields"] = compute_yields
    return TurnCycleDependencies(**kwargs)


def _plain_decision(declaration_id: DeclarationId, *, is_end_turn: bool = False) -> RawDecision:
    return RawDecision(
        action_declaration_id=declaration_id,
        reasoning="scripted step",
        parameters={},
        is_end_turn=is_end_turn,
        prompt_type=None,
    )


async def test_each_steps_observation_is_assembled_after_the_previous_effect_was_verified(
    tmp_path: Path,
) -> None:
    run_id = RunId("run-freshness")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)
    spy = _SpyStore(store)

    game = _FakeGame()
    provider = FakeModelProvider()
    for _ in range(4):
        provider.queue_decision(_plain_decision(TICK_DECLARATION_ID))
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID, is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=spy,
        game=game,
        provider=provider,
        no_progress_step_limit=5,
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
    finally:
        store.close()

    assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT

    store2 = SqliteMatchStore(tmp_path / "match.db")
    try:
        record = store2.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.turn_cycle.step_count == 5

        # Step n's own observation shows exactly n-1 prior ticks having already landed -- i.e. it
        # was assembled strictly after step n-1's effect was executed and verified, never before.
        for step_index, bundle in enumerate(record.steps, start=1):
            turn_value = next(
                entry.value["turn_number"]
                for entry in bundle.observation.entries
                if entry.declaration_id == GAME_TURN_STATE_DECLARATION_ID
            )
            assert turn_value == step_index

        # Exactly one model call served each step (never a batch, never omitted).
        assert len(provider.calls) == 5
        assert [request.step_index for request in provider.calls] == [1, 2, 3, 4, 5]

        # Exactly one capture write per step transition (5 steps -> 6 fresh reads: the initial
        # one plus one after each step's dispatch), every one bound to a distinct decision step,
        # and every one of the 5 persisted steps' own decision_step_id has exactly one capture.
        assert len(spy.capture_writes) == 6
        capture_step_ids = [c.decision_step_id for c in spy.capture_writes]
        assert len(capture_step_ids) == len(set(capture_step_ids))  # no capture reused
        recorded_step_ids = {b.step.decision_step_id for b in record.steps}
        assert recorded_step_ids <= set(capture_step_ids)

        # No two steps share an observation_id either.
        observation_ids = [b.observation.observation_id for b in record.steps]
        assert len(observation_ids) == len(set(observation_ids))
    finally:
        store2.close()


async def test_self_cancellation_both_steps_recorded_and_yields_reflect_resulting_state(
    tmp_path: Path,
) -> None:
    run_id = RunId("run-self-cancel")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    game = _FakeGame()
    provider = FakeModelProvider()
    # Step 1: advance. Step 2: having seen the board reflect that, undo it. Step 3: end turn
    # without touching the counter again -- the net effect over the whole turn is zero.
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID))
    provider.queue_decision(_plain_decision(UNTICK_DECLARATION_ID))
    provider.queue_decision(_plain_decision(STUCK_DECLARATION_ID, is_end_turn=True))

    # No real per-turn yield computation exists anywhere in this codebase yet (out of this wave's
    # scope) -- `compute_yields` is `TurnCycleDependencies`' own injection point for it. This
    # closure stands in for "whatever the harness derives from final game state", reading the
    # *same* FakeGame instance `execute()` mutated, specifically to prove the hook sees the
    # resulting state rather than being fed anything about the agent's first, later-reversed
    # intent.
    def compute_yields(_result: Any) -> dict[str, int]:
        return {"final_turn_number": game.turn_number}

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=game,
        provider=provider,
        no_progress_step_limit=5,
        compute_yields=compute_yields,
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
    finally:
        store.close()

    assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
    # The tick and the untick cancelled out; the game ends this turn exactly where it started.
    assert game.turn_number == 1

    store2 = SqliteMatchStore(tmp_path / "match.db")
    try:
        record = store2.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.turn_cycle.step_count == 3

        tick_step, untick_step, end_step = record.steps
        # Both the original action and its own later reversal are recorded as issued...
        assert tick_step.decision.action_declaration_id == TICK_DECLARATION_ID
        assert untick_step.decision.action_declaration_id == UNTICK_DECLARATION_ID
        # ...and both genuinely verified as having changed game state at the moment each was
        # dispatched -- ordering is preserved, nothing is silently dropped or merged.
        assert tick_step.step.progress.value == "changed_state"
        assert untick_step.step.progress.value == "changed_state"
        assert end_step.decision.is_end_turn is True

        # The turn's recorded yields reflect the *resulting* state (net zero change), not the
        # first step's intent (which alone would have suggested the counter advanced).
        assert record.turn_cycle.yields == {"final_turn_number": 1}
    finally:
        store2.close()


class _LeakingGame(_FakeGame):
    """A client whose declared capability returns one field it had no business returning.

    Exactly the case the preventive controls do **not** stop: `test.turn_state`'s own
    `output_schema` sets no `additionalProperties: false`, so an extra key validates, resolves
    against a real declaration, and is attributed to it like any legitimate field. Only the
    forbidden-field guard notices that `unrevealed_tiles` is not something a human could read
    off the top bar (FR-019).
    """

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        results, screen = await super().read()
        value = dict(results[0].value)
        value["unrevealed_tiles"] = [{"x": 12, "y": 7, "resource": "RESOURCE_IRON"}]
        return ([CapabilityResult(declaration_id=results[0].declaration_id, value=value)], screen)


async def test_a_leaked_field_halts_the_run_instead_of_reaching_the_agent(
    tmp_path: Path,
) -> None:
    """T225 / Constitution Principle I: the forbidden-field guard runs on the real loop's context.

    The guard (`parity/forbidden.py`, T128) was complete and tested from the day it was written,
    and was called from **nowhere in `src/`** -- so every assurance it provided came from tests
    invoking it directly, never from the path the agent's context actually travels. Asserting it
    here, through `run_turn_cycle` against a leaking client, is the difference.

    Principle I is non-negotiable and admits no partial compliance, so the expected behaviour is
    that the run **stops**: the `ParityViolation` propagates uncaught (for `run/runner.py` to
    record as a run failure) and no provider call is ever made with the leaked context.
    """
    run_id = RunId("run-parity-leak")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    game = _LeakingGame()
    provider = FakeModelProvider()
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID, is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=game,
        provider=provider,
        no_progress_step_limit=5,
    )

    try:
        with pytest.raises(ParityViolation) as excinfo:
            await run_turn_cycle(deps, run=run)

        assert excinfo.value.detail["by_category"] == {"game_state_leakage": 1}

        # The leak never reached the model: the guard runs before `provider.complete`.
        assert provider.calls == []

        # And the turn never came into existence as a record, so nothing downstream can mistake
        # this for a turn that merely lacked images (FR-050) -- it is a halted run (Principle I).
        assert store.get_turn_cycle(run_id, 1, authoritative_only=False) is None
    finally:
        store.close()


async def test_a_clean_observation_passes_the_guard_untouched(tmp_path: Path) -> None:
    """The guard's own false-positive floor: the ordinary path must not trip it.

    A detective control that fires on legitimate traffic gets switched off, so this asserts the
    complement of the test above against the same registry and the same loop -- the plain
    `_FakeGame` plays its turn and the guard is silent.
    """
    run_id = RunId("run-parity-clean")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    provider = FakeModelProvider()
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID, is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=_FakeGame(),
        provider=provider,
        no_progress_step_limit=5,
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
        assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
        assert len(provider.calls) == 1
    finally:
        store.close()


async def test_an_out_of_parity_camera_request_is_recorded_as_exactly_that(
    tmp_path: Path,
) -> None:
    """T230 / FR-026, FR-027, research R8: a refused camera move carries its own reason.

    A camera request is an information-channel request wearing a movement request's clothes:
    pointing the camera somewhere the run has not revealed and capturing it would hand the agent
    fog-of-war contents through the image channel. FR-026 gives that its own rejection reason,
    `OUT_OF_PARITY_CAMERA`, and `act/camera.py` (T132) is its only producer -- with no caller in
    `src/`, the reason was unreachable in production and every refused camera action was recorded
    as an ordinary `UNAVAILABLE_TO_HUMAN_NOW`, indistinguishable in the audit from "the button
    was greyed out".

    The refusal itself is not new (`dispatch_action` evaluates the same catalog predicate); what
    this asserts is that the *record* now says which kind of refusal it was, which is the whole
    point of SC-006/SC-020-style after-the-fact auditability.
    """
    run_id = RunId("run-camera-parity")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    provider = FakeModelProvider()
    provider.queue_decision(
        RawDecision(
            action_declaration_id=CAMERA_SET_VIEW_MODE,
            reasoning="look at the whole map",
            # Not one of the two modes a human can toggle directly (the catalog's own bound).
            parameters={"target": "omniscient"},
            is_end_turn=False,
            prompt_type=None,
        )
    )
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID, is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=_FakeGame(),
        provider=provider,
        no_progress_step_limit=5,
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
        assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT

        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        camera_step = record.steps[0]
        assert camera_step.decision.action_declaration_id == CAMERA_SET_VIEW_MODE
        assert (
            camera_step.decision.execution.rejection_reason
            is RejectionReason.OUT_OF_PARITY_CAMERA
        ), (
            "a refused camera request was recorded under the generic availability reason; the "
            "audit cannot tell an attempted image-channel leak from a greyed-out button"
        )
        # Rejected, never executed -- and the turn still completed normally afterwards, so the
        # refusal is a recorded step rather than a halted run.
        assert camera_step.step.progress is StepProgress.REJECTED
        assert len(record.steps) == 2
    finally:
        store.close()
