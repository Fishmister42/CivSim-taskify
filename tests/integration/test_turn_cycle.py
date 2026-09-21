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

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import DiskHeadroomError
from civsim_harness.host.detect import HostInfo, LinuxSessionType, OperatingSystem
from civsim_harness.host.port import DiskSpace, HostPlatform
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
from civsim_harness.models.records import RunEventType, SavePoint
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
from civsim_harness.resilience.detector import DetectionAggregator
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.detection import DetectionWatch
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
    host: FakeHostPlatform | None = None,
    detection: DetectionWatch | None = None,
    detection_interval_s: float = 10.0,
    save_loader: Any = None,
) -> TurnCycleDependencies:
    registry = _build_registry()
    host = host if host is not None else FakeHostPlatform()
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
            run_id=run_id,
            store=store,
            loader=save_loader if save_loader is not None else _FakeSaveLoader(),
            recovery_attempt_limit=3,
        ),
        home=tmp_path,
        detection=detection,
        detection_interval_s=detection_interval_s,
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


async def test_a_headroom_halt_puts_its_own_warning_on_the_run_timeline(tmp_path: Path) -> None:
    """T229 / Principle III: a run that halts on disk headroom records *why*, in the run's record.

    data-model.md SS14 calls `disk_headroom_low` "the warning before the halt ... without it the
    halt looks arbitrary". `saves/headroom.py`'s `check_headroom` is deliberately a pure check
    (R17: it never deletes and never records), and its `build_disk_headroom_event` companion had
    **no caller in `src/`** -- so `RunEventType.DISK_HEADROOM_LOW` was emitted nowhere in
    production and every headroom halt left the timeline silent about its cause.

    Driven through `run_turn_cycle` rather than by calling the two functions in sequence here:
    the existing unit coverage does exactly that sequence in its own test body, which asserts the
    store works and says nothing about whether the harness ever performs it.
    """
    run_id = RunId("run-headroom-halt")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    # Below `_make_deps`' own `min_free_disk_gb=1.0` floor.
    host = FakeHostPlatform()
    host.set_disk_space(
        DiskSpace(path=tmp_path, free_bytes=512 * 1024 * 1024, total_bytes=100 * 1024**3)
    )

    provider = FakeModelProvider()
    provider.set_default_decision_factory(_two_step_factory)
    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=_FakeGame(),
        provider=provider,
        host=host,
    )

    try:
        with pytest.raises(DiskHeadroomError):
            await run_turn_cycle(deps, run=run)

        events = store.list_run_events(run_id, event_types=[RunEventType.DISK_HEADROOM_LOW])
        assert len(events) == 1, "the halt left no recorded reason on the run's timeline"
        assert events[0].turn_number == 1
        # The detail names the floor that was breached, not merely that something went wrong.
        assert events[0].detail["min_free_disk_gb"] == 1.0
        assert events[0].detail["free_bytes"] == 512 * 1024 * 1024

        # R17: the halt deletes nothing and the turn never came into existence as an attempt
        # (data-model.md SS5's failure table) -- no quicksave was taken, so none was recorded.
        assert store.get_turn_cycle(run_id, 1, authoritative_only=False) is None
        assert store.list_save_points(run_id) == []
    finally:
        store.close()


# --------------------------------------------------------------------------
# T233 -- the in-turn crash watchdog
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _ScriptedLiveness:
    """A `ProcessLivenessMonitor` whose answer this test controls, turn by turn.

    Duck-typed rather than subclassed: `resilience/detector.py` asks a liveness monitor exactly
    two things -- `.pid` and `.check()` -- and a real `psutil` monitor cannot be made to report a
    process dying at a chosen instant. What is under test here is the *wiring* (does the watchdog
    run during a turn, and does a trip route into `RecoveryEngine`), not `psutil` itself, which
    `resilience/liveness.py` already has its own coverage for.
    """

    pid: int
    alive: list[bool]

    def check(self) -> bool:
        return self.alive[0]


class _RevivingSaveLoader:
    """A save loader that also brings the client back -- what a real recovery would achieve.

    Recovery's whole job is to get the run playing again from this turn's start quicksave; a
    loader that left the client dead would only ever exercise the "still faulty after recovery"
    refusal. This exercises the path that matters: detect, abandon, recover, replay, finish.
    """

    def __init__(self, alive: list[bool]) -> None:
        self._alive = alive
        self.loads: list[str] = []

    async def load(self, save: SavePoint) -> None:
        self.loads.append(str(save.save_point_id))
        self._alive[0] = True


async def test_a_client_that_dies_mid_turn_is_detected_and_the_turn_is_replayed(
    tmp_path: Path,
) -> None:
    """T233 / FR-044, FR-045, SC-010: the in-turn watchdog runs, trips, and routes to recovery.

    `DetectionAggregator`, `HeartbeatMonitor` and `ProcessLivenessMonitor` were complete and
    **constructed nowhere in `src/`**, so none of research R12's signals ran during a real turn:
    a client that died mid-turn was noticed only when the next Nexus call happened to fail, if it
    did. This drives the real `run_turn_cycle` with the detection layer attached and a client that
    stops being a live process partway through the first attempt.

    The claim is the whole chain, not any one link: the detection is **recorded**
    (`crash_detected`), the attempt is **abandoned** with that detection named as its trigger
    (`turn_abandoned`), recovery resumes from *this turn's own* start quicksave (FR-045), and the
    replay lands as a fresh attempt. Reverting `_run_decision_loop_watched` in
    `run/turn_cycle.py` to a bare `run_decision_loop` call makes this test fail.
    """
    run_id = RunId("run-detect-midturn")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    alive = [True]
    liveness = _ScriptedLiveness(pid=4242, alive=alive)
    loader = _RevivingSaveLoader(alive)

    game = _FakeGame()
    real_read = game.read

    async def read_and_die() -> tuple[Sequence[CapabilityResult], str]:
        """The client dies during the first attempt's very first observation read.

        The sleep is what gives the watchdog a pass to run in: the cadence is deliberately real
        (`asyncio.wait` with a timeout), so a decision loop that never yields would finish before
        any pass happened -- which is exactly why the interval below is small rather than the
        production default.
        """
        if alive[0] and loader.loads == []:
            alive[0] = False
        await asyncio.sleep(0.05)
        return await real_read()

    game.read = read_and_die  # type: ignore[method-assign]

    provider = FakeModelProvider()
    provider.set_default_decision_factory(_two_step_factory)
    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=game,
        provider=provider,
        detection=DetectionWatch(
            aggregator=DetectionAggregator(liveness=liveness),
            store=store,
            run_id=run_id,
            clock=lambda: datetime.now(UTC),
            liveness=liveness,
        ),
        detection_interval_s=0.01,
        save_loader=loader,
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)

        crashes = store.list_run_events(run_id, event_types=[RunEventType.CRASH_DETECTED])
        assert crashes, (
            "the client stopped being a live process mid-turn and nothing detected it; SC-010 "
            "has no mechanism behind it"
        )
        assert crashes[0].detail["pid"] == 4242

        abandoned = store.list_run_events(run_id, event_types=[RunEventType.TURN_ABANDONED])
        assert len(abandoned) == 1
        assert abandoned[0].detail["trigger"] == RunEventType.CRASH_DETECTED.value
        assert abandoned[0].turn_number == 1

        # FR-045: resumed from *this turn's* start quicksave, not "the most recent good save".
        save_points = store.list_save_points(run_id)
        assert [point.turn_number for point in save_points] == [1]
        assert loader.loads == [str(save_points[0].save_point_id)]

        # The replay is a fresh attempt and it is the authoritative one (FR-047).
        assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.turn_cycle.attempt_index == 1
        assert record.turn_cycle.is_authoritative
        assert record.steps
    finally:
        store.close()


async def test_a_persisted_turn_rederives_the_runs_stored_completeness(tmp_path: Path) -> None:
    """T239: run_turn_cycle's write_then_advance is the "turn persisted" moment, and the stored
    Run's record_completeness_status must be re-derived there -- not only at the terminal
    transition. Created UNKNOWN (nothing to judge), still `playing` afterwards (no runner, no
    _finish in this test), yet the store's Run already carries the derivation: COMPLETE for the
    gap-free turn just persisted. Fails with the refresh in run/turn_cycle.py reverted, since
    nothing else in this test ever touches the field."""
    run_id = RunId("run-t239-persist")
    run, config = _build_run_and_config(run_id)
    run = run.model_copy(
        update={"record_completeness_status": RecordCompletenessStatus.UNKNOWN}
    )
    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        store.create_run(run, config)

        game = _FakeGame()
        provider = FakeModelProvider()
        provider.set_default_decision_factory(_two_step_factory)
        deps = _make_deps(
            tmp_path=tmp_path,
            run_id=run_id,
            turn_number=1,
            store=store,
            game=game,
            provider=provider,
        )
        await run_turn_cycle(deps, run=run)

        persisted = store.get_run(run_id)
        assert persisted is not None
        assert persisted.lifecycle_state is LifecycleState.PLAYING  # mid-run, not terminal
        assert persisted.record_completeness_status is RecordCompletenessStatus.COMPLETE, (
            "the turn persisted and the stored Run still carries its creation-time "
            "completeness -- the turn-persisted derive-and-persist moment is unwired (T239)"
        )
    finally:
        store.close()


@pytest.fixture(autouse=True)
def _no_end_turn_confirmation_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live client confirms an end turn only after the AI players' turns, so the production
    loop re-reads the game for a bounded time before recording the agent's (or the backstop's)
    end turn as unconfirmed (measured 2026-09-21). A fake game answers at once, so the bound is
    zeroed here: these tests keep their original single-read semantics and their speed."""
    import civsim_harness.run.decision_loop as decision_loop_module
    import civsim_harness.run.turn_cycle as turn_cycle_module

    monkeypatch.setattr(decision_loop_module, "END_TURN_CONFIRM_TIMEOUT_S", 0.0)
    monkeypatch.setattr(turn_cycle_module, "BACKSTOP_CONFIRM_TIMEOUT_S", 0.0)
