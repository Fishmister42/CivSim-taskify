"""T143 -- provider resilience end to end: retry, fallback, and chain exhaustion.

FR-041, FR-042, SC-012: a transient failure retries with backoff, then falls back to the next
model in the chain, with **every attempt a recorded run event** (``provider/chain.py``'s P5);
once every model in the chain has failed, the run **pauses** in a recorded state -- FR-042 says
so verbatim ("the harness MUST pause the run in a recorded state"; ``paused`` is reached from
``playing`` via a legal ``run/lifecycle.py`` edge, distinct from FR-048's ``failed``, which is the
*recovery-limit* outcome reached only from ``resuming``) -- alongside a ``model_chain_exhausted``
event, with **zero fabricated, skipped, or defaulted turns** (P6).

``tests/contract/test_model_provider_port.py`` and ``tests/contract/test_provider_resilience_
contract.py`` already exercise ``ProviderChain.complete_step`` directly and in a multi-step
pipeline; this file is the one place that wires a real ``ProviderChain`` into the actual
``run/decision_loop.py`` -> ``run/turn_cycle.py`` -> ``run/runner.py`` orchestration (no such
composition exists anywhere in ``src/`` yet -- decision_loop.py's own docstring names this exact
wiring "a different wave's task"), against a real ``SqliteMatchStore``, so the whole path -- not
just the chain in isolation -- is proven to retry, fall back, and refuse to paper over exhaustion.
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
from civsim_harness.models.config import ModelConfig, RunConfiguration, TurnReachedStopCondition
from civsim_harness.models.records import RunEventType, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
)
from civsim_harness.observe.assemble import CapabilityResult
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.provider.chain import ProviderChain, RetryPolicy
from civsim_harness.provider.port import (
    DecisionRequest,
    DecisionResponse,
    ModelCapabilities,
    RawDecision,
)
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.runner import PreparedRun, Runner, RunnerDependencies
from civsim_harness.run.stop import StopEvaluation
from civsim_harness.run.turn_cycle import TurnCycleDependencies
from civsim_harness.store.completeness import record_completeness_status
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "configs" / "turn50-validation.yaml"

TICK_DECLARATION_ID = DeclarationId("test.tick")
GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")

PRIMARY = ModelRef(provider="test", model="primary")
FALLBACK = ModelRef(provider="test", model="fallback")


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


class _ChainBackedProvider:
    """Bridges ``provider.chain.ProviderChain`` (``complete_step(request, *, run_id,
    turn_number)``) to the plain ``ModelProvider`` shape (``complete(request)``)
    ``run/decision_loop.py`` actually calls -- the composition this codebase does not wire up
    anywhere in ``src/`` yet (see this module's own docstring). Retry/fallback/exhaustion all
    happen inside ``complete_step`` before this ever returns or raises."""

    def __init__(self, chain: ProviderChain, *, run_id: RunId, turn_number: int, raw: Any) -> None:
        self._chain = chain
        self._run_id = run_id
        self._turn_number = turn_number
        self._raw = raw

    def describe(self, model: ModelRef) -> ModelCapabilities:
        return self._raw.describe(model)  # pragma: no cover - decision_loop never calls this

    def complete(self, request: DecisionRequest) -> DecisionResponse:
        return self._chain.complete_step(
            request, run_id=self._run_id, turn_number=self._turn_number
        )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def _end_turn_decision() -> RawDecision:
    return RawDecision(
        action_declaration_id=TICK_DECLARATION_ID,
        reasoning="one step, then end",
        parameters={},
        is_end_turn=True,
        prompt_type=None,
    )


def _build_runner(
    *, tmp_path: Path, store: SqliteMatchStore, raw_provider: FakeModelProvider
) -> Runner:
    """Turn 1 is served by a primary+fallback chain (a transient failure retries, then falls
    back); turn 2 is served by a single-model chain with no fallback at all, so its own transient
    failures exhaust the chain outright -- both driven by the *same* underlying
    ``FakeModelProvider``, scripted per turn by the test itself."""
    registry = _build_registry()
    host = FakeHostPlatform()
    host_info = HostInfo(
        os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
    )
    game = _FakeGame()

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
        if turn_number == 1:
            model_config = ModelConfig(primary=PRIMARY, fallbacks=[FALLBACK])
        else:
            model_config = ModelConfig(primary=PRIMARY)  # no fallback -- exhausts on failure

        chain = ProviderChain(
            raw_provider,
            model_config,
            event_sink=store,
            retry_policy=RetryPolicy(max_attempts_per_model=2, base_delay_s=0.0, jitter_s=0.0),
            sleep=lambda _delay: None,
            rand=lambda: 0.0,
        )
        provider = _ChainBackedProvider(
            chain, run_id=run_id, turn_number=turn_number, raw=raw_provider
        )

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
        return StopEvaluation(current_turn=turn_number)

    return Runner(
        RunnerDependencies(
            store=store,
            prepare_run=prepare_run,
            build_turn_dependencies=build_turn_dependencies,
            evaluate_stop_facts=evaluate_stop_facts,
        )
    )


async def _wait_until_terminal_or_stalled(
    runner: Runner, run_id: RunId, *, timeout_s: float = 5.0
) -> Any:
    """Wait for the run to reach a terminal ``lifecycle_state``, to ``pause`` (FR-042's chain
    -exhaustion outcome), or for the runner to have recorded a ``last_error`` (i.e. the play loop
    has stopped making progress even where it could not complete a lifecycle transition) -- so a
    caller can go on to inspect exactly how far the run got, rather than this helper masking a
    stall as an opaque timeout."""
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


def _queue_turn1_retry_then_fallback(raw_provider: FakeModelProvider) -> None:
    """Turn 1: primary fails twice (its retry budget), fallback serves the call."""
    raw_provider.queue_rate_limited()
    raw_provider.queue_rate_limited()
    raw_provider.queue_decision(_end_turn_decision(), model_served=FALLBACK)


def _queue_turn2_exhaustion(raw_provider: FakeModelProvider) -> None:
    """Turn 2: the lone model (no fallback configured) fails twice -- the chain exhausts."""
    raw_provider.queue_failed()
    raw_provider.queue_failed()


async def test_transient_failure_retries_then_falls_back_with_every_attempt_recorded(
    tmp_path: Path,
) -> None:
    """P5/FR-041: retry with backoff, then fall back -- every attempt against the chain (both
    failed primary attempts, the retry between them, and the move to the fallback model) is a
    recorded run event, and the persisted turn 1 record itself shows the fallback."""
    store = SqliteMatchStore(tmp_path / "match.db")
    raw_provider = FakeModelProvider()
    _queue_turn1_retry_then_fallback(raw_provider)
    _queue_turn2_exhaustion(raw_provider)  # turn 2 still runs; not this test's concern

    runner = _build_runner(tmp_path=tmp_path, store=store, raw_provider=raw_provider)
    try:
        run_id = runner.start(CONFIG_PATH)
        await _wait_until_terminal_or_stalled(runner, run_id)
    finally:
        store.close()

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        turn1_events = [
            e
            for e in store.list_run_events(
                run_id,
                event_types=[
                    RunEventType.PROVIDER_FAILURE,
                    RunEventType.PROVIDER_RETRY,
                    RunEventType.PROVIDER_FALLBACK,
                ],
            )
            if e.turn_number == 1
        ]
        by_type = {
            t: [e for e in turn1_events if e.event_type is t]
            for t in (
                RunEventType.PROVIDER_FAILURE,
                RunEventType.PROVIDER_RETRY,
                RunEventType.PROVIDER_FALLBACK,
            )
        }
        assert len(by_type[RunEventType.PROVIDER_FAILURE]) == 2  # both failed primary attempts
        assert len(by_type[RunEventType.PROVIDER_RETRY]) == 1  # the one retry between them
        assert len(by_type[RunEventType.PROVIDER_FALLBACK]) == 1  # the move to the fallback model

        turn1_record = store.get_turn_cycle(run_id, 1)
        assert turn1_record is not None
        assert turn1_record.turn_cycle.is_authoritative is True
        from civsim_harness.models.turn import TurnOutcome

        assert turn1_record.turn_cycle.outcome is TurnOutcome.ENDED_BY_AGENT
        model_call = turn1_record.steps[0].model_call
        assert model_call.fallback_occurred is True
        assert model_call.model_requested == PRIMARY
        assert model_call.model_served == FALLBACK
        assert model_call.retry_count == 2
    finally:
        store.close()


async def test_chain_exhaustion_produces_no_fabricated_turn_and_is_recorded_by_the_chain_itself(
    tmp_path: Path,
) -> None:
    """P6/FR-042/SC-012: once the (fallback-less, for turn 2) chain exhausts, no TurnCycle is
    ever fabricated for that turn, and the exhaustion itself is durably recorded by
    ``provider/chain.py`` -- independent of whatever ``run/runner.py`` goes on to do with the
    exception it raises (see the two defects the next test documents)."""
    store = SqliteMatchStore(tmp_path / "match.db")
    raw_provider = FakeModelProvider()
    _queue_turn1_retry_then_fallback(raw_provider)
    _queue_turn2_exhaustion(raw_provider)

    runner = _build_runner(tmp_path=tmp_path, store=store, raw_provider=raw_provider)
    try:
        run_id = runner.start(CONFIG_PATH)
        status = await _wait_until_terminal_or_stalled(runner, run_id)
    finally:
        store.close()

    assert status.last_error is not None
    assert status.last_error.type == "ProviderChainExhausted"

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        exhausted_events = store.list_run_events(
            run_id, event_types=[RunEventType.MODEL_CHAIN_EXHAUSTED]
        )
        assert len(exhausted_events) == 1
        assert exhausted_events[0].turn_number == 2
        assert exhausted_events[0].detail["chain"] == ["test/primary"]

        # Turn 2 never produced a TurnCycle record at all: a quicksave was taken (the turn
        # attempt began), but nothing was ever persisted to paper over the exhausted chain.
        assert store.get_turn_cycle(run_id, 2) is None
        save_points = {s.turn_number for s in store.list_save_points(run_id)}
        assert 2 in save_points  # the quicksave for turn 2 was taken before the loop ran

        # Nothing for turn 2 resembles a decision either -- the only events turn 2 produced are
        # the chain's own failure/exhaustion bookkeeping, never a fabricated decision payload.
        turn2_events = [e for e in store.list_run_events(run_id) if e.turn_number == 2]
        assert RunEventType.MODEL_CHAIN_EXHAUSTED in {e.event_type for e in turn2_events}
        for event in turn2_events:
            assert "action_declaration_id" not in event.detail

        # DEFECT 1 (found by this test, not asserted around): record_completeness_status
        # (src/civsim_harness/store/completeness.py:28-59) returns COMPLETE here, not HAS_GAPS,
        # even though turn 2 was genuinely attempted (it has a save point) and produced zero
        # TurnCycle rows at all. Root cause: store.turn_gaps (sqlite_adapter.py:753-764) derives
        # its own "highest recorded turn" purely from MAX(turn_number) in the turn_cycles table
        # (here, 1 -- turn 2 has no turn_cycle row, authoritative or otherwise), so it never
        # considers turn 2 part of its own gap range at all and returns []. completeness.py's
        # record_completeness_status calls store.turn_gaps(run_id) unscoped and only re-derives
        # a *wider* turn range (from list_save_points) for its step_gaps loop -- but step_gaps
        # itself explicitly returns [] for a turn with no authoritative attempt at all ("that
        # case is turn_gaps's to report, not this method's", per its own docstring). So a turn
        # that is the *last* one ever attempted, has a save point, and never got a single
        # TurnCycle written (exactly what chain exhaustion produces) falls through both checks
        # silently. Every existing tests/unit/test_completeness.py (T145) case only covers a gap
        # strictly *between* two turns that both have turn_cycle rows (e.g. turns 1 and 3
        # written, 2 missing) -- that shape is caught correctly, since turn_gaps's own
        # max(present) is pulled forward by the later turn. A *trailing* attempted-but-recordless
        # turn, as produced here, is not covered by any existing test and is not detected.
        assert record_completeness_status(store, run_id) is RecordCompletenessStatus.HAS_GAPS
    finally:
        store.close()


async def test_chain_exhaustion_pauses_the_run_in_a_recorded_paused_state(tmp_path: Path) -> None:
    """FR-042 verbatim: "When no decision can be obtained for a turn, the harness MUST **pause**
    the run in a recorded state. It MUST NOT fabricate an action, skip the turn, or substitute a
    default or heuristic move." The target lifecycle state for chain exhaustion is ``paused``
    (data-model.md SS4's ``playing -> paused`` edge, a legal transition) -- ``failed`` is reserved
    for FR-048's *recovery* limit (reached from ``resuming``, never from ``playing`` directly)."""
    store = SqliteMatchStore(tmp_path / "match.db")
    raw_provider = FakeModelProvider()
    _queue_turn1_retry_then_fallback(raw_provider)
    _queue_turn2_exhaustion(raw_provider)

    runner = _build_runner(tmp_path=tmp_path, store=store, raw_provider=raw_provider)
    try:
        run_id = runner.start(CONFIG_PATH)
        status = await _wait_until_terminal_or_stalled(runner, run_id)
    finally:
        store.close()

    # DEFECT 2 (found by this test, not asserted around; confirmed against data-model.md SS4):
    # ``Runner._fail`` (src/civsim_harness/run/runner.py:360-381) calls
    # ``transition(state.run, LifecycleState.FAILED, ...)`` whenever a HarnessError (here,
    # ``ProviderChainExhausted``) propagates out of ``run_turn_cycle`` while the run is still
    # ``playing`` -- the normal case, since nothing moves the run out of ``playing`` before the
    # exception is raised. Two things are wrong with this, stacked: (1) FR-042 requires chain
    # exhaustion to **pause** the run, not fail it -- ``failed`` is FR-048's *recovery-limit*
    # outcome, reached only from ``resuming``; and (2) even setting that aside,
    # ``run/lifecycle.py``'s own transition graph (``LEGAL_TRANSITIONS``, lines 56-74) does not
    # permit ``playing -> failed`` at all -- only ``preparing -> failed`` and
    # ``resuming -> failed`` are legal edges, so ``transition()`` itself raises a *new*
    # HarnessError from inside ``_fail`` (uncaught there). That second exception propagates out
    # of the ``except HarnessError`` handler in ``_play_run`` (runner.py:339-340) -- a coroutine
    # scheduled fire-and-forget via ``asyncio.run_coroutine_threadsafe`` (``_schedule``,
    # runner.py:401-402) whose Future result is never retrieved, so it is silently swallowed. Net
    # effect: the run is stuck in ``playing`` forever, with no recorded terminal or paused state
    # at all -- neither ``get_status`` nor the persisted ``Run`` ever reports anything went wrong.
    # This reproduces for *any* HarnessError raised mid-turn while playing, not just chain
    # exhaustion -- e.g. ``UnknownScreenEncountered`` (FR-049) would hit the same illegal
    # transition (though routed to the correct target state for *that* condition is a separate
    # question this test does not adjudicate).
    assert status.lifecycle_state is LifecycleState.PAUSED

    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        persisted = store.get_run(run_id)
        assert persisted is not None
        assert persisted.lifecycle_state is LifecycleState.PAUSED
        # paused is not a terminal state (models.run.Run's own invariant): no stop_resolution is
        # recorded here, unlike finished/failed -- the run is stopped, not concluded.
        assert persisted.stop_resolution is None

        # "in a recorded state" (FR-042) -- the pause itself is a durable lifecycle_transition
        # event, not merely an in-memory status flag.
        transitions = store.list_run_events(run_id, event_types=[RunEventType.LIFECYCLE_TRANSITION])
        paused_transitions = [e for e in transitions if e.detail.get("to") == "paused"]
        assert len(paused_transitions) == 1
        assert paused_transitions[0].turn_number == 2
    finally:
        store.close()
