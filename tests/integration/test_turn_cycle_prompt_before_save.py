"""A save-blocking prompt is answered *before* the turn-start quicksave (2026-09-21).

MEASURED, gameplay blocks 16 and 17: Gathering Storm's eruption cinematic
(``prompt.natural_disaster``) makes the game refuse every save while it is up, and the quicksave
used to be the first thing a turn did -- so the run deadlocked in 12 s before the agent could
answer. The turn now runs: screen-identity probe -> one prompt-answer decision through the
ordinary decision path (a real, recorded step) -> quicksave -> the rest of the turn. The save
still precedes every non-prompt action, and a save the game still refuses pauses the run with the
error recorded, exactly as before.
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
from civsim_harness.errors import NexusError
from civsim_harness.host.detect import HostInfo, LinuxSessionType, OperatingSystem
from civsim_harness.host.port import HostPlatform
from civsim_harness.models.catalog import (
    CapabilityId,
    CatalogVersion,
    DeclarationKind,
    IntegrationCapability,
    ParityDeclaration,
)
from civsim_harness.models.catalog import CapabilityPath as CatalogCapabilityPath
from civsim_harness.models.common import (
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
from civsim_harness.models.decision import DecisionTrigger
from civsim_harness.models.records import RunEventType
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
from civsim_harness.run import decision_loop as decision_loop_module
from civsim_harness.run.decision_loop import SCREEN_STATE_DECLARATION_ID, DecisionLoopContext
from civsim_harness.run.turn_cycle import TurnCycleDependencies, run_turn_cycle
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

TURN_STATE_ID = DeclarationId("game.turn_state")
TICK_ID = DeclarationId("test.tick")
PROMPT_SCREEN = "prompt.test_prompt"
PROMPT_ACTION_ID = DeclarationId("prompts.test_prompt")
END_TURN_ID = DeclarationId("turn.end_turn")


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
    screen_state = ParityDeclaration(
        declaration_id=SCREEN_STATE_DECLARATION_ID,
        kind=DeclarationKind.OBSERVATION,
        summary="Test-only screen identity.",
        parity_basis="Look at which screen or popup is on top.",
        context=LuaContext.IN_GAME,
        capability_id=capability_id,
        output_schema={
            "type": "object",
            "required": ["screen", "recognized", "has_blocking_prompt"],
            "properties": {
                "screen": {"type": "string"},
                "raw_screen_id": {"type": "string"},
                "recognized": {"type": "boolean"},
                "has_blocking_prompt": {"type": "boolean"},
                "prompt_options": {"type": "array", "items": {"type": "string"}},
            },
        },
        introduced_in_version="test",
    )
    tick = ParityDeclaration(
        declaration_id=TICK_ID,
        kind=DeclarationKind.ACTION,
        summary="Test-only action: always available, advances the counter by one.",
        parity_basis="Click a UI element that always advances the test counter.",
        context=LuaContext.IN_GAME,
        capability_id=capability_id,
        availability_predicate="not game.has_blocking_prompt",
        verification_predicate="game.turn_number == observed_turn_number + 1",
        introduced_in_version="test",
    )
    end_turn = ParityDeclaration(
        declaration_id=END_TURN_ID,
        kind=DeclarationKind.ACTION,
        summary="Test-only end turn.",
        parity_basis="Click the end-turn button.",
        context=LuaContext.IN_GAME,
        capability_id=capability_id,
        availability_predicate="not game.has_blocking_prompt",
        verification_predicate="game.turn_number == observed_turn_number + 1",
        introduced_in_version="test",
    )
    answer = ParityDeclaration(
        declaration_id=PROMPT_ACTION_ID,
        kind=DeclarationKind.ACTION,
        summary="Test-only prompt answer: press the popup's own Continue.",
        parity_basis="Click Continue on the popup.",
        context=LuaContext.IN_GAME,
        capability_id=capability_id,
        availability_predicate=(
            'game.has_blocking_prompt and game.current_screen == "prompt.test_prompt" '
            "and target in prompt.options"
        ),
        verification_predicate='game.current_screen != "prompt.test_prompt"',
        introduced_in_version="test",
    )
    capability = IntegrationCapability(
        capability_id=capability_id,
        path=CatalogCapabilityPath.FIRETUNER,
        implementation_ref="test",
        reads=["turn state", "screen"],
        writes=["turn state", "screen"],
    )
    declarations = (turn_state, screen_state, tick, end_turn, answer)
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
    """A counter plus one popup that blocks saving while it is up."""

    def __init__(self, *, prompt_up: bool) -> None:
        self.counter = 1
        self.prompt_up = prompt_up
        self.executed: list[DeclarationId] = []

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        screen: dict[str, Any] = (
            {
                "screen": PROMPT_SCREEN,
                "raw_screen_id": "TestPopup",
                "recognized": True,
                "has_blocking_prompt": True,
                "prompt_options": ["continue"],
            }
            if self.prompt_up
            else {
                "screen": "world",
                "raw_screen_id": "WorldView",
                "recognized": True,
                "has_blocking_prompt": False,
            }
        )
        return (
            [
                CapabilityResult(
                    declaration_id=TURN_STATE_ID,
                    value={
                        "turn_number": self.counter,
                        "is_local_player_turn": True,
                        "is_waiting_for_other_players": False,
                    },
                ),
                CapabilityResult(declaration_id=SCREEN_STATE_DECLARATION_ID, value=screen),
            ],
            screen["screen"],
        )

    async def execute(
        self, declaration_id: DeclarationId, parameters: Mapping[str, Any], target: Any
    ) -> None:
        self.executed.append(declaration_id)
        if declaration_id == PROMPT_ACTION_ID:
            self.prompt_up = False
        else:
            self.counter += 1


class _SaveRefusedWhilePromptUp:
    """The measured behaviour: the client refuses to save under the cinematic."""

    def __init__(self, host: HostPlatform, home: Path, game: _FakeGame) -> None:
        self._host = host
        self._home = home
        self._game = game
        self.attempts = 0

    async def save_game(self, save_name: str) -> None:
        self.attempts += 1
        if self._game.prompt_up:
            raise NexusError("the game refused the save: a popup is up")
        saves_dir = self._host.resolve_game_directories(home=self._home).saves_dir
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / f"{save_name}.Civ6Save").write_bytes(b"fake-save-data")


class _SaveAlwaysRefused(_SaveRefusedWhilePromptUp):
    async def save_game(self, save_name: str) -> None:
        self.attempts += 1
        raise NexusError("the game refused the save even with the board clear")


class _FakeSaveLoader:
    async def load(self, save: Any) -> None:  # pragma: no cover - never exercised here
        return None


def _decision_factory(request: DecisionRequest) -> RawDecision:
    """Answer the prompt whenever the rendered board shows it (the request carries text, never a
    structured observation -- Principle I); otherwise tick once, then end the turn."""
    board = request.observation
    if PROMPT_SCREEN in board and '"has_blocking_prompt": true' in board:
        return RawDecision(
            action_declaration_id=PROMPT_ACTION_ID,
            reasoning="acknowledge the popup",
            parameters={"target": "continue"},
            is_end_turn=False,
            prompt_type=None,
        )
    # Step numbering continues past the clearance step, so "second proactive step" is what ends
    # the turn -- not a fixed index.
    return RawDecision(
        action_declaration_id=END_TURN_ID if request.step_index >= 2 else TICK_ID,
        reasoning="advance, then end the turn",
        parameters={},
        is_end_turn=request.step_index >= 2,
        prompt_type=None,
    )


def _run(run_id: RunId) -> Run:
    return Run(
        run_id=run_id,
        config_id=ConfigId("cfg-prompt-before-save"),
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
        config_id=ConfigId("cfg-prompt-before-save"),
        map_seed="seed",
        civilization="civ",
        leader="leader",
        ruleset="ruleset",
        difficulty="prince",
        stop_condition=TurnReachedStopCondition(turn=5),
        agent_model_config=ModelConfig(primary=ModelRef(provider="test", model="test-model")),
        no_progress_step_limit=5,
        recovery_attempt_limit=3,
        min_free_disk_gb=1.0,
        created_at=datetime.now(UTC),
    )


def _make_deps(
    *, tmp_path: Path, run_id: RunId, store: SqliteMatchStore, game: _FakeGame, save_capability: Any
) -> TurnCycleDependencies:
    registry = _build_registry()
    host = FakeHostPlatform()
    host_info = HostInfo(
        os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
    )
    provider = FakeModelProvider()
    provider.set_default_decision_factory(_decision_factory)

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


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(decision_loop_module, "END_TURN_CONFIRM_TIMEOUT_S", 0.0)
    monkeypatch.setattr(decision_loop_module, "ACTION_CONFIRM_TIMEOUT_S", 0.0)


def _fresh_store(tmp_path: Path, run_id: RunId) -> SqliteMatchStore:
    store = SqliteMatchStore(tmp_path / "store.db")
    store.create_run(_run(run_id), _config())
    return store


async def test_a_blocking_prompt_is_answered_then_saved_then_the_turn_proceeds(
    tmp_path: Path,
) -> None:
    run_id = RunId("run-prompt-first")
    store = _fresh_store(tmp_path, run_id)
    game = _FakeGame(prompt_up=True)
    host = FakeHostPlatform()
    saves = _SaveRefusedWhilePromptUp(host, tmp_path, game)
    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=store, game=game, save_capability=saves
    )

    outcome = await run_turn_cycle(deps, run=_run(run_id))

    assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
    # The popup was answered by the executor first, then the save landed on the first try.
    assert game.executed[0] == PROMPT_ACTION_ID
    assert saves.attempts == 1

    record = store.get_turn_cycle(run_id, 1)
    assert record is not None
    assert record.turn_cycle.turn_cycle_id == outcome.turn_cycle_id
    steps = record.steps
    assert [s.step.step_index for s in steps] == list(range(1, len(steps) + 1))
    assert steps[0].decision.trigger is DecisionTrigger.PROMPT_RESPONSE
    assert steps[0].decision.action_declaration_id == PROMPT_ACTION_ID
    assert all(s.decision.trigger is DecisionTrigger.PROACTIVE for s in steps[1:])
    assert steps[-1].decision.is_end_turn

    (save_taken,) = store.list_run_events(run_id, event_types=[RunEventType.SAVE_TAKEN])
    assert save_taken.detail["prompt_answered_before_save"] == {
        "step_count": 1,
        "prompt_types": [PROMPT_SCREEN],
        "cleared": True,
    }
    assert store.list_run_events(run_id, event_types=[RunEventType.SAVE_FAILED]) == []


async def test_no_prompt_means_the_save_comes_first_exactly_as_before(tmp_path: Path) -> None:
    run_id = RunId("run-no-prompt")
    store = _fresh_store(tmp_path, run_id)
    game = _FakeGame(prompt_up=False)
    host = FakeHostPlatform()
    saves = _SaveRefusedWhilePromptUp(host, tmp_path, game)
    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=store, game=game, save_capability=saves
    )

    outcome = await run_turn_cycle(deps, run=_run(run_id))

    assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
    assert PROMPT_ACTION_ID not in game.executed
    record = store.get_turn_cycle(run_id, 1)
    assert record is not None
    assert [s.step.step_index for s in record.steps] == [1, 2]
    assert all(s.decision.trigger is DecisionTrigger.PROACTIVE for s in record.steps)
    (save_taken,) = store.list_run_events(run_id, event_types=[RunEventType.SAVE_TAKEN])
    assert "prompt_answered_before_save" not in save_taken.detail


async def test_a_save_still_refused_after_the_answer_pauses_with_the_error_recorded(
    tmp_path: Path,
) -> None:
    run_id = RunId("run-save-still-refused")
    store = _fresh_store(tmp_path, run_id)
    game = _FakeGame(prompt_up=True)
    host = FakeHostPlatform()
    saves = _SaveAlwaysRefused(host, tmp_path, game)
    deps = _make_deps(
        tmp_path=tmp_path, run_id=run_id, store=store, game=game, save_capability=saves
    )

    with pytest.raises(NexusError):
        await run_turn_cycle(deps, run=_run(run_id))

    # The prompt was answered (the agent's call is on record even though no attempt exists)...
    assert game.executed == [PROMPT_ACTION_ID]
    assert len(store.list_model_calls(run_id)) == 1
    # ...and the failed save says a prompt was answered ahead of it.
    (save_failed,) = store.list_run_events(run_id, event_types=[RunEventType.SAVE_FAILED])
    assert save_failed.detail["prompt_answered_before_save"]["step_count"] == 1
    assert "refused the save" in save_failed.detail["reason"]
    assert store.list_run_events(run_id, event_types=[RunEventType.SAVE_TAKEN]) == []
