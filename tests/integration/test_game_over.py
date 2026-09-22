"""A game that ended before a turn could begin finishes the run, it does not fail its save.

MEASURED 2026-09-21, 18:50 EDT, live Linux client: Persia was defeated at game turn 59 (Georgia
took the only city). The harness had no game-over detection, so the next turn began as any other:
the FR-007 start-of-turn quicksave was requested, the already-finished game never wrote one, and
the run paused with ``SaveVerificationError("expected .Civ6Save not found ... waited 10 s")``. A
fresh ``InGame`` read showed ``Players[0]:IsAlive() == false`` and ``EndGameMenu`` as the only
shown screen.

Two halves. First, ``run/turn_cycle.py``'s pre-save read against the fakes: a defeat and a victory
each raise :class:`~civsim_harness.run.game_over.GameOverDetected` with the quicksave never
attempted and the ``game_over_detected`` event on the timeline, while every kind of unreadable
result plays the turn exactly as before. Then the same endings driven through the real
``run/runner.py`` against a real ``SqliteMatchStore``, proving the run actually reaches
``finished`` with one recorded ``stop_resolution`` -- and that the poll loop both live drivers
share (``status.lifecycle_state in TERMINAL`` -> break) exits on it rather than spinning.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import NexusError
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
from civsim_harness.models.config import (
    ModelConfig,
    RunConfiguration,
    StopCondition,
    TurnReachedStopCondition,
)
from civsim_harness.models.records import RunEventType
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
    StopResolution,
)
from civsim_harness.models.turn import TurnOutcome
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.port import RawDecision
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.game_over import (
    NO_GAME_OVER,
    GameOverDetected,
    interpret_game_over,
)
from civsim_harness.run.runner import PreparedRun, Runner, RunnerDependencies
from civsim_harness.run.stop import GameOutcome, StopEvaluation
from civsim_harness.run.turn_cycle import TurnCycleDependencies, run_turn_cycle
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "configs" / "turn50-validation.yaml"

TURN_STATE_ID = DeclarationId("game.turn_state")
END_TURN_ID = DeclarationId("turn.end_turn")

#: What `lua/ingame/game_over.lua` prints on the 2026-09-21 board: eliminated, nobody had won yet,
#: the end-game screen up. The winner panel is empty on this ending (endgamemenu.lua:832), so no
#: victory type and no winner are named.
DEFEAT_BY_ELIMINATION: Mapping[str, Any] = {
    "game_over": True,
    "outcome": "defeat",
    "local_player_alive": False,
    "basis": "local_player_not_alive",
    "end_game_screen_shown": True,
}

#: A rival's win: the end-game screen names both the victory and the civilization that took it.
DEFEAT_BY_RIVAL_VICTORY: Mapping[str, Any] = {
    "game_over": True,
    "outcome": "defeat",
    "local_player_alive": True,
    "basis": "winning_team_is_another_team",
    "victory_type": "Domination Victory",
    "winner": "Georgia",
    "end_game_screen_shown": True,
}

VICTORY: Mapping[str, Any] = {
    "game_over": True,
    "outcome": "victory",
    "local_player_alive": True,
    "basis": "winning_team_is_local_team",
    "victory_type": "Science Victory",
    "winner": "Persia",
    "end_game_screen_shown": True,
}

STILL_PLAYING: Mapping[str, Any] = {"game_over": False, "local_player_alive": True}


# --------------------------------------------------------------------------
# Shared fakes
# --------------------------------------------------------------------------


def _build_registry() -> CapabilityRegistry:
    capability_id = CapabilityId("test.turn_control")
    turn_state = ParityDeclaration(
        declaration_id=TURN_STATE_ID,
        kind=DeclarationKind.OBSERVATION,
        summary="Test-only turn state.",
        parity_basis="Look at the turn counter in the top bar.",
        context=LuaContext.IN_GAME,
        capability_id=capability_id,
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
    end_turn = ParityDeclaration(
        declaration_id=END_TURN_ID,
        kind=DeclarationKind.ACTION,
        summary="Test-only end turn.",
        parity_basis="Click the end-turn button.",
        context=LuaContext.IN_GAME,
        capability_id=capability_id,
        availability_predicate="true",
        verification_predicate="game.turn_number == observed_turn_number + 1",
        introduced_in_version="test",
    )
    capability = IntegrationCapability(
        capability_id=capability_id,
        path=CatalogCapabilityPath.FIRETUNER,
        implementation_ref="test.lua",
        reads=["turn state"],
        writes=["turn state"],
    )
    declarations = (turn_state, end_turn)
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
        self.counter = 1

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        return (
            [
                CapabilityResult(
                    declaration_id=TURN_STATE_ID,
                    value={
                        "turn_number": self.counter,
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
        self.counter += 1


class _CountingSaveCapability:
    """A quicksave that records every attempt. The whole point of the pre-save read is that a
    detected game over produces **zero** of them."""

    def __init__(self, host: HostPlatform, home: Path) -> None:
        self._host = host
        self._home = home
        self.attempts = 0

    async def save_game(self, save_name: str) -> None:
        self.attempts += 1
        saves_dir = self._host.resolve_game_directories(home=self._home).saves_dir
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / f"{save_name}.Civ6Save").write_bytes(b"fake-save-data")


class _FakeSaveLoader:
    async def load(self, save: Any) -> None:  # pragma: no cover - never exercised here
        return None


class _ScriptedGameOverRead:
    """The injected pre-save read: answers *results* in order, or raises *error* every time."""

    def __init__(self, *results: Any, error: Exception | None = None) -> None:
        self._results = list(results)
        self._error = error
        self.calls = 0

    async def __call__(self) -> Any:
        self.calls += 1
        if self._error is not None:
            raise self._error
        if not self._results:
            return STILL_PLAYING
        if len(self._results) == 1:
            return self._results[0]
        return self._results.pop(0)


CONFIG_ID = ConfigId("cfg-game-over")


def _run(run_id: RunId, config_id: ConfigId = CONFIG_ID) -> Run:
    return Run(
        run_id=run_id,
        config_id=config_id,
        lifecycle_state=LifecycleState.PLAYING,
        record_completeness_status=RecordCompletenessStatus.COMPLETE,
        comparability_status=ComparabilityStatus.COMPARABLE,
        observation_catalog_version=CatalogVersionRef(version="test", content_hash="test"),
        action_catalog_version=CatalogVersionRef(version="test", content_hash="test"),
        game_build="test/1.0",
        host_support_tier=HostSupportTier.VALIDATED,
        capture_path=CapturePath.NONE,
    )


def _config() -> RunConfiguration:
    return RunConfiguration(
        config_id=CONFIG_ID,
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
        created_at=datetime.now(UTC),
    )


def _end_turn_decision(request: Any) -> RawDecision:
    return RawDecision(
        action_declaration_id=END_TURN_ID,
        reasoning="end the turn",
        parameters={},
        is_end_turn=True,
        prompt_type=None,
    )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


# --------------------------------------------------------------------------
# Part 1: run/turn_cycle.py's pre-save read
# --------------------------------------------------------------------------


def _make_deps(
    *,
    tmp_path: Path,
    run_id: RunId,
    store: SqliteMatchStore,
    saves: _CountingSaveCapability,
    read_game_over: Any,
    turn_number: int = 1,
) -> TurnCycleDependencies:
    registry = _build_registry()
    host = FakeHostPlatform()
    host_info = HostInfo(
        os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
    )
    provider = FakeModelProvider()
    provider.set_default_decision_factory(_end_turn_decision)
    game = _FakeGame()

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
        save_capability=saves,
        host=host,
        min_free_disk_gb=1.0,
        disk_check_path=tmp_path,
        build_loop_context=build_loop_context,
        recovery=RecoveryEngine(
            run_id=run_id, store=store, loader=_FakeSaveLoader(), recovery_attempt_limit=3
        ),
        home=tmp_path,
        read_game_over=read_game_over,
    )


def _fresh_store(tmp_path: Path, run_id: RunId) -> SqliteMatchStore:
    store = SqliteMatchStore(tmp_path / "store.db")
    store.create_run(_run(run_id), _config())
    return store


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        (DEFEAT_BY_ELIMINATION, StopResolution.DEFEAT),
        (DEFEAT_BY_RIVAL_VICTORY, StopResolution.DEFEAT),
        (VICTORY, StopResolution.VICTORY),
    ],
)
async def test_a_game_over_at_turn_start_raises_before_any_save_is_attempted(
    tmp_path: Path, read: Mapping[str, Any], expected: StopResolution
) -> None:
    run_id = RunId(f"run-over-{expected.value}")
    store = _fresh_store(tmp_path, run_id)
    host = FakeHostPlatform()
    saves = _CountingSaveCapability(host, tmp_path)
    deps = _make_deps(
        tmp_path=tmp_path,
        run_id=run_id,
        store=store,
        saves=saves,
        read_game_over=_ScriptedGameOverRead(read),
    )

    with pytest.raises(GameOverDetected) as caught:
        await run_turn_cycle(deps, run=_run(run_id))

    assert caught.value.stop_resolution is expected
    # The whole point: a finished game refuses to save, so the save is never even asked for.
    assert saves.attempts == 0

    # "The turn never comes into existence as an attempt" -- no save point, no TurnCycle, and so
    # no gap either: store/completeness.py only owes a record for turns that took a quicksave.
    assert store.list_save_points(run_id) == []
    assert store.get_turn_cycle(run_id, 1, authoritative_only=False) is None
    assert store.list_run_events(run_id, event_types=[RunEventType.SAVE_TAKEN]) == []
    assert store.list_run_events(run_id, event_types=[RunEventType.SAVE_FAILED]) == []

    (event,) = store.list_run_events(run_id, event_types=[RunEventType.GAME_OVER_DETECTED])
    assert event.turn_number == 1
    assert event.detail["stop_resolution"] == expected.value
    assert event.detail["outcome"] == read["outcome"]
    assert event.detail["local_player_alive"] == read["local_player_alive"]
    assert event.detail["victory_type"] == read.get("victory_type")
    assert event.detail["winner"] == read.get("winner")
    # The raw Lua result rides along, so a later reader can tell an elimination defeat from a
    # rival's victory without re-running anything (Principle III).
    assert event.detail["raw"] == dict(read)


@pytest.mark.parametrize(
    "read",
    [
        STILL_PLAYING,
        # `game_over` true with an outcome nothing here can name is not actionable: a run cannot
        # be `finished` without exactly one stop_resolution, so the turn is played as usual.
        {"game_over": True, "outcome": "adjourned"},
        # Structurally wrong shapes -- a bare string, a list, None -- are read failures, not
        # endings.
        "EndGameMenu",
        None,
    ],
)
async def test_a_read_that_names_no_ending_plays_the_turn_exactly_as_before(
    tmp_path: Path, read: Any
) -> None:
    run_id = RunId(f"run-not-over-{uuid.uuid4().hex[:8]}")
    store = _fresh_store(tmp_path, run_id)
    host = FakeHostPlatform()
    saves = _CountingSaveCapability(host, tmp_path)
    probe = _ScriptedGameOverRead(read)
    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=store, saves=saves, read_game_over=probe
    )

    outcome = await run_turn_cycle(deps, run=_run(run_id))

    assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
    assert probe.calls == 1
    assert saves.attempts == 1
    assert store.list_run_events(run_id, event_types=[RunEventType.GAME_OVER_DETECTED]) == []
    assert store.get_turn_cycle(run_id, 1) is not None


async def test_a_game_over_read_that_errors_is_not_a_game_over(tmp_path: Path) -> None:
    """The rule that keeps this check safe to add: a transport failure, a Lua accessor this build
    does not have, a malformed body -- none of them may terminate a run. The cost of a missed
    detection is the save error this replaces; the cost of a fabricated one is a run recorded as
    defeated that was still being played."""
    run_id = RunId("run-read-errored")
    store = _fresh_store(tmp_path, run_id)
    host = FakeHostPlatform()
    saves = _CountingSaveCapability(host, tmp_path)
    probe = _ScriptedGameOverRead(error=NexusError("the tuner did not answer"))
    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=store, saves=saves, read_game_over=probe
    )

    outcome = await run_turn_cycle(deps, run=_run(run_id))

    assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
    assert probe.calls == 1
    assert saves.attempts == 1
    assert store.list_run_events(run_id, event_types=[RunEventType.GAME_OVER_DETECTED]) == []


async def test_a_turn_cycle_composed_without_the_reader_behaves_exactly_as_it_always_did(
    tmp_path: Path,
) -> None:
    """`read_game_over=None` is the honest default for a caller driving a scripted in-memory game
    with no client to ask -- every pre-existing caller of this module keeps its behaviour."""
    run_id = RunId("run-no-reader")
    store = _fresh_store(tmp_path, run_id)
    host = FakeHostPlatform()
    saves = _CountingSaveCapability(host, tmp_path)
    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=store, saves=saves, read_game_over=None
    )

    outcome = await run_turn_cycle(deps, run=_run(run_id))

    assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
    assert saves.attempts == 1


def test_interpretation_never_names_a_winner_on_a_game_that_is_not_over() -> None:
    """Principle I, re-asserted on the Python side of the boundary: even a Lua result that somehow
    carried a victory type and a winner while reporting the game still running has both dropped."""
    read = interpret_game_over(
        {
            "game_over": False,
            "local_player_alive": True,
            "victory_type": "Domination Victory",
            "winner": "Georgia",
        }
    )
    assert read.game_over is False
    assert read.stop_resolution is None
    assert read.victory_type is None
    assert read.winner is None
    assert interpret_game_over("not a mapping") == NO_GAME_OVER


def test_interpretation_drops_a_blank_name_rather_than_recording_it_as_one() -> None:
    read = interpret_game_over(
        {"game_over": True, "outcome": "victory", "victory_type": "  ", "winner": ""}
    )
    assert read.stop_resolution is StopResolution.VICTORY
    assert read.victory_type is None
    assert read.winner is None


# --------------------------------------------------------------------------
# Part 2: the same endings through the real Runner
# --------------------------------------------------------------------------


def _build_runner(
    *,
    tmp_path: Path,
    store: SqliteMatchStore,
    stop_condition: StopCondition,
    game_over_on_turn: int,
    read: Mapping[str, Any],
    facts_seen: list[int],
    outcome_after_game_over: GameOutcome | None = None,
    facts_raise: bool = False,
) -> tuple[Runner, dict[str, _CountingSaveCapability]]:
    registry = _build_registry()
    host = FakeHostPlatform()
    host_info = HostInfo(
        os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
    )
    game = _FakeGame()
    provider = FakeModelProvider()
    provider.set_default_decision_factory(_end_turn_decision)
    saves_by_run: dict[str, _CountingSaveCapability] = {}

    def prepare_run(config: RunConfiguration) -> PreparedRun:
        run = _run(RunId(f"run-{uuid.uuid4().hex}"), config.config_id)
        store.create_run(run, config)
        return PreparedRun(run=run, stop_condition=stop_condition)

    def build_turn_dependencies(prepared: PreparedRun, turn_number: int) -> TurnCycleDependencies:
        run_id = prepared.run.run_id
        saves = saves_by_run.setdefault(str(run_id), _CountingSaveCapability(host, tmp_path))

        async def read_game_over() -> Any:
            return read if turn_number >= game_over_on_turn else STILL_PLAYING

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
            save_capability=saves,
            host=host,
            min_free_disk_gb=1.0,
            disk_check_path=tmp_path,
            build_loop_context=build_loop_context,
            recovery=RecoveryEngine(
                run_id=run_id, store=store, loader=_FakeSaveLoader(), recovery_attempt_limit=3
            ),
            home=tmp_path,
            read_game_over=read_game_over,
        )

    def evaluate_stop_facts(prepared: PreparedRun, turn_number: int) -> StopEvaluation:
        # The production composition's own seam does the terminal-state close here (it releases
        # the run's client and identity lock), so the runner must still call it on a game over.
        # This stand-in records that it was called, and with which turn.
        facts_seen.append(turn_number)
        if facts_raise:
            raise RuntimeError("the composition's own teardown blew up")
        return StopEvaluation(current_turn=turn_number, game_outcome=outcome_after_game_over)

    runner = Runner(
        RunnerDependencies(
            store=store,
            prepare_run=prepare_run,
            build_turn_dependencies=build_turn_dependencies,
            evaluate_stop_facts=evaluate_stop_facts,
        )
    )
    return runner, saves_by_run


async def _wait_until_terminal(
    runner: Runner, run_id: RunId, *, timeout_s: float = 15.0
) -> Any:
    """The poll loop both live drivers run verbatim (`tests/live/demo_landed_run.py`'s
    `TERMINAL` break and `tests/live/goal_run.py`'s `terminal` break): ask `get_status`, stop on a
    terminal lifecycle state. A run that finishes on a detected game over must land here, not sit
    in `playing` forever."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while True:
        status = runner.get_status(run_id)
        if status.lifecycle_state in (LifecycleState.FINISHED, LifecycleState.FAILED):
            return status
        if loop.time() > deadline:  # pragma: no cover - only on a regression
            raise AssertionError(f"run did not reach a terminal state in time: {status!r}")
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        (DEFEAT_BY_ELIMINATION, StopResolution.DEFEAT),
        (VICTORY, StopResolution.VICTORY),
    ],
)
async def test_the_runner_finishes_the_run_on_a_detected_game_over(
    tmp_path: Path, read: Mapping[str, Any], expected: StopResolution
) -> None:
    store = SqliteMatchStore(tmp_path / "match.db")
    facts_seen: list[int] = []
    runner, saves_by_run = _build_runner(
        tmp_path=tmp_path,
        store=store,
        stop_condition=TurnReachedStopCondition(turn=50),
        # Turn 1 plays normally; turn 2 opens onto a finished game, exactly as the live run did.
        game_over_on_turn=2,
        read=read,
        facts_seen=facts_seen,
    )
    try:
        run_id = runner.start(CONFIG_PATH)
        status = await _wait_until_terminal(runner, run_id)
    finally:
        store.close()

    assert status.lifecycle_state is LifecycleState.FINISHED
    # The ending is not a fault: nothing is reported to an operator as a last error.
    assert status.last_error is None
    # The terminal-state close seam was reached, on the turn that discovered the ending.
    assert facts_seen[-1] == 2

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        persisted = store.get_run(run_id)
        assert persisted is not None
        assert persisted.lifecycle_state is LifecycleState.FINISHED
        assert persisted.stop_resolution is expected
        assert persisted.ended_at is not None

        (over,) = store.list_run_events(run_id, event_types=[RunEventType.GAME_OVER_DETECTED])
        assert over.turn_number == 2
        assert over.detail["stop_resolution"] == expected.value

        transitions = store.list_run_events(
            run_id, event_types=[RunEventType.LIFECYCLE_TRANSITION]
        )
        finishing = [e for e in transitions if e.detail.get("to") == "finished"]
        assert len(finishing) == 1

        # No gap: turn 1 is a complete authoritative attempt, and turn 2 never took a quicksave,
        # so the record owes it nothing (store/completeness.py).
        assert store.turn_gaps(run_id) == []
        assert persisted.record_completeness_status is RecordCompletenessStatus.COMPLETE
        assert [save.turn_number for save in store.list_save_points(run_id)] == [1]
        assert store.get_turn_cycle(run_id, 2, authoritative_only=False) is None
    finally:
        store.close()

    # And the failed turn never asked the finished game for a save.
    assert saves_by_run[str(run_id)].attempts == 1


async def test_a_coinciding_condition_is_kept_as_an_event_beside_the_single_resolution(
    tmp_path: Path,
) -> None:
    """Invariant I10: the game's own ending is what is recorded, and the turn cap that happened to
    fall on the same turn survives on the timeline rather than being dropped."""
    store = SqliteMatchStore(tmp_path / "match.db")
    facts_seen: list[int] = []
    runner, _saves = _build_runner(
        tmp_path=tmp_path,
        store=store,
        stop_condition=TurnReachedStopCondition(turn=2),
        game_over_on_turn=2,
        read=DEFEAT_BY_RIVAL_VICTORY,
        facts_seen=facts_seen,
    )
    try:
        run_id = runner.start(CONFIG_PATH)
        status = await _wait_until_terminal(runner, run_id)
    finally:
        store.close()

    assert status.lifecycle_state is LifecycleState.FINISHED

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        persisted = store.get_run(run_id)
        assert persisted is not None
        assert persisted.stop_resolution is StopResolution.DEFEAT
        transitions = store.list_run_events(
            run_id, event_types=[RunEventType.LIFECYCLE_TRANSITION]
        )
        coincident = [e for e in transitions if "coincident_stop_resolution" in e.detail]
        assert [e.detail["coincident_stop_resolution"] for e in coincident] == [
            StopResolution.TURN_REACHED.value
        ]
    finally:
        store.close()


async def test_a_failing_stop_facts_seam_still_records_why_the_run_ended(tmp_path: Path) -> None:
    """The bookkeeping seam must never be able to stop the run from recording its own ending."""
    store = SqliteMatchStore(tmp_path / "match.db")
    facts_seen: list[int] = []
    runner, _saves = _build_runner(
        tmp_path=tmp_path,
        store=store,
        stop_condition=TurnReachedStopCondition(turn=50),
        game_over_on_turn=1,
        read=DEFEAT_BY_ELIMINATION,
        facts_seen=facts_seen,
        facts_raise=True,
    )
    try:
        run_id = runner.start(CONFIG_PATH)
        status = await _wait_until_terminal(runner, run_id)
    finally:
        store.close()

    assert status.lifecycle_state is LifecycleState.FINISHED

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        persisted = store.get_run(run_id)
        assert persisted is not None
        assert persisted.stop_resolution is StopResolution.DEFEAT
        over = store.list_run_events(run_id, event_types=[RunEventType.GAME_OVER_DETECTED])
        assert len(over) == 1
    finally:
        store.close()


async def test_the_live_drivers_poll_loops_exit_on_a_run_finished_by_a_game_over(
    tmp_path: Path,
) -> None:
    """Both live drivers break their poll loop on `status.lifecycle_state in TERMINAL` and the
    demo driver's process exit code is `0` exactly when `final_state == "finished"`. A run that
    ends on a detected defeat must satisfy both, so a game over costs a block seconds rather than
    hanging it (`tests/live/demo_landed_run.py`, `tests/live/goal_run.py`)."""
    # `tests/` is on sys.path (that is how `fakes.*` resolves), and `tests/live/` is a package,
    # so the driver imports as `live.demo_landed_run`. Importing it runs no client work.
    demo = pytest.importorskip("live.demo_landed_run")

    store = SqliteMatchStore(tmp_path / "match.db")
    facts_seen: list[int] = []
    runner, _saves = _build_runner(
        tmp_path=tmp_path,
        store=store,
        stop_condition=TurnReachedStopCondition(turn=50),
        game_over_on_turn=1,
        read=DEFEAT_BY_ELIMINATION,
        facts_seen=facts_seen,
    )
    try:
        run_id = runner.start(CONFIG_PATH)
        status = await _wait_until_terminal(runner, run_id)
    finally:
        store.close()

    assert status.lifecycle_state in demo.TERMINAL
    assert status.lifecycle_state is not LifecycleState.PAUSED
    final_state = status.lifecycle_state.value
    assert final_state == "finished"
    assert (0 if final_state == "finished" else 1) == 0
