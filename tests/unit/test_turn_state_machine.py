"""T062 -- the turn attempt's failure shapes (data-model.md SS5's failure table).

Three distinct failures, three distinct, deliberately non-uniform outcomes (invariants I2, I3):

- A failed quicksave halts the run before a ``TurnCycle`` attempt exists at all (FR-007).
- A failed persist halts the run *before* the end-turn action is ever reachable -- structurally
  guaranteed by ``store.guard``'s write-before-advance functions, not merely by convention
  (FR-013).
- A mid-turn observation-assembly failure at step *n* > 1 does not repair in place: the attempt is
  abandoned (retained, with every step it completed) and a fresh attempt replays from the turn's
  start quicksave, and exactly one attempt ends up authoritative (T152, invariant I9).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import NexusError, ObservationAssemblyError, StoreWriteError
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
from civsim_harness.models.records import ModelCall, RunEventType, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
)
from civsim_harness.models.turn import ScreenCapture, TurnOutcome
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.port import RawDecision
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.turn_cycle import TurnCycleDependencies, run_turn_cycle
from civsim_harness.store.port import MatchStore, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

TICK_DECLARATION_ID = DeclarationId("test.tick")
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
    def __init__(self) -> None:
        self.turn_number = 1

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
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
            "world",
        )

    async def execute(
        self, declaration_id: DeclarationId, parameters: Mapping[str, Any], target: Any
    ) -> None:
        assert declaration_id == TICK_DECLARATION_ID
        self.turn_number += 1


class _FlakyGame(_FakeGame):
    """Like ``_FakeGame``, but its 4th-ever read raises ``ObservationAssemblyError`` exactly
    once. Reads go: #1 step 1's own observation, #2 (after step 1's dispatch) becomes step 2's
    own observation, #3 (after step 2's dispatch) becomes step 3's own observation, #4 (after
    step 3's dispatch, while step 3 is still being processed) is where this fails -- so two
    decision steps (1 and 2) have already executed and verified by the time it does, reproducing
    "a failure at step n > 1, after earlier steps have already executed and verified" (T096)."""

    def __init__(self) -> None:
        super().__init__()
        self._read_count = 0
        self._fail_armed = True

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        self._read_count += 1
        if self._fail_armed and self._read_count == 4:
            self._fail_armed = False
            raise ObservationAssemblyError(
                "simulated mid-turn observation failure", detail={"read_count": self._read_count}
            )
        return await super().read()


class _RaisingSaveCapability:
    """FR-007: a quicksave that never lands -- the turn must not come into existence."""

    async def save_game(self, save_name: str) -> None:
        raise NexusError("simulated FireTuner save failure", detail={"save_name": save_name})


class _WorkingSaveCapability:
    def __init__(self, host: HostPlatform, home: Path) -> None:
        self._host = host
        self._home = home

    async def save_game(self, save_name: str) -> None:
        saves_dir = self._host.resolve_game_directories(home=self._home).saves_dir
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / f"{save_name}.Civ6Save").write_bytes(b"fake-save-data")


class _FakeSaveLoader:
    async def load(self, save: SavePoint) -> None:
        return None


class _PersistFailingStore:
    """Delegates everything to *inner* except ``write_turn_cycle``, which always raises --
    reproduces FR-013's "a failed write halts the run rather than advance it"."""

    def __init__(self, inner: MatchStore) -> None:
        self._inner = inner

    def write_turn_cycle(self, record: TurnCycleRecord) -> Any:
        raise StoreWriteError("simulated persistence failure", detail={})

    def write_capture(self, capture: ScreenCapture, blob: bytes | None) -> Any:
        return self._inner.write_capture(capture, blob)

    def write_model_call(self, call: ModelCall) -> Any:
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


def _plain_decision(declaration_id: DeclarationId, *, is_end_turn: bool = False) -> RawDecision:
    return RawDecision(
        action_declaration_id=declaration_id,
        reasoning="scripted step",
        parameters={},
        is_end_turn=is_end_turn,
        prompt_type=None,
    )


def _make_deps(
    *,
    tmp_path: Path,
    run_id: RunId,
    store: MatchStore,
    game: _FakeGame,
    provider: FakeModelProvider,
    save_capability: Any,
) -> TurnCycleDependencies:
    registry = _build_registry()
    host = FakeHostPlatform()
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
            view_declaration_id=DeclarationId("views.test"),
            screening_profiles=load_screening_profiles(),
            store=store,
        )

    return TurnCycleDependencies(
        run_id=run_id,
        turn_number=1,
        store=store,
        save_capability=save_capability,
        host=host,
        min_free_disk_gb=1.0,
        disk_check_path=tmp_path,
        build_loop_context=build_loop_context,
        recovery=RecoveryEngine(
            run_id=run_id, store=store, loader=_FakeSaveLoader(), recovery_attempt_limit=3
        ),
        home=tmp_path,
    )


# --------------------------------------------------------------------------
# 1. A failed quicksave: the turn never comes into existence as an attempt.
# --------------------------------------------------------------------------


async def test_a_failed_quicksave_halts_the_run_and_no_attempt_ever_exists(
    tmp_path: Path,
) -> None:
    run_id = RunId("run-quicksave-fails")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    game = _FakeGame()
    provider = FakeModelProvider()
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID, is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        store=store,
        game=game,
        provider=provider,
        save_capability=_RaisingSaveCapability(),
    )

    with pytest.raises(NexusError):
        await run_turn_cycle(deps, run=run)

    assert store.get_turn_cycle(run_id, 1) is None
    assert store.list_save_points(run_id) == []
    save_failed = store.list_run_events(run_id, event_types=[RunEventType.SAVE_FAILED])
    assert len(save_failed) == 1
    store.close()


# --------------------------------------------------------------------------
# 2. A failed persist: the run halts before end-turn is ever reachable.
# --------------------------------------------------------------------------


async def test_a_failed_persist_halts_the_run_before_end_turn(tmp_path: Path) -> None:
    run_id = RunId("run-persist-fails")
    run, config = _build_run_and_config(run_id)
    real_store = SqliteMatchStore(tmp_path / "match.db")
    real_store.create_run(run, config)
    failing_store = _PersistFailingStore(real_store)

    game = _FakeGame()
    provider = FakeModelProvider()
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID, is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        store=failing_store,
        game=game,
        provider=provider,
        save_capability=_WorkingSaveCapability(FakeHostPlatform(), tmp_path),
    )

    with pytest.raises(StoreWriteError):
        await run_turn_cycle(deps, run=run)

    # The quicksave itself succeeded (it happens before the loop, let alone persistence) --
    # only the persist-and-end-turn tail failed.
    save_points = real_store.list_save_points(run_id)
    assert len(save_points) == 1
    assert save_points[0].verified is True

    # But no turn record exists, and never will for this attempt: the write itself raised, so
    # store.guard's write-before-advance functions never mint a token, and the end-turn callable
    # is structurally unreachable (invariant I3).
    assert real_store.get_turn_cycle(run_id, 1) is None
    real_store.close()


# --------------------------------------------------------------------------
# 3. A mid-turn observation-assembly failure: abandon, retain, and replay (T152).
# --------------------------------------------------------------------------


async def test_mid_turn_observation_failure_abandons_and_replays_from_the_start_quicksave(
    tmp_path: Path,
) -> None:
    run_id = RunId("run-mid-turn-failure")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    game = _FlakyGame()
    provider = FakeModelProvider()
    # Attempt 0: steps 1 and 2 complete; step 3's own decision is requested and dispatched, but
    # the fresh observation needed to verify and record it fails to assemble (T096).
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID))
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID))
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID))
    # Attempt 1 (the replay, from a clean start): one step, agent ends immediately.
    provider.queue_decision(_plain_decision(TICK_DECLARATION_ID, is_end_turn=True))

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        store=store,
        game=game,
        provider=provider,
        save_capability=_WorkingSaveCapability(FakeHostPlatform(), tmp_path),
    )

    outcome = await run_turn_cycle(deps, run=run)

    assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
    # Attempt 0 requested 3 decisions (steps 1, 2, and the one that failed to record as step 3);
    # attempt 1's replay requested 1 more, from a clean start -- 4 total, never fewer (which
    # would mean the replay somehow skipped steps) and never more (which would mean it repeated
    # ones that had already run).
    assert len(provider.calls) == 4

    events = store.list_run_events(
        run_id,
        event_types=[
            RunEventType.OBSERVATION_ASSEMBLY_FAILED,
            RunEventType.TURN_ABANDONED,
            RunEventType.RESUMED,
        ],
    )
    event_types = [event.event_type for event in events]
    assert RunEventType.OBSERVATION_ASSEMBLY_FAILED in event_types
    assert RunEventType.TURN_ABANDONED in event_types
    assert RunEventType.RESUMED in event_types
    store.close()

    # Raw-table inspection: exactly two attempts were written for (run, turn), and exactly one
    # of them is authoritative (invariant I9) -- the abandoned one retains every step it
    # completed (T152), never silently dropped.
    conn = sqlite3.connect(str(tmp_path / "match.db"))
    try:
        rows = conn.execute(
            "SELECT attempt_index, is_authoritative, step_count, turn_json "
            "FROM turn_cycles WHERE run_id = ? AND turn_number = ? ORDER BY attempt_index",
            (run_id, 1),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    (attempt0_index, attempt0_authoritative, attempt0_step_count, attempt0_json), (
        attempt1_index,
        attempt1_authoritative,
        attempt1_step_count,
        attempt1_json,
    ) = rows

    assert attempt0_index == 0
    assert attempt0_authoritative == 0
    assert attempt0_step_count == 2
    assert '"outcome":"abandoned"' in attempt0_json

    assert attempt1_index == 1
    assert attempt1_authoritative == 1
    assert attempt1_step_count == 1
    assert '"outcome":"ended_by_agent"' in attempt1_json

    # Exactly one authoritative attempt overall (invariant I9), and it is the one get_turn_cycle
    # returns by default.
    authoritative_count = sum(row[1] for row in rows)
    assert authoritative_count == 1

    store2 = SqliteMatchStore(tmp_path / "match.db")
    try:
        authoritative_record = store2.get_turn_cycle(run_id, 1)
        assert authoritative_record is not None
        assert authoritative_record.turn_cycle.attempt_index == 1
        assert authoritative_record.turn_cycle.is_authoritative is True
        assert authoritative_record.turn_cycle.outcome is TurnOutcome.ENDED_BY_AGENT
    finally:
        store2.close()
