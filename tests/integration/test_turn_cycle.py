"""T058 -- the full turn cycle, end to end, across 50 authoritative turns.

FR-007 / FR-013 / SC-001 / SC-003: the orchestrated sequence -- quicksave, the T110/T112 multi-step
decision loop, persist, end turn -- run 50 times in a row (via ``run/turn_cycle.py``'s single entry
point, ``run_turn_cycle``) must land exactly 50/50 authoritative turns, with **zero turn gaps and
zero step gaps**, and the configured ``turn_reached(50)`` stop condition must actually resolve once
that point is reached (``run/stop.py``). This file drives the real orchestration against a real
``SqliteMatchStore`` and asserts against what actually landed there, not against any in-memory
return value alone.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
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
from civsim_harness.models.records import SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
)
from civsim_harness.models.turn import TurnOutcome
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.port import DecisionRequest, RawDecision
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.stop import GameOutcome, StopEvaluation, evaluate_stop
from civsim_harness.run.turn_cycle import TurnCycleDependencies, run_turn_cycle
from civsim_harness.store.completeness import record_completeness_status
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

TICK_DECLARATION_ID = DeclarationId("test.tick")
GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")

TOTAL_TURNS = 50


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
    capability = IntegrationCapability(
        capability_id=CapabilityId("test.turn_control"),
        path=CatalogCapabilityPath.FIRETUNER,
        implementation_ref="test",
        reads=["turn state"],
        writes=["turn state"],
    )
    declarations = (turn_state, tick)
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
    """A persistent, ever-incrementing counter shared across all 50 turn attempts -- unrelated to
    the harness's own ``turn_number`` bookkeeping (``TurnCycleDependencies.turn_number``), exactly
    as ``tests/integration/test_turn_endings.py``'s multi-turn scenario already establishes."""

    def __init__(self) -> None:
        self.counter = 1

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        return (
            [
                CapabilityResult(
                    declaration_id=GAME_TURN_STATE_DECLARATION_ID,
                    value={
                        "turn_number": self.counter,
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
        assert declaration_id == TICK_DECLARATION_ID
        self.counter += 1


class _FakeSaveCapability:
    def __init__(self, host: HostPlatform, home: Path) -> None:
        self._host = host
        self._home = home

    async def save_game(self, save_name: str) -> None:
        saves_dir = self._host.resolve_game_directories(home=self._home).saves_dir
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / f"{save_name}.Civ6Save").write_bytes(b"fake-save-data")


class _FakeSaveLoader:
    async def load(self, save: SavePoint) -> None:  # pragma: no cover - never exercised here
        return None


def _build_run_and_config(run_id: RunId) -> tuple[Run, RunConfiguration]:
    now = datetime.now(UTC)
    config = RunConfiguration(
        config_id=ConfigId("cfg-turn-cycle"),
        map_seed="seed",
        civilization="civ",
        leader="leader",
        ruleset="ruleset",
        difficulty="prince",
        stop_condition=TurnReachedStopCondition(turn=TOTAL_TURNS),
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
    """``saves.verify.verify_save``'s stability wait is real by default; 50 turns' worth of
    quicksaves don't need to pay for it."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _make_deps(
    *,
    tmp_path: Path,
    run_id: RunId,
    turn_number: int,
    store: SqliteMatchStore,
    game: _FakeGame,
    provider: FakeModelProvider,
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
            no_progress_step_limit=5,
            read_observation_inputs=game.read,
            execute_action=game.execute,
            host=host,
            host_info=host_info,
            view_declaration_id=DeclarationId("views.test"),
            screening_profiles=load_screening_profiles(),
            store=store,
        )

    return TurnCycleDependencies(
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


def _decision(*, is_end_turn: bool) -> RawDecision:
    return RawDecision(
        action_declaration_id=TICK_DECLARATION_ID,
        reasoning="advance, then end the turn" if is_end_turn else "advance",
        parameters={},
        is_end_turn=is_end_turn,
        prompt_type=None,
    )


def _two_step_factory(request: DecisionRequest) -> RawDecision:
    """Every turn's decision loop resets ``step_index`` to 1, so this alternates
    productive-then-end deterministically, turn after turn, without per-turn scripting -- proving
    the "multi-step loop" half of the orchestration runs on every one of the 50 turns, not just the
    first."""
    return _decision(is_end_turn=request.step_index >= 2)


async def test_fifty_turns_land_fully_authoritative_with_no_turn_or_step_gaps(
    tmp_path: Path,
) -> None:
    run_id = RunId("run-turn-cycle-50")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    game = _FakeGame()
    provider = FakeModelProvider()
    provider.set_default_decision_factory(_two_step_factory)

    current_run = run
    try:
        for turn_number in range(1, TOTAL_TURNS + 1):
            deps = _make_deps(
                tmp_path=tmp_path,
                run_id=run_id,
                turn_number=turn_number,
                store=store,
                game=game,
                provider=provider,
            )
            outcome = await run_turn_cycle(deps, run=current_run)
            assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
            current_run = outcome.run
    finally:
        store.close()

    # SC-001/SC-003: 50/50 authoritative turns, zero turn gaps, zero step gaps.
    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        assert store.turn_gaps(run_id) == []

        authoritative_count = 0
        for turn_number in range(1, TOTAL_TURNS + 1):
            assert store.step_gaps(run_id, turn_number) == []
            record = store.get_turn_cycle(run_id, turn_number)
            assert record is not None
            assert record.turn_cycle.is_authoritative is True
            assert record.turn_cycle.outcome is TurnOutcome.ENDED_BY_AGENT
            assert record.turn_cycle.step_count == 2
            assert len(record.steps) == 2
            authoritative_count += 1
        assert authoritative_count == TOTAL_TURNS

        # Nothing beyond turn 50 was ever attempted.
        assert store.get_turn_cycle(run_id, TOTAL_TURNS + 1) is None

        # SC-011/FR-052: derived completeness genuinely reflects the gap-free record.
        assert record_completeness_status(store, run_id) is RecordCompletenessStatus.COMPLETE

        # 2 steps/turn * 50 turns = 100 model calls, never fewer (a skipped step) or more
        # (a repeated one).
        assert len(provider.calls) == 2 * TOTAL_TURNS
    finally:
        store.close()

    # The configured turn_reached(50) stop condition resolves exactly once the 50th turn is
    # actually reached -- not before, and to the right recorded resolution (FR-005, R/stop.py).
    stop_condition = TurnReachedStopCondition(turn=TOTAL_TURNS)
    not_yet = evaluate_stop(stop_condition, StopEvaluation(current_turn=TOTAL_TURNS - 1))
    assert not_yet.resolution is None

    reached = evaluate_stop(stop_condition, StopEvaluation(current_turn=TOTAL_TURNS))
    from civsim_harness.models.run import StopResolution

    assert reached.resolution is StopResolution.TURN_REACHED
    assert reached.coincident == ()

    # A victory reported on the same turn is not what this run was configured to stop on -- the
    # separate GameOutcome check confirms it is orthogonal, not accidentally conflated (models/
    # run.py's own point about the 5-value recorded set vs. the 3-value configured one).
    assert evaluate_stop(
        stop_condition, StopEvaluation(current_turn=TOTAL_TURNS, game_outcome=GameOutcome.VICTORY)
    ).resolution is StopResolution.VICTORY
