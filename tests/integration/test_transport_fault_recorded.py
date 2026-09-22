"""T281 -- a client death on the wire must end the run in a *recorded* state, not strand it.

**The live finding this closes** (2026-09-21): the game client died during the pre-save prompt
probe and the run's stop reason came out `None`. FR-005 requires every run to end on exactly one
recorded stop condition; data-model.md pins that as invariant **I10**. Nothing was recorded at
all -- the run sat in `playing` forever.

**Why**, and why it is a one-line-shaped fix rather than new error handling. `ConnectionReset
Error` is an `OSError`, not a `civsim_harness.errors.HarnessError`. `Runner._play_run` routes a
`HarnessError` to `_handle_run_failure`, which is exhaustive over that hierarchy and lands every
member in a legal recorded lifecycle state (`paused` for anything unclassified -- the one legal,
non-destructive state `playing` can still reach). A raw `OSError` misses that handler entirely and
falls to the outer `except Exception` -> `_record_unexpected_failure`, which sets an in-memory
flag and logs, and performs **no lifecycle transition and writes no stop resolution**.

So this file is the *production-path* half of T281's proof. `tests/unit/test_nexus_transport_
faults.py` proves the transport now raises `NexusError` where it used to raise `Connection
ResetError`; this one proves that change alone -- with no new handling logic anywhere downstream
-- is what makes the run's failure get recorded.

**The negative control.** Remove either `except OSError` arm from
`civsim_harness.nexus.client._send_raw` / `_read_frame` and this test fails at its first
assertion: the run is still `PLAYING` and `last_error.type` is `"ConnectionResetError"`, with no
`paused` lifecycle_transition event ever written. That is exactly the stranded run the live
session saw.

**How much of the production stack is real here.** The `NexusClient` and its socket, the real
`observe.reader.ObservationReader`, the real `run/decision_loop.py`, the real
`run/turn_cycle.py` pre-save prompt probe and the real `run/runner.py` -- all of them. The one
seam replaced by a double is `capability/executor.py`'s `CapabilityExecutor.execute`, which needs
a Lua tree on disk to dispatch; it contains no exception handling whatsoever and its own docstring
promises to raise "`NexusError` for ... any transport/Lua-side failure", so it is a pass-through
for the exception this test is about.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

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
from civsim_harness.models.config import RunConfiguration, TurnReachedStopCondition
from civsim_harness.models.records import RunEventType, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
)
from civsim_harness.nexus.client import REASON_CONNECTION_RESET, NexusClient
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.observe.reader import ObservationReader
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.runner import PreparedRun, Runner, RunnerDependencies
from civsim_harness.run.stop import StopEvaluation
from civsim_harness.run.turn_cycle import TurnCycleDependencies
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.aborting_tuner import AbortingTuner
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "configs" / "turn50-validation.yaml"

GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")
PRIMARY = ModelRef(provider="test", model="primary")


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
    capability = IntegrationCapability(
        capability_id=CapabilityId("test.turn_control"),
        path=CatalogCapabilityPath.FIRETUNER,
        implementation_ref="test",
        reads=["turn state"],
        writes=["turn state"],
    )
    declarations = (turn_state,)
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


class _ClientBackedExecutor:
    """The `CapabilityExecutor` seam `observe.reader.ObservationReader` dispatches through.

    Stands in for `capability/executor.py` only because the real one loads a Lua tree from disk to
    build its dispatch body; the part that matters here -- "await the client, wrap nothing" -- is
    the real one's behaviour verbatim (it has no `except` at all).
    """

    def __init__(self, client: NexusClient, *, state_index: int) -> None:
        self._client = client
        self._state_index = state_index

    async def execute(
        self,
        declaration_id: DeclarationId,
        *,
        context: LuaContext,
        arguments: Sequence[Any] = (),
    ) -> CapabilityResult:
        value = await self._client.execute_command(
            state_index=self._state_index,
            lua_body="return CivSim_TurnState()",
            timeout_s=5.0,
        )
        return CapabilityResult(declaration_id=declaration_id, value=value)


class _FakeSaveCapability:
    def __init__(self, host: HostPlatform, home: Path) -> None:
        self._host = host
        self._home = home

    async def save_game(self, save_name: str) -> None:  # pragma: no cover - never reached
        saves_dir = self._host.resolve_game_directories(home=self._home).saves_dir
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / f"{save_name}.Civ6Save").write_bytes(b"fake-save-data")


class _FakeSaveLoader:
    async def load(self, save: SavePoint) -> None:  # pragma: no cover - never reached
        return None


def _build_runner(
    *, tmp_path: Path, store: SqliteMatchStore, reader: ObservationReader
) -> Runner:
    registry = _build_registry()
    host = FakeHostPlatform()
    host_info = HostInfo(
        os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
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
        return PreparedRun(run=run, stop_condition=TurnReachedStopCondition(turn=50))

    def build_turn_dependencies(prepared: PreparedRun, turn_number: int) -> TurnCycleDependencies:
        run_id = prepared.run.run_id

        def build_loop_context(turn_cycle_id: TurnCycleId) -> DecisionLoopContext:
            return DecisionLoopContext(
                run_id=run_id,
                turn_number=turn_number,
                turn_cycle_id=turn_cycle_id,
                registry=registry,
                catalog_version=CatalogVersionRef(version="test", content_hash="test"),
                model=PRIMARY,
                guidance=None,
                provider=FakeModelProvider(),
                no_progress_step_limit=5,
                read_observation_inputs=reader,
                execute_action=_never_executed,
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
        return StopEvaluation(current_turn=turn_number)

    return Runner(
        RunnerDependencies(
            store=store,
            prepare_run=prepare_run,
            build_turn_dependencies=build_turn_dependencies,
            evaluate_stop_facts=evaluate_stop_facts,
        )
    )


async def _never_executed(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
    raise AssertionError("the run died on its first read; no action should ever be dispatched")


async def _wait_until_stopped(runner: Runner, run_id: RunId, *, timeout_s: float = 10.0) -> Any:
    """Wait for the run to stop making progress -- terminal, paused, *or* merely holding a
    `last_error`. The third case is the stranded-run shape this test exists to rule out, so it
    must be waited for too rather than masked as an opaque timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while True:
        status = runner.get_status(run_id)
        if status.lifecycle_state in (
            LifecycleState.FINISHED,
            LifecycleState.FAILED,
            LifecycleState.PAUSED,
        ):
            return status
        if status.last_error is not None:
            return status
        if loop.time() > deadline:
            raise AssertionError(f"run did not stop making progress in time: {status!r}")
        await asyncio.sleep(0.01)


async def test_a_client_death_on_the_wire_pauses_the_run_in_a_recorded_state(
    tmp_path: Path,
) -> None:
    """A TCP reset under the pre-save probe ends the run in a recorded `paused`, named `NexusError`.

    The turn's very first transport touch is the pre-save prompt probe's fresh read
    (`run/turn_cycle.py`'s `_clear_blocking_prompt_before_save` -> `run_decision_loop` ->
    `_observe`), which is exactly where the live client died.
    """
    tuner = AbortingTuner()
    await tuner.start()
    client = NexusClient(host="127.0.0.1", port=tuner.port)
    indices = await client.connect()
    await tuner.wait_connected()

    reader = ObservationReader(
        executor=_ClientBackedExecutor(client, state_index=indices.in_game or 0),
        registry=_build_registry(),
        declaration_ids=[GAME_TURN_STATE_DECLARATION_ID],
    )

    # The client is now dead the way a killed game is dead: an RST, not an orderly FIN.
    tuner.abort()

    store = SqliteMatchStore(tmp_path / "match.db")
    runner = _build_runner(tmp_path=tmp_path, store=store, reader=reader)
    try:
        run_id = runner.start(CONFIG_PATH)
        status = await _wait_until_stopped(runner, run_id)
    finally:
        store.close()
        await client.close()
        await tuner.stop()

    # BEFORE T281 both of these failed: the raw `ConnectionResetError` missed
    # `Runner._handle_run_failure` entirely, so the run stayed `PLAYING` and the only trace was an
    # in-memory `last_error` of type `ConnectionResetError`.
    assert status.lifecycle_state is LifecycleState.PAUSED
    assert status.last_error is not None
    assert status.last_error.type == "NexusError"

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        persisted = store.get_run(run_id)
        assert persisted is not None
        assert persisted.lifecycle_state is LifecycleState.PAUSED
        # `paused` is not terminal, so no stop_resolution is recorded here -- the run is stopped
        # in a legal, recorded, resumable state rather than concluded.
        assert persisted.stop_resolution is None

        transitions = store.list_run_events(
            run_id, event_types=[RunEventType.LIFECYCLE_TRANSITION]
        )
        paused = [e for e in transitions if e.detail.get("to") == "paused"]
        assert len(paused) == 1
        assert paused[0].detail["error_type"] == "NexusError"
        # The record says *why*, in the transport's own vocabulary -- not merely that something
        # went wrong. This is the whole difference from the live run's `stop_reason: None`.
        assert REASON_CONNECTION_RESET in paused[0].detail["reason"]
    finally:
        store.close()
