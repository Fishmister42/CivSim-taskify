"""T068 -- stop resolution: exactly one recorded, coincident conditions kept as events.

FR-005, invariant I10: a run resolves to exactly one recorded ``StopResolution`` -- the 5-value
set (``turn_reached | victory | defeat | operator_stop | unrecoverable_failure``) -- even when
more than one condition is true on the same turn. Whichever one is *not* recorded still lands on
the timeline as a plain event, never silently dropped. This is deliberately kept distinct from
``StopCondition.type``, the narrower 3-value set (``turn_reached | game_outcome | operator_stop``)
an operator actually configures a run to stop at (models/config.py, models/run.py's own module
docstrings make the same point) -- ``victory``/``defeat``/``unrecoverable_failure`` are things a
run can resolve *to*, never things it can be configured to stop *at*.

Two halves: ``run/stop.py``'s ``evaluate_stop`` is exercised directly as the pure decision function
it is (no store, no lifecycle, no observation) for the precedence rule itself; then the same
coincidence is driven through the real ``run/runner.py`` orchestration against a real
``SqliteMatchStore``, proving the single resolution and the coincident event both actually land in
the persisted record, not just in an in-memory ``StopDecision``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping, Sequence
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
    DeclarationId,
    LuaContext,
    ModelRef,
    RunId,
    TurnCycleId,
)
from civsim_harness.models.config import (
    GameOutcomeStopCondition,
    OperatorStopStopCondition,
    RunConfiguration,
    StopCondition,
    TurnReachedStopCondition,
)
from civsim_harness.models.records import RunEventType, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
    StopResolution,
)
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.port import RawDecision
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.runner import PreparedRun, Runner, RunnerDependencies
from civsim_harness.run.stop import GameOutcome, StopDecision, StopEvaluation, evaluate_stop
from civsim_harness.run.turn_cycle import TurnCycleDependencies
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "configs" / "turn50-validation.yaml"

TICK_DECLARATION_ID = DeclarationId("test.tick")
GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")


# --------------------------------------------------------------------------
# Part 1: evaluate_stop as a pure decision function (no store, no runner)
# --------------------------------------------------------------------------


def test_exactly_one_resolution_when_nothing_coincides() -> None:
    decision = evaluate_stop(
        TurnReachedStopCondition(turn=50), StopEvaluation(current_turn=50)
    )
    assert decision.resolution is StopResolution.TURN_REACHED
    assert decision.coincident == ()


def test_nothing_triggers_before_the_configured_turn() -> None:
    decision = evaluate_stop(
        TurnReachedStopCondition(turn=50), StopEvaluation(current_turn=49)
    )
    assert decision.resolution is None
    assert decision.coincident == ()


def test_victory_coincident_with_turn_reached_records_victory_only() -> None:
    decision = evaluate_stop(
        TurnReachedStopCondition(turn=50),
        StopEvaluation(current_turn=50, game_outcome=GameOutcome.VICTORY),
    )
    assert decision.resolution is StopResolution.VICTORY
    assert decision.coincident == (StopResolution.TURN_REACHED,)


def test_defeat_coincident_with_turn_reached_records_defeat_only() -> None:
    decision = evaluate_stop(
        TurnReachedStopCondition(turn=50),
        StopEvaluation(current_turn=50, game_outcome=GameOutcome.DEFEAT),
    )
    assert decision.resolution is StopResolution.DEFEAT
    assert decision.coincident == (StopResolution.TURN_REACHED,)


def test_crash_outranks_victory_operator_stop_and_turn_reached_when_all_coincide() -> None:
    """The full pile-up (spec edge case): unrecoverable_failure, a resolved game outcome, an
    explicit operator stop, and the configured turn threshold all true on the same turn. Exactly
    one is recorded (the crash), and the other three survive as coincident, in the documented
    precedence order (``run/stop.py``'s own module docstring)."""
    decision = evaluate_stop(
        TurnReachedStopCondition(turn=50),
        StopEvaluation(
            current_turn=50,
            game_outcome=GameOutcome.VICTORY,
            operator_stop_requested=True,
            unrecoverable_failure=True,
        ),
    )
    assert decision.resolution is StopResolution.UNRECOVERABLE_FAILURE
    assert decision.coincident == (
        StopResolution.VICTORY,
        StopResolution.OPERATOR_STOP,
        StopResolution.TURN_REACHED,
    )


def test_operator_stop_is_honoured_even_when_configured_stop_is_game_outcome() -> None:
    """game_outcome and operator_stop are evaluated regardless of the configured type -- an
    explicit stop command is always honoured (FR-004), not only on an operator_stop-configured
    run."""
    decision = evaluate_stop(
        GameOutcomeStopCondition(), StopEvaluation(current_turn=10, operator_stop_requested=True)
    )
    assert decision.resolution is StopResolution.OPERATOR_STOP
    assert decision.coincident == ()


def test_turn_reached_is_gated_on_the_configuration_actually_naming_it() -> None:
    """A game_outcome-configured run does not stop merely because some unrelated turn number was
    reached -- ``turn_reached`` is the one condition gated on the configured type."""
    decision = evaluate_stop(
        GameOutcomeStopCondition(), StopEvaluation(current_turn=50)
    )
    assert decision.resolution is None


def test_stop_decision_rejects_a_coincident_set_with_no_primary_resolution() -> None:
    with pytest.raises(ValueError):
        StopDecision(resolution=None, coincident=(StopResolution.TURN_REACHED,))


def test_stop_decision_rejects_the_resolution_reappearing_in_coincident() -> None:
    with pytest.raises(ValueError):
        StopDecision(
            resolution=StopResolution.VICTORY,
            coincident=(StopResolution.VICTORY,),
        )


def test_the_recorded_and_configured_stop_vocabularies_are_genuinely_distinct() -> None:
    """The recorded ``StopResolution`` (5 values) is strictly broader than the configured
    ``StopCondition.type`` (3 values) -- ``victory``/``defeat``/``unrecoverable_failure`` are
    resolvable outcomes, never configurable conditions (models/run.py's own module docstring)."""
    recorded = {member.value for member in StopResolution}
    assert recorded == {
        "turn_reached",
        "victory",
        "defeat",
        "operator_stop",
        "unrecoverable_failure",
    }

    configured_types = {
        TurnReachedStopCondition(turn=1).type,
        GameOutcomeStopCondition().type,
        OperatorStopStopCondition().type,
    }
    assert configured_types == {"turn_reached", "game_outcome", "operator_stop"}

    # The two outcomes a configured game_outcome condition actually resolves to are members of
    # the recorded set but were never, themselves, configurable condition types.
    assert "victory" not in configured_types
    assert "defeat" not in configured_types
    assert "unrecoverable_failure" not in configured_types


# --------------------------------------------------------------------------
# Part 2: the same coincidence, driven through the real Runner orchestration
# --------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _build_runner(
    *, tmp_path: Path, store: SqliteMatchStore, stop_condition: StopCondition, facts: Any
) -> Runner:
    registry = _build_registry()
    host = FakeHostPlatform()
    host_info = HostInfo(
        os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
    )
    game = _FakeGame()
    provider = FakeModelProvider()
    provider.set_default_decision_factory(
        lambda request: RawDecision(
            action_declaration_id=TICK_DECLARATION_ID,
            reasoning="one step, then end",
            parameters={},
            is_end_turn=True,
            prompt_type=None,
        )
    )

    def prepare_run(config: RunConfiguration) -> PreparedRun:
        run = Run(
            run_id=RunId(f"run-{uuid.uuid4().hex}"),
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
        store.create_run(run, config)
        return PreparedRun(run=run, stop_condition=stop_condition)

    def build_turn_dependencies(prepared: PreparedRun, turn_number: int) -> TurnCycleDependencies:
        run_id = prepared.run.run_id

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

    def evaluate_stop_facts(prepared: PreparedRun, turn_number: int) -> StopEvaluation:
        return facts(turn_number)

    return Runner(
        RunnerDependencies(
            store=store,
            prepare_run=prepare_run,
            build_turn_dependencies=build_turn_dependencies,
            evaluate_stop_facts=evaluate_stop_facts,
        )
    )


async def _wait_until_terminal(runner: Runner, run_id: RunId, *, timeout_s: float = 15.0) -> Any:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while True:
        status = runner.get_status(run_id)
        if status.lifecycle_state in (LifecycleState.FINISHED, LifecycleState.FAILED):
            return status
        if loop.time() > deadline:
            raise AssertionError(f"run did not reach a terminal state in time: {status!r}")
        await asyncio.sleep(0.01)


async def test_runner_records_a_single_resolution_when_victory_coincides_with_turn_reached(
    tmp_path: Path,
) -> None:
    store = SqliteMatchStore(tmp_path / "match.db")
    stop_condition = TurnReachedStopCondition(turn=2)

    def facts(turn_number: int) -> StopEvaluation:
        if turn_number >= 2:
            return StopEvaluation(current_turn=turn_number, game_outcome=GameOutcome.VICTORY)
        return StopEvaluation(current_turn=turn_number)

    runner = _build_runner(
        tmp_path=tmp_path, store=store, stop_condition=stop_condition, facts=facts
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
        # Exactly one recorded resolution -- the victory, not the coinciding turn_reached.
        assert persisted.stop_resolution is StopResolution.VICTORY

        transitions = store.list_run_events(run_id, event_types=[RunEventType.LIFECYCLE_TRANSITION])
        finishing = [e for e in transitions if e.detail.get("to") == "finished"]
        assert len(finishing) == 1

        coincident = [e for e in transitions if "coincident_stop_resolution" in e.detail]
        assert len(coincident) == 1
        coincident_value = coincident[0].detail["coincident_stop_resolution"]
        assert coincident_value == StopResolution.TURN_REACHED.value
        assert coincident[0].turn_number == 2
    finally:
        store.close()


async def test_runner_records_a_single_resolution_when_a_crash_coincides_with_turn_reached(
    tmp_path: Path,
) -> None:
    store = SqliteMatchStore(tmp_path / "match.db")
    stop_condition = TurnReachedStopCondition(turn=2)

    def facts(turn_number: int) -> StopEvaluation:
        if turn_number >= 2:
            return StopEvaluation(current_turn=turn_number, unrecoverable_failure=True)
        return StopEvaluation(current_turn=turn_number)

    runner = _build_runner(
        tmp_path=tmp_path, store=store, stop_condition=stop_condition, facts=facts
    )
    try:
        run_id = runner.start(CONFIG_PATH)
        status = await _wait_until_terminal(runner, run_id)
    finally:
        store.close()

    # unrecoverable_failure reported as a *fact* (as opposed to a raised HarnessError) still
    # resolves through the normal per-turn stop-evaluation path, landing in `finished` --
    # distinct from the `failed` lifecycle state a raised exception would produce via `_fail`.
    assert status.lifecycle_state is LifecycleState.FINISHED

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        persisted = store.get_run(run_id)
        assert persisted is not None
        # The crash outranks the coinciding turn_reached -- exactly one resolution recorded.
        assert persisted.stop_resolution is StopResolution.UNRECOVERABLE_FAILURE

        transitions = store.list_run_events(run_id, event_types=[RunEventType.LIFECYCLE_TRANSITION])
        coincident = [e for e in transitions if "coincident_stop_resolution" in e.detail]
        assert len(coincident) == 1
        coincident_value = coincident[0].detail["coincident_stop_resolution"]
        assert coincident_value == StopResolution.TURN_REACHED.value
    finally:
        store.close()
