"""T060 -- the two turn endings, end to end through the persisted record.

FR-008 / FR-014 / SC-022: a turn ends exactly one of two ways -- the agent's own end-turn decision
(``ended_by_agent``) or the no-progress backstop tripping (``ended_on_no_progress``) -- and the two
must be distinguishable in the record a caller reads back from the store, not merely in an
in-memory result object. This file drives the full ``run/turn_cycle.py`` orchestration (quicksave,
the T110/T112 loop, persist) against a real ``SqliteMatchStore`` and asserts against what actually
landed there.
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
from civsim_harness.provider.port import RawDecision
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.turn_cycle import (
    BackstopEndTurnNotConfirmed,
    TurnCycleDependencies,
    run_turn_cycle,
)
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

TICK_DECLARATION_ID = DeclarationId("test.tick")
STUCK_DECLARATION_ID = DeclarationId("test.stuck")
GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")
GAME_SCREEN_STATE_DECLARATION_ID = DeclarationId("game.screen_state")
END_TURN_DECLARATION_ID = DeclarationId("turn.end_turn")


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
    screen_state = ParityDeclaration(
        declaration_id=GAME_SCREEN_STATE_DECLARATION_ID,
        kind=DeclarationKind.OBSERVATION,
        summary="Test-only screen/prompt state, mirroring catalogs/observations/game.yaml.",
        parity_basis="Look at whichever screen or panel is currently open.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        output_schema={
            "type": "object",
            "required": ["screen", "recognized", "has_blocking_prompt"],
            "properties": {
                "screen": {"type": "string"},
                "recognized": {"type": "boolean"},
                "has_blocking_prompt": {"type": "boolean"},
            },
        },
        introduced_in_version="test",
    )
    tick = ParityDeclaration(
        declaration_id=TICK_DECLARATION_ID,
        kind=DeclarationKind.ACTION,
        summary="Test-only productive action: always available, always advances the counter.",
        parity_basis="Click a UI element that always advances the test counter.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        availability_predicate="true",
        verification_predicate="game.turn_number == observed_turn_number + 1",
        introduced_in_version="test",
    )
    stuck = ParityDeclaration(
        declaration_id=STUCK_DECLARATION_ID,
        kind=DeclarationKind.ACTION,
        summary="Test-only no-op action: always available, never actually changes anything.",
        parity_basis="Click a UI element that always exists but never does anything.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        availability_predicate="true",
        verification_predicate="game.turn_number == observed_turn_number + 1",
        introduced_in_version="test",
    )
    # Mirrors catalogs/actions/turn.yaml's turn.end_turn verbatim (T114 dispatches this exact
    # declaration on the harness's own behalf when the no-progress backstop trips).
    end_turn = ParityDeclaration(
        declaration_id=END_TURN_DECLARATION_ID,
        kind=DeclarationKind.ACTION,
        summary="End the current turn and pass play to the other civilizations.",
        parity_basis="Click the end-turn button in the lower-right action panel.",
        context=LuaContext.IN_GAME,
        capability_id=CapabilityId("test.turn_control"),
        availability_predicate="game.is_local_player_turn and not game.has_blocking_prompt",
        verification_predicate=(
            "game.turn_number == observed_turn_number + 1 or game.is_waiting_for_other_players"
        ),
        introduced_in_version="test",
    )
    capability = IntegrationCapability(
        capability_id=CapabilityId("test.turn_control"),
        path=CatalogCapabilityPath.FIRETUNER,
        implementation_ref="test",
        reads=["turn state"],
        writes=["turn state"],
    )
    declarations = (turn_state, screen_state, tick, stuck, end_turn)
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
    """``test.tick`` always advances the counter (verifies ``changed_state``); ``test.stuck`` is
    dispatched but its execution is a deliberate no-op, so its verification always reports
    ``rejected`` -- a stuck agent spinning on it never makes progress.

    ``blocking_prompt`` mirrors ``game.screen_state.has_blocking_prompt`` (catalogs/observations/
    game.yaml): a test sets it to simulate the T114 backstop tripping while a prompt is still up,
    which ``turn.end_turn``'s own ``availability_predicate`` must refuse (FR-010).
    ``end_turn_swallowed`` simulates ``UI.RequestAction`` being dispatched but the click never
    landing (turn_number does not advance) -- the exact ambiguity the turn-number readback exists
    to catch, per turn.end_turn's own verification_predicate.
    """

    def __init__(self, *, blocking_prompt: bool = False, end_turn_swallowed: bool = False) -> None:
        self.turn_number = 1
        self.blocking_prompt = blocking_prompt
        self.end_turn_swallowed = end_turn_swallowed

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
                ),
                CapabilityResult(
                    declaration_id=GAME_SCREEN_STATE_DECLARATION_ID,
                    value={
                        "screen": "world",
                        "recognized": True,
                        "has_blocking_prompt": self.blocking_prompt,
                    },
                ),
            ],
            "world",
        )

    async def execute(
        self, declaration_id: DeclarationId, parameters: Mapping[str, Any], target: Any
    ) -> None:
        if declaration_id == TICK_DECLARATION_ID:
            self.turn_number += 1
        elif declaration_id == END_TURN_DECLARATION_ID and not self.end_turn_swallowed:
            self.turn_number += 1
        # test.stuck, and a swallowed end_turn: deliberately do nothing.


class _FakeSaveCapability:
    def __init__(self, host: HostPlatform, home: Path) -> None:
        self._host = host
        self._home = home

    async def save_game(self, save_name: str) -> None:
        saves_dir = self._host.resolve_game_directories(home=self._home).saves_dir
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / f"{save_name}.Civ6Save").write_bytes(b"fake-save-data")


class _FakeSaveLoader:
    async def load(self, save: SavePoint) -> None:  # pragma: no cover - not exercised in T060
        return None


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
        no_progress_step_limit=3,
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
    """``saves.verify.verify_save``'s stability wait is real by default; tests don't need to pay
    for it (nothing here writes a save that is still being flushed)."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _make_deps(
    *,
    tmp_path: Path,
    run_id: RunId,
    turn_number: int,
    store: SqliteMatchStore,
    game: _FakeGame,
    provider: FakeModelProvider,
    no_progress_step_limit: int,
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


async def test_a_one_step_turn_ended_immediately_by_the_agent_is_valid(tmp_path: Path) -> None:
    run_id = RunId("run-ended-by-agent")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    game = _FakeGame()
    provider = FakeModelProvider()
    provider.queue_decision(
        RawDecision(
            action_declaration_id=TICK_DECLARATION_ID,
            reasoning="nothing to do this turn, ending immediately",
            parameters={},
            is_end_turn=True,
            prompt_type=None,
        )
    )
    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=game,
        provider=provider,
        no_progress_step_limit=3,
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
    finally:
        store.close()

    assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.turn_cycle.outcome is TurnOutcome.ENDED_BY_AGENT
        assert record.turn_cycle.is_authoritative is True
        assert record.turn_cycle.step_count == 1
        assert len(record.steps) == 1
        assert record.steps[0].decision.is_end_turn is True
    finally:
        store.close()


async def test_ends_on_no_progress_and_a_productive_step_resets_the_counter(tmp_path: Path) -> None:
    run_id = RunId("run-no-progress")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    game = _FakeGame()
    provider = FakeModelProvider()

    def _decision(declaration_id: DeclarationId) -> RawDecision:
        return RawDecision(
            action_declaration_id=declaration_id,
            reasoning="scripted step",
            parameters={},
            is_end_turn=False,
            prompt_type=None,
        )

    # streak: stuck(1) stuck(2) tick(reset->0) stuck(1) stuck(2) stuck(3=trip)
    scripted = [
        STUCK_DECLARATION_ID,
        STUCK_DECLARATION_ID,
        TICK_DECLARATION_ID,
        STUCK_DECLARATION_ID,
        STUCK_DECLARATION_ID,
        STUCK_DECLARATION_ID,
    ]
    for declaration_id in scripted:
        provider.queue_decision(_decision(declaration_id))

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=game,
        provider=provider,
        no_progress_step_limit=3,
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
    finally:
        store.close()

    assert outcome.outcome is TurnOutcome.ENDED_ON_NO_PROGRESS

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.turn_cycle.outcome is TurnOutcome.ENDED_ON_NO_PROGRESS
        assert record.turn_cycle.final_no_progress_streak == 3
        assert record.turn_cycle.step_count == len(scripted)

        # No synthetic end_turn decision was ever written to represent the backstop (T113,
        # SC-012, SC-022): every recorded step's decision is exactly the scripted action, and
        # none of them carries is_end_turn.
        assert all(not bundle.decision.is_end_turn for bundle in record.steps)
        assert [b.decision.action_declaration_id for b in record.steps] == scripted

        # The productive step (index 3) reset the streak to zero; the trailing three rejections
        # climbed back up to the limit from there, not from wherever the first two left off.
        streaks = [b.step.no_progress_streak_after for b in record.steps]
        assert streaks == [1, 2, 0, 1, 2, 3]
        progresses = [b.step.progress.value for b in record.steps]
        assert progresses == [
            "rejected",
            "rejected",
            "changed_state",
            "rejected",
            "rejected",
            "rejected",
        ]

        events = store.list_run_events(run_id, event_types=[RunEventType.TURN_ENDED_ON_NO_PROGRESS])
        assert len(events) == 1
        assert events[0].detail["final_no_progress_streak"] == 3
    finally:
        store.close()


async def test_the_two_endings_are_distinguishable(tmp_path: Path) -> None:
    """Same configuration, two different agent behaviours, two differently-recorded outcomes."""
    run_id = RunId("run-distinguish")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    # Turn 1: the agent ends by its own decision after two productive steps.
    game = _FakeGame()
    provider = FakeModelProvider()
    provider.queue_decision(
        RawDecision(
            action_declaration_id=TICK_DECLARATION_ID,
            reasoning="advance",
            parameters={},
            is_end_turn=False,
            prompt_type=None,
        )
    )
    provider.queue_decision(
        RawDecision(
            action_declaration_id=TICK_DECLARATION_ID,
            reasoning="that's enough this turn",
            parameters={},
            is_end_turn=True,
            prompt_type=None,
        )
    )
    deps1 = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=game,
        provider=provider,
        no_progress_step_limit=5,
    )
    outcome1 = await run_turn_cycle(deps1, run=run)
    assert outcome1.outcome is TurnOutcome.ENDED_BY_AGENT

    # Turn 2: the agent never ends its turn; the backstop does it instead.
    game2 = _FakeGame()
    provider2 = FakeModelProvider()
    for _ in range(5):
        provider2.queue_decision(
            RawDecision(
                action_declaration_id=STUCK_DECLARATION_ID,
                reasoning="try the same thing again",
                parameters={},
                is_end_turn=False,
                prompt_type=None,
            )
        )
    deps2 = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=2,
        store=store,
        game=game2,
        provider=provider2,
        no_progress_step_limit=5,
    )
    outcome2 = await run_turn_cycle(deps2, run=outcome1.run)
    assert outcome2.outcome is TurnOutcome.ENDED_ON_NO_PROGRESS
    store.close()

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        record1 = store.get_turn_cycle(run_id, 1)
        record2 = store.get_turn_cycle(run_id, 2)
        assert record1 is not None
        assert record2 is not None
        assert record1.turn_cycle.outcome is TurnOutcome.ENDED_BY_AGENT
        assert record2.turn_cycle.outcome is TurnOutcome.ENDED_ON_NO_PROGRESS
        assert record1.turn_cycle.outcome != record2.turn_cycle.outcome
    finally:
        store.close()


def _scripted_stuck_decisions(count: int) -> list[RawDecision]:
    return [
        RawDecision(
            action_declaration_id=STUCK_DECLARATION_ID,
            reasoning="try the same thing again",
            parameters={},
            is_end_turn=False,
            prompt_type=None,
        )
        for _ in range(count)
    ]


async def test_the_backstop_dispatches_turn_end_turn_and_verifies_by_turn_number_readback(
    tmp_path: Path,
) -> None:
    """T113/T114: the harness -- never a synthesised agent decision -- issues turn.end_turn to the
    game once the no-progress backstop trips, and that dispatch is proven by the same turn-number
    readback every other action uses (act.dispatch/act.verify), not merely a non-error return."""
    run_id = RunId("run-backstop-dispatches")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    game = _FakeGame()
    provider = FakeModelProvider()
    for decision in _scripted_stuck_decisions(2):
        provider.queue_decision(decision)

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=game,
        provider=provider,
        no_progress_step_limit=2,
    )

    try:
        outcome = await run_turn_cycle(deps, run=run)
    finally:
        store.close()

    assert outcome.outcome is TurnOutcome.ENDED_ON_NO_PROGRESS
    # The fake game's own turn counter only ever advances through a genuine execute() call for
    # test.tick or turn.end_turn (see _FakeGame.execute) -- neither scripted step here was
    # test.tick, so this advance can only be the harness's own backstop end-turn dispatch, and it
    # is derived from the game's own state, not asserted by this test.
    assert game.turn_number == 2

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.turn_cycle.outcome is TurnOutcome.ENDED_ON_NO_PROGRESS
        assert record.turn_cycle.is_authoritative is True

        # No Decision, DecisionStep, or ModelCall was created for the backstop's own end-turn
        # dispatch (T113, SC-012, SC-022): step_count and the persisted steps match the scripted
        # stuck decisions exactly, one-for-one -- not one more.
        assert record.turn_cycle.step_count == 2
        assert len(record.steps) == 2
        assert [b.decision.action_declaration_id for b in record.steps] == [
            STUCK_DECLARATION_ID,
            STUCK_DECLARATION_ID,
        ]
        assert all(not b.decision.is_end_turn for b in record.steps)
    finally:
        store.close()


async def test_the_backstop_end_turn_surfaces_a_recorded_stall_when_blocked_by_a_prompt(
    tmp_path: Path,
) -> None:
    """T114: turn.end_turn's own availability_predicate refuses an end-turn while a blocking
    prompt is up (FR-010). If the backstop trips in that state, it is a genuine stuck state --
    this must surface as a recorded, visible failure, never a silent retry loop and never a
    fabricated success (the game's own turn counter must never move)."""
    run_id = RunId("run-backstop-blocked")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    game = _FakeGame(blocking_prompt=True)
    provider = FakeModelProvider()
    for decision in _scripted_stuck_decisions(2):
        provider.queue_decision(decision)

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=game,
        provider=provider,
        no_progress_step_limit=2,
    )

    try:
        with pytest.raises(BackstopEndTurnNotConfirmed) as exc_info:
            await run_turn_cycle(deps, run=run)
    finally:
        store.close()

    assert exc_info.value.detail["stage"] == "dispatch"
    # No fabricated success: the game's own turn counter never moved, because turn.end_turn was
    # never even authorized to dispatch.
    assert game.turn_number == 1

    # The turn's own record -- what the *agent* did -- is still durably persisted (write-before-
    # advance, I3): this is a recorded, visible stall, not a turn that silently vanishes.
    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.turn_cycle.outcome is TurnOutcome.ENDED_ON_NO_PROGRESS
        assert record.turn_cycle.is_authoritative is True
        assert record.turn_cycle.step_count == 2
    finally:
        store.close()


async def test_the_backstop_end_turn_is_not_treated_as_success_when_the_click_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T114: ``UI.RequestAction``'s own return value is ``nil`` and carries no information -- a
    non-error dispatch must never be treated as success. When the post-dispatch turn-number
    readback does not confirm the turn actually ended (the click was swallowed), that must also
    surface as a recorded failure rather than a fabricated success.

    The backstop now re-reads the game for a bounded time before concluding this (the live
    client confirms an end turn asynchronously, after the AI turns -- measured 2026-09-21); the
    bound is zeroed here so the swallowed click is concluded at once, not after 45 s."""
    import civsim_harness.run.turn_cycle as turn_cycle_module

    monkeypatch.setattr(turn_cycle_module, "BACKSTOP_CONFIRM_TIMEOUT_S", 0.0)
    run_id = RunId("run-backstop-swallowed")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    game = _FakeGame(end_turn_swallowed=True)
    provider = FakeModelProvider()
    for decision in _scripted_stuck_decisions(2):
        provider.queue_decision(decision)

    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        turn_number=1,
        store=store,
        game=game,
        provider=provider,
        no_progress_step_limit=2,
    )

    try:
        with pytest.raises(BackstopEndTurnNotConfirmed) as exc_info:
            await run_turn_cycle(deps, run=run)
    finally:
        store.close()

    assert exc_info.value.detail["stage"] == "verify"
    assert game.turn_number == 1

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.turn_cycle.outcome is TurnOutcome.ENDED_ON_NO_PROGRESS
        assert record.turn_cycle.step_count == 2
    finally:
        store.close()
