"""T142 -- crash recovery, end to end, over a real (fake) Nexus wire connection.

FR-044 - FR-047: the fake Nexus drops the connection mid-turn at a specific decision step,
simulating a crash; the harness's own detection (``resilience/detector.py``'s process-liveness
signal) records a ``crash_detected`` event; the run resumes from *that turn's* quicksave as the
*same* run (``resilience/recovery.py``); it re-observes before acting rather than trusting
anything captured before the interruption; and the turn's persisted record carries exactly one
``abandoned`` attempt (retaining the steps it completed) and one ``authoritative`` attempt.

Unlike ``tests/unit/test_turn_state_machine.py``'s own mid-turn-failure case (T062, which drives
an in-memory fake that raises ``ObservationAssemblyError`` directly), this file drives the
failure through a real ``civsim_harness.nexus.client.NexusClient`` talking to
``tests/fakes/fake_nexus.py``'s ``FakeNexusServer`` via ``drop_connection_at`` -- an actual
dropped TCP connection -- and through the real ``resilience.detector.check_process_liveness``
signal, so the ``crash_detected`` event this test asserts on is produced by production detection
code, not merely simulated.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import psutil
import pytest

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import NexusError, ObservationAssemblyError
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
from civsim_harness.nexus.client import NexusClient
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.port import RawDecision
from civsim_harness.resilience.detector import check_process_liveness
from civsim_harness.resilience.liveness import ProcessLivenessMonitor
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.turn_cycle import TurnCycleDependencies, run_turn_cycle
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_nexus import STATE_INDEX_GAME_CORE_TUNER, STATE_INDEX_IN_GAME, FakeNexusServer
from fakes.fake_provider import FakeModelProvider

TICK_DECLARATION_ID = DeclarationId("test.tick")
GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")

READ_LUA = "CivSim_TurnControl.read_turn_state"
TICK_LUA = "CivSim_TurnControl.tick"


def _turn_state_response(turn_number: int) -> dict[str, Any]:
    return {
        "turn_number": turn_number,
        "is_local_player_turn": True,
        "is_waiting_for_other_players": False,
    }


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


class _NexusBackedGame:
    """Reads and executes go over a real ``NexusClient`` connected to a ``FakeNexusServer``.

    On a ``NexusError`` (the connection having been dropped -- ``drop_connection_at``), this
    performs the harness's real crash-detection signal (``resilience.detector.
    check_process_liveness`` against a PID that is genuinely not a live process, per
    ``resilience/liveness.py``'s own point-in-time semantics), durably records the resulting
    ``crash_detected`` event, and raises ``ObservationAssemblyError`` so that ``run/decision_loop.
    py``'s already-built T096/T152 abandon-and-replay path takes over -- exactly the composition
    ``run/turn_cycle.py``'s own module docstring describes as "a different wave's task" to wire.
    """

    def __init__(
        self, *, server: FakeNexusServer, store: Any, run_id: RunId, turn_number: int, dead_pid: int
    ) -> None:
        self._server = server
        self._store = store
        self._run_id = run_id
        self._turn_number = turn_number
        self._dead_pid = dead_pid
        self._client: NexusClient | None = None
        self.crash_events: list[Any] = []

    async def _connected_client(self) -> NexusClient:
        if self._client is None:
            client = NexusClient(host="127.0.0.1", port=self._server.port)
            await client.connect()
            self._client = client
        return self._client

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        client = await self._connected_client()
        try:
            result = await client.execute_command(
                state_index=STATE_INDEX_GAME_CORE_TUNER, lua_body=READ_LUA
            )
        except NexusError as exc:
            # The drop is a one-shot simulated crash -- clear it so the *replayed* attempt's own
            # (new) connection is not dropped again at whatever ordinal it happens to reach.
            self._server.clear_drop()
            await client.close()
            self._client = None

            event = check_process_liveness(
                ProcessLivenessMonitor(pid=self._dead_pid),
                run_id=self._run_id,
                occurred_at=datetime.now(UTC),
                turn_number=self._turn_number,
            )
            assert event is not None  # the scripted PID is genuinely not alive
            self._store.write_run_event(event)
            self.crash_events.append(event)

            raise ObservationAssemblyError(
                "observation could not be assembled: the Nexus connection dropped mid-turn "
                "(crash_detected)",
                detail={"cause": str(exc)},
            ) from exc
        return (
            [CapabilityResult(declaration_id=GAME_TURN_STATE_DECLARATION_ID, value=result)],
            "world_view",
        )

    async def execute(
        self, declaration_id: DeclarationId, parameters: Mapping[str, Any], target: Any
    ) -> None:
        assert declaration_id == TICK_DECLARATION_ID
        client = await self._connected_client()
        await client.execute_command(state_index=STATE_INDEX_IN_GAME, lua_body=TICK_LUA)

    async def close(self) -> None:
        """Close whatever connection is currently open, if any -- ``FakeNexusServer.stop()``'s
        own ``wait_closed()`` waits for every accepted connection's handler task to finish, not
        merely for the listening socket, so a still-open client connection left dangling at the
        end of a test would otherwise hang ``server.stop()`` forever."""
        if self._client is not None:
            await self._client.close()
            self._client = None


class _FakeSaveCapability:
    def __init__(self, host: HostPlatform, home: Path) -> None:
        self._host = host
        self._home = home

    async def save_game(self, save_name: str) -> None:
        saves_dir = self._host.resolve_game_directories(home=self._home).saves_dir
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / f"{save_name}.Civ6Save").write_bytes(b"fake-save-data")


class _FakeSaveLoader:
    """Recovery's own reload target is the Nexus connection/observation itself here (the next
    ``read()`` genuinely shows turn_number=1 again, scripted below) -- this loader has nothing
    further to restore, matching the pattern already established in
    ``tests/unit/test_turn_state_machine.py``/``tests/integration/test_turn_endings.py``."""

    async def load(self, save: SavePoint) -> None:
        return None


def _build_run_and_config(run_id: RunId) -> tuple[Run, RunConfiguration]:
    now = datetime.now(UTC)
    config = RunConfiguration(
        config_id=ConfigId("cfg-recovery"),
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


def _dead_pid() -> int:
    """A PID that is genuinely not a live process right now (the same convention
    ``tests/unit/test_detection.py`` already uses for this exact purpose)."""
    pid = 1
    while psutil.pid_exists(pid):
        pid += 1
    return pid


async def test_crash_mid_turn_is_detected_and_the_turn_recovers_as_the_same_run(
    tmp_path: Path,
) -> None:
    run_id = RunId("run-recovery")
    run, config = _build_run_and_config(run_id)
    store = SqliteMatchStore(tmp_path / "match.db")
    store.create_run(run, config)

    server = FakeNexusServer()
    # Attempt 0 (conn 1): read(1) -> tick -> read(2) -> tick -> read DROPS on the 5th command.
    server.queue_response(_turn_state_response(1), match=READ_LUA)
    server.queue_response(_turn_state_response(2), match=READ_LUA)
    # Attempt 1 (conn 2, replayed from the turn-start quicksave): read(1) -> tick+end -> read(2).
    server.queue_response(_turn_state_response(1), match=READ_LUA)
    server.queue_response(_turn_state_response(2), match=READ_LUA)
    server.drop_connection_at(5)
    await server.start()

    game = _NexusBackedGame(
        server=server, store=store, run_id=run_id, turn_number=1, dead_pid=_dead_pid()
    )
    provider = FakeModelProvider()

    def _tick(*, is_end_turn: bool) -> RawDecision:
        return RawDecision(
            action_declaration_id=TICK_DECLARATION_ID,
            reasoning="advance",
            parameters={},
            is_end_turn=is_end_turn,
            prompt_type=None,
        )

    # Attempt 0: two productive steps, the second interrupted before it can be recorded.
    provider.queue_decision(_tick(is_end_turn=False))
    provider.queue_decision(_tick(is_end_turn=False))
    # Attempt 1 (the replay): one step, ends the turn immediately.
    provider.queue_decision(_tick(is_end_turn=True))

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

    deps = TurnCycleDependencies(
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

    try:
        outcome = await run_turn_cycle(deps, run=run)
    finally:
        await game.close()
        await server.stop()
        store.close()

    assert outcome.outcome is TurnOutcome.ENDED_BY_AGENT
    # FR-045: the same continuous run -- recovery drove it interrupted -> resuming -> playing,
    # never a different run_id.
    assert outcome.run.run_id == run_id
    assert outcome.run.lifecycle_state is LifecycleState.PLAYING

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        # -- a crash_detected event is recorded (FR-044) --
        crash_events = store.list_run_events(run_id, event_types=[RunEventType.CRASH_DETECTED])
        assert len(crash_events) == 1
        assert crash_events[0].turn_number == 1
        assert crash_events[0].detail["pid"] == game._dead_pid  # noqa: SLF001

        # -- the run resumed from THIS turn's quicksave as the same run, re-observing before
        # acting (FR-045, FR-046) -- proven both by the recorded event trail and by the replayed
        # attempt's own steps showing a fresh read (turn_number=1 again), not anything the
        # abandoned attempt had already seen (which had progressed to turn_number=2).
        for event_type in (
            RunEventType.TURN_ABANDONED,
            RunEventType.OBSERVATION_ASSEMBLY_FAILED,
            RunEventType.RESUMED,
        ):
            events = store.list_run_events(run_id, event_types=[event_type])
            assert len(events) == 1, f"expected exactly one {event_type.value} event"

        # -- the turn carries one abandoned and one authoritative attempt (FR-047) --
        record = store.get_turn_cycle(run_id, 1)
        assert record is not None
        assert record.turn_cycle.is_authoritative is True
        assert record.turn_cycle.attempt_index == 1
        assert record.turn_cycle.outcome is TurnOutcome.ENDED_BY_AGENT
        assert record.turn_cycle.step_count == 1
        assert len(record.steps) == 1
        # The replayed attempt's one step shows the *reloaded* turn state (1), not a continuation
        # from where the abandoned attempt left off (2) -- re-observed before acting, genuinely.
        replayed_turn_value = next(
            entry.value["turn_number"]
            for entry in record.steps[0].observation.entries
            if entry.declaration_id == GAME_TURN_STATE_DECLARATION_ID
        )
        assert replayed_turn_value == 1

        abandoned = store.get_turn_cycle(run_id, 1, authoritative_only=False)
        # get_turn_cycle(authoritative_only=False) returns the most recent attempt regardless of
        # status; since the authoritative (replayed) attempt is itself the most recent, fall back
        # to a raw count via the store's own event/step evidence for the abandoned one instead.
        assert abandoned is not None

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "match.db"))
        try:
            rows = conn.execute(
                "SELECT attempt_index, is_authoritative, step_count FROM turn_cycles "
                "WHERE run_id = ? AND turn_number = ? ORDER BY attempt_index",
                (run_id, 1),
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 2
        (a0_index, a0_authoritative, a0_step_count), (a1_index, a1_authoritative, a1_step_count) = (
            rows
        )
        assert a0_index == 0
        assert a0_authoritative == 0
        assert a0_step_count == 1  # only the first of the two attempt-0 decisions was recorded
        assert a1_index == 1
        assert a1_authoritative == 1
        assert a1_step_count == 1
        assert sum(row[1] for row in rows) == 1  # exactly one authoritative attempt (invariant I9)
    finally:
        store.close()
