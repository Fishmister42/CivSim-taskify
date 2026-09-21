"""T116 defect fix -- ``run/runner.py`` routes every mid-play failure to a *legal* lifecycle
state and never lets a failure inside its own fire-and-forget play loop vanish silently.

Three problems, stacked, all in :meth:`~civsim_harness.run.runner.Runner._play_run` /
(the former) ``Runner._fail``:

1. ``playing -> failed`` is not an edge in ``run/lifecycle.py``'s ``LEGAL_TRANSITIONS`` (only
   ``preparing -> failed`` (FR-002) and ``resuming -> failed`` (FR-048) are legal) -- yet the old
   ``_fail`` unconditionally called ``transition(state.run, FAILED, ...)`` for *any*
   ``HarnessError`` raised while playing, which is the normal case for e.g.
   ``ProviderChainExhausted`` (FR-042 requires **pause**, not fail).
2. That call raised a *second* ``HarnessError`` (the illegal transition itself).
3. ``_play_run`` is always scheduled fire-and-forget
   (``asyncio.run_coroutine_threadsafe``, never awaited) -- so that second exception was silently
   discarded, stranding the run in ``playing`` forever with nothing recorded and nothing
   surfaced via ``get_status``.

This module proves the fix: ``ProviderChainExhausted`` now legally pauses the run (FR-042);
``RecoveryLimitReached`` (already legally failed by ``resilience.recovery.RecoveryEngine`` before
it ever reaches the runner, FR-048) is resynced rather than re-transitioned; and a failure that
occurs *while the runner is trying to record why the run stopped* is still surfaced rather than
swallowed a second time -- never leaving the run parked in ``playing`` as though nothing happened.

Deliberately does **not** touch ``run/lifecycle.py`` or its ``LEGAL_TRANSITIONS`` -- widening the
graph to make ``playing -> failed`` legal would hide this defect rather than fix it, and
``tests/unit/test_lifecycle.py`` already pins that graph.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from civsim_harness.capability.loader import Catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.config.run_config import load_run_configuration_file
from civsim_harness.errors import HarnessError, ObservationAssemblyError
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
from civsim_harness.models.config import ModelConfig, TurnReachedStopCondition
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
from civsim_harness.provider.chain import ProviderChain, RetryPolicy
from civsim_harness.provider.port import DecisionRequest, DecisionResponse, ModelCapabilities
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.runner import PreparedRun, Runner, RunnerDependencies, _RunState
from civsim_harness.run.stop import StopEvaluation
from civsim_harness.run.turn_cycle import TurnCycleDependencies
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from civsim_harness.telemetry.logging import get_harness_logger
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "configs" / "turn50-validation.yaml"

GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")
PRIMARY = ModelRef(provider="test", model="primary")


# --------------------------------------------------------------------------
# Shared harness (adapted from tests/integration/test_provider_resilience.py, trimmed to what
# these unit-level runner tests actually need -- one turn attempt, never a full multi-turn game).
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


class _FakeGame:
    """Always reports turn 1, never asked to execute anything in these scenarios (the provider
    fails, or the observation read itself fails, before any decision is ever dispatched)."""

    async def read(self) -> tuple[Sequence[CapabilityResult], str]:
        return (
            [
                CapabilityResult(
                    declaration_id=GAME_TURN_STATE_DECLARATION_ID,
                    value={
                        "turn_number": 1,
                        "is_local_player_turn": True,
                        "is_waiting_for_other_players": False,
                    },
                )
            ],
            "world",
        )


class _FailingObservationReader:
    """Raises ``ObservationAssemblyError`` on every call -- T096/T152's mid-turn observation
    failure, the trigger ``resilience.recovery.RecoveryEngine.recover_from_observation_assembly_
    error`` abandons and replays from the turn-start quicksave (FR-046)."""

    async def __call__(self) -> tuple[Sequence[CapabilityResult], str]:
        raise ObservationAssemblyError(
            "test-induced observation assembly failure", detail={"reason": "scripted"}
        )


class _FakeSaveCapability:
    def __init__(self, host: HostPlatform, home: Path) -> None:
        self._host = host
        self._home = home

    async def save_game(self, save_name: str) -> None:
        saves_dir = self._host.resolve_game_directories(home=self._home).saves_dir
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / f"{save_name}.Civ6Save").write_bytes(b"fake-save-data")


class _AlwaysFailingSaveLoader:
    """Every ``load()`` raises a plain, non-``FileNotFoundError`` exception -- simulating "the game
    client never came back", the failure mode ``RecoveryEngine._on_recovery_attempt_failed`` (not
    the separate missing-save path) counts against ``recovery_attempt_limit`` (FR-048)."""

    async def load(self, save: SavePoint) -> None:
        raise RuntimeError("test-induced: the game client never came back")


class _ChainBackedProvider:
    """Bridges ``provider.chain.ProviderChain`` to the plain ``ModelProvider`` shape
    ``run/decision_loop.py`` calls -- see ``tests/integration/test_provider_resilience.py``, which
    documents this composition in full; retry/fallback/exhaustion all happen inside
    ``complete_step`` before this ever returns or raises."""

    def __init__(self, chain: ProviderChain, *, run_id: RunId, turn_number: int) -> None:
        self._chain = chain
        self._run_id = run_id
        self._turn_number = turn_number

    def describe(self, model: ModelRef) -> ModelCapabilities:  # pragma: no cover - never called
        raise NotImplementedError

    def complete(self, request: DecisionRequest) -> DecisionResponse:
        return self._chain.complete_step(
            request, run_id=self._run_id, turn_number=self._turn_number
        )


def _host_info() -> HostInfo:
    return HostInfo(os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11)


def _build_run(config_id: ConfigId) -> Run:
    return Run(
        run_id=RunId(f"run-{uuid.uuid4().hex}"),
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


def _make_prepared_run(store: SqliteMatchStore) -> PreparedRun:
    """A real ``RunConfiguration`` (``configs/turn50-validation.yaml``, the same fixture
    ``tests/integration/test_provider_resilience.py`` uses) -- these tests only ever care about
    the resulting ``Run``/``PreparedRun``, never about the configuration's own game-setup
    content, so there is no reason to hand-roll a second, narrower schema here."""
    config = load_run_configuration_file(CONFIG_PATH)
    run = _build_run(config.config_id)
    store.create_run(run, config)
    return PreparedRun(run=run, stop_condition=TurnReachedStopCondition(turn=50))


def _evaluate_stop_facts(prepared: PreparedRun, turn_number: int) -> StopEvaluation:
    return StopEvaluation(current_turn=turn_number)


def _build_chain_exhaustion_runner(
    *, tmp_path: Path, store: SqliteMatchStore, raw_provider: FakeModelProvider
) -> tuple[Runner, RunId]:
    """One turn, one model, no fallback -- any scripted failure exhausts the chain outright
    (FR-042), exercised through the *real* ``ProviderChain`` so ``model_chain_exhausted`` is
    actually recorded (``provider/chain.py``'s own responsibility, not the runner's)."""
    registry = _build_registry()
    host = FakeHostPlatform()
    game = _FakeGame()
    prepared = _make_prepared_run(store)
    run_id = prepared.run.run_id

    def build_turn_dependencies(prepared: PreparedRun, turn_number: int) -> TurnCycleDependencies:
        chain = ProviderChain(
            raw_provider,
            ModelConfig(primary=PRIMARY),  # no fallback configured -- exhausts on failure
            event_sink=store,
            retry_policy=RetryPolicy(max_attempts_per_model=1, base_delay_s=0.0, jitter_s=0.0),
            sleep=lambda _delay: None,
            rand=lambda: 0.0,
        )
        provider = _ChainBackedProvider(chain, run_id=run_id, turn_number=turn_number)

        def build_loop_context(turn_cycle_id: TurnCycleId) -> DecisionLoopContext:
            return DecisionLoopContext(
                run_id=run_id,
                turn_number=turn_number,
                turn_cycle_id=turn_cycle_id,
                registry=registry,
                catalog_version=CatalogVersionRef(version="test", content_hash="test"),
                model=PRIMARY,
                guidance=None,
                provider=provider,
                no_progress_step_limit=5,
                read_observation_inputs=game.read,
                execute_action=lambda *a, **kw: (_ for _ in ()).throw(  # pragma: no cover
                    AssertionError("no decision should ever be dispatched in this scenario")
                ),
                host=host,
                host_info=_host_info(),
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
                loader=_AlwaysFailingSaveLoader(),
                recovery_attempt_limit=3,
            ),
            home=tmp_path,
        )

    runner = Runner(
        RunnerDependencies(
            store=store,
            prepare_run=lambda _config: prepared,
            build_turn_dependencies=build_turn_dependencies,
            evaluate_stop_facts=_evaluate_stop_facts,
        )
    )
    return runner, run_id


def _build_recovery_limit_runner(
    *, tmp_path: Path, store: SqliteMatchStore
) -> tuple[Runner, RunId]:
    """One turn whose observation can never be assembled and whose recovery reload can never
    succeed -- ``recovery_attempt_limit=1`` means the very first failed reload already exceeds it
    (FR-048)."""
    registry = _build_registry()
    host = FakeHostPlatform()
    prepared = _make_prepared_run(store)
    run_id = prepared.run.run_id

    def build_turn_dependencies(prepared: PreparedRun, turn_number: int) -> TurnCycleDependencies:
        def build_loop_context(turn_cycle_id: TurnCycleId) -> DecisionLoopContext:
            return DecisionLoopContext(
                run_id=run_id,
                turn_number=turn_number,
                turn_cycle_id=turn_cycle_id,
                registry=registry,
                catalog_version=CatalogVersionRef(version="test", content_hash="test"),
                model=PRIMARY,
                guidance=None,
                provider=FakeModelProvider(),  # never called -- observation fails first
                no_progress_step_limit=5,
                read_observation_inputs=_FailingObservationReader(),
                execute_action=lambda *a, **kw: (_ for _ in ()).throw(  # pragma: no cover
                    AssertionError("no decision should ever be dispatched in this scenario")
                ),
                host=host,
                host_info=_host_info(),
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
                loader=_AlwaysFailingSaveLoader(),
                recovery_attempt_limit=1,
            ),
            home=tmp_path,
        )

    runner = Runner(
        RunnerDependencies(
            store=store,
            prepare_run=lambda _config: prepared,
            build_turn_dependencies=build_turn_dependencies,
            evaluate_stop_facts=_evaluate_stop_facts,
        )
    )
    return runner, run_id


async def _wait_until(condition: Any, *, timeout_s: float = 5.0) -> None:
    import asyncio

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while True:
        if condition():
            return
        if loop.time() > deadline:
            raise AssertionError("condition did not become true in time")
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


async def test_chain_exhaustion_pauses_the_run_with_event_recorded_and_status_reflecting_it(
    tmp_path: Path,
) -> None:
    """FR-042 verbatim: chain exhaustion pauses the run in a recorded state (``playing -> paused``
    is legal; ``failed`` is FR-048's recovery-limit outcome, reached only from ``resuming``, never
    from ``playing``). ``model_chain_exhausted`` is recorded by ``provider/chain.py`` itself before
    ``ProviderChainExhausted`` ever reaches the runner -- this test proves the runner's own half:
    it does not try (and fail) to fail the run, it pauses it, and ``get_status`` reflects that."""
    store = SqliteMatchStore(tmp_path / "match.db")
    raw_provider = FakeModelProvider()
    raw_provider.queue_failed()

    runner, run_id = _build_chain_exhaustion_runner(
        tmp_path=tmp_path, store=store, raw_provider=raw_provider
    )
    try:
        runner.start(CONFIG_PATH)
        await _wait_until(lambda: runner.get_status(run_id).last_error is not None)
        status = runner.get_status(run_id)

        assert status.lifecycle_state is LifecycleState.PAUSED
        assert status.last_error is not None
        assert status.last_error.type == "ProviderChainExhausted"

        persisted = store.get_run(run_id)
        assert persisted is not None
        assert persisted.lifecycle_state is LifecycleState.PAUSED
        assert persisted.stop_resolution is None  # paused is not terminal (FR-003, SS4)

        transitions = store.list_run_events(run_id, event_types=[RunEventType.LIFECYCLE_TRANSITION])
        paused_transitions = [e for e in transitions if e.detail.get("to") == "paused"]
        assert len(paused_transitions) == 1

        exhausted_events = store.list_run_events(
            run_id, event_types=[RunEventType.MODEL_CHAIN_EXHAUSTED]
        )
        assert len(exhausted_events) == 1
    finally:
        store.close()


async def test_recovery_limit_reached_fails_via_the_legal_path_with_last_known_good_identified(
    tmp_path: Path,
) -> None:
    """FR-048: once consecutive recovery attempts exceed the configured bound, the run stops in a
    recorded ``failed`` state identifying the last-known-good save -- reached only through the
    legal ``playing -> interrupted -> resuming -> failed`` chain, which
    ``resilience.recovery.RecoveryEngine`` itself already drives and persists before
    ``RecoveryLimitReached`` ever reaches the runner. This proves the runner does not then try to
    ``transition()`` an already-``failed`` run a second time (which would raise on the illegal
    ``failed -> failed`` no-op) -- it resyncs instead."""
    store = SqliteMatchStore(tmp_path / "match.db")

    runner, run_id = _build_recovery_limit_runner(tmp_path=tmp_path, store=store)
    try:
        runner.start(CONFIG_PATH)
        await _wait_until(lambda: runner.get_status(run_id).last_error is not None)
        status = runner.get_status(run_id)

        assert status.lifecycle_state is LifecycleState.FAILED
        assert status.last_error is not None
        assert status.last_error.type == "RecoveryLimitReached"
        # the turn-1 quicksave taken before the observation failure is the last-known-good save.
        assert status.last_known_good_save is not None
        assert status.last_known_good_save.turn == 1

        persisted = store.get_run(run_id)
        assert persisted is not None
        assert persisted.lifecycle_state is LifecycleState.FAILED
        assert persisted.stop_resolution is StopResolution.UNRECOVERABLE_FAILURE

        # reached only through the legal chain -- every one of these transitions is on record.
        transitions = store.list_run_events(run_id, event_types=[RunEventType.LIFECYCLE_TRANSITION])
        seen = [(e.detail.get("from"), e.detail.get("to")) for e in transitions]
        assert ("playing", "interrupted") in seen
        assert ("interrupted", "resuming") in seen
        assert ("resuming", "failed") in seen
        assert ("playing", "failed") not in seen  # the illegal edge is never attempted

        limit_events = store.list_run_events(
            run_id, event_types=[RunEventType.RECOVERY_LIMIT_REACHED]
        )
        assert len(limit_events) == 1
        assert limit_events[0].detail["last_known_good_save_point_id"] is not None
    finally:
        store.close()


class _CapturingLogHandler(logging.Handler):
    """Attached directly to the harness logger (not the root logger) -- ``telemetry.logging``'s
    own ``configure_logging`` sets ``propagate = False``, so a handler on the root logger (e.g.
    pytest's ``caplog``) would never see anything logged here; attaching straight to the named
    logger sidesteps that."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def test_double_failure_inside_failure_handling_is_logged_and_not_swallowed(
    tmp_path: Path,
) -> None:
    """Defense in depth, one level deeper than the defect itself: force the run into a lifecycle
    state ``paused`` cannot legally be reached from (``waiting_on_model``, a real SS4 node nothing
    in this codebase currently transitions into, chosen precisely because it is not ``playing``),
    then let an ordinary ``HarnessError`` reach the runner's failure routing.
    ``_pause_on_failure``'s own ``transition()`` call then itself raises a *second*
    ``HarnessError`` -- exactly the shape of the original defect, but one level deeper (inside the
    routing this fix added, rather than the single unconditional ``_fail`` the old code had). This
    proves the outer safety net
    (``Runner._record_unexpected_failure``) holds even here: the failure is logged loudly through
    ``telemetry.logging`` (never silently dropped), ``state.last_error`` is still set, and the
    exception itself propagates out through the coroutine's own scheduling ``Future`` rather than
    vanishing -- so *if* anything is watching (a caller that does check the Future, a log
    aggregator), nothing about this failure is invisible."""
    store = SqliteMatchStore(tmp_path / "match.db")
    handler = _CapturingLogHandler()
    harness_logger = get_harness_logger()
    harness_logger.addHandler(handler)
    try:
        prepared = _make_prepared_run(store)
        # Corrupt the in-memory run to a state `paused` cannot legally follow, *after* it was
        # already durably created `playing` -- isolating the runner's own routing defect from
        # anything upstream that decides what state a run starts or resumes in.
        run = prepared.run.model_copy(update={"lifecycle_state": LifecycleState.WAITING_ON_MODEL})
        prepared = PreparedRun(run=run, stop_condition=prepared.stop_condition)

        def _raise_boom(_prepared: PreparedRun, _turn_number: int) -> TurnCycleDependencies:
            raise HarnessError("synthetic failure for the double-failure safety net")

        runner = Runner(
            RunnerDependencies(
                store=store,
                prepare_run=lambda _config: prepared,  # never called; state is injected directly
                build_turn_dependencies=_raise_boom,
                evaluate_stop_facts=_evaluate_stop_facts,
            )
        )
        runner._runs[run.run_id] = _RunState(run=run, prepared=prepared)

        future = runner._schedule(runner._play_run(run.run_id))
        with pytest.raises(HarnessError, match="illegal lifecycle transition"):
            future.result(timeout=5.0)

        status = runner.get_status(run.run_id)
        assert status.last_error is not None
        # the run never silently reverts to looking like it is still playing/waiting -- the
        # lifecycle_state on record is whatever it legitimately was, never masked as healthy.
        assert status.lifecycle_state is LifecycleState.WAITING_ON_MODEL

        # logged loudly, not just left for an unread Future to discard.
        assert len(handler.records) == 1
        assert handler.records[0].levelno == logging.ERROR
        assert handler.records[0].exc_info is not None
    finally:
        harness_logger.removeHandler(handler)
        store.close()


async def test_a_run_never_remains_non_terminal_after_an_unrecoverable_condition(
    tmp_path: Path,
) -> None:
    """General invariant behind the whole fix: whatever kind of unrecoverable ``HarnessError``
    ends a turn's play loop, the run always lands somewhere other than ``playing`` -- never
    stranded there as though nothing happened. Uses a plain, unclassified ``HarnessError`` (not
    one of the two specifically-routed kinds) to prove the generic fallback path also moves the
    run out of ``playing``, into the one legal, recorded state it can still reach."""
    store = SqliteMatchStore(tmp_path / "match.db")
    host = FakeHostPlatform()
    prepared = _make_prepared_run(store)
    run_id = prepared.run.run_id

    def build_turn_dependencies(prepared: PreparedRun, turn_number: int) -> TurnCycleDependencies:
        def build_loop_context(turn_cycle_id: TurnCycleId) -> DecisionLoopContext:
            raise HarnessError("unclassified mid-turn-dependency-build failure")

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
                loader=_AlwaysFailingSaveLoader(),
                recovery_attempt_limit=3,
            ),
            home=tmp_path,
        )

    runner = Runner(
        RunnerDependencies(
            store=store,
            prepare_run=lambda _config: prepared,
            build_turn_dependencies=build_turn_dependencies,
            evaluate_stop_facts=_evaluate_stop_facts,
        )
    )
    try:
        runner.start(CONFIG_PATH)
        await _wait_until(lambda: runner.get_status(run_id).last_error is not None)
        status = runner.get_status(run_id)

        assert status.lifecycle_state not in (
            LifecycleState.PREPARING,
            LifecycleState.PLAYING,
            LifecycleState.WAITING_ON_MODEL,
            LifecycleState.WAITING_ON_GAME,
            LifecycleState.INTERRUPTED,
            LifecycleState.RESUMING,
        )
        assert status.lifecycle_state is LifecycleState.PAUSED
    finally:
        store.close()


async def test_completeness_served_and_persisted_is_the_derivation_not_the_creation_stamp(
    tmp_path: Path,
) -> None:
    """T239 / FR-052, Principle III: the mid-flight-death shape. This file's own ``_build_run``
    stamps ``record_completeness_status=COMPLETE`` at creation -- exactly the constructor
    constant the audit found served through ``run status`` forever. The run below quicksaves
    turn 1 and then exhausts its provider chain before a single ``TurnCycle`` is persisted, so
    the record genuinely has a gap behind it: both the *served* value (``get_status``, which now
    derives fresh from the store) and the *persisted* value (re-derived when the runner records
    the pause) must say ``has_gaps``, the derivation's answer -- never the stamp's ``complete``.

    Revert either half of the wiring and this fails with the live symptom: with ``get_status``
    serving ``state.run.record_completeness_status`` again, the served value is the stamp's
    ``complete``; with ``_pause_on_failure``'s refresh removed, the persisted Run keeps it."""
    store = SqliteMatchStore(tmp_path / "match.db")
    raw_provider = FakeModelProvider()
    raw_provider.queue_failed()

    runner, run_id = _build_chain_exhaustion_runner(
        tmp_path=tmp_path, store=store, raw_provider=raw_provider
    )
    try:
        runner.start(CONFIG_PATH)
        await _wait_until(lambda: runner.get_status(run_id).last_error is not None)

        # Served: run status reports the derivation, fresh from the record.
        status = runner.get_status(run_id)
        assert status.lifecycle_state is LifecycleState.PAUSED
        assert status.record_completeness_status is RecordCompletenessStatus.HAS_GAPS, (
            "run status served the creation-time stamp instead of the derivation: turn 1 has a "
            "quicksave and no TurnCycle, which Principle III requires to surface as has_gaps"
        )

        # Persisted: the Run the store holds -- what Deliverable 1's trend gate reads across
        # the port -- carries the derivation too, not the stamp it was created with.
        persisted = store.get_run(run_id)
        assert persisted is not None
        assert persisted.record_completeness_status is RecordCompletenessStatus.HAS_GAPS, (
            "the persisted Run still carries its creation-time completeness stamp; the gap "
            "behind this paused run would silently qualify it for trending (FR-052)"
        )
    finally:
        store.close()
