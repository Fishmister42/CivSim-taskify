"""Unit tests for crash/hang/unresponsiveness detection and recovery (T144).

Covers the four independent signals research R12 names -- process liveness
(T147), the tuner heartbeat (T148), per-operation bounds (T149), and the
screen-identity probe, aggregated by the detector (T150) -- and the
recovery engine built on top of them (T151, T153, T154).

**The centerpiece assertion (T144): no signal may key off elapsed turn
time.** `test_long_productive_turn_trips_nothing` scripts a turn running
across hundreds of decision steps and several *simulated* hours of
wall-clock, with every signal reporting healthy throughout, and asserts
zero detection events are produced -- a long turn is not evidence of a
fault (SC-010, FR-014, research R12). A second test asserts the removed
`stall` event type is gone from the model for good.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from civsim_harness.errors import NexusError, ObservationAssemblyError, RecoveryLimitReached
from civsim_harness.models.common import (
    CapturePath,
    CatalogVersionRef,
    ConfigId,
    RunId,
    SavePointId,
)
from civsim_harness.models.records import RetentionStatus, RunEvent, RunEventType, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
    StopResolution,
)
from civsim_harness.nexus.client import REASON_CONNECTION_CLOSED, REASON_TIMEOUT, StateIndices
from civsim_harness.resilience.detector import (
    DEFAULT_RECONNECT_REPROBE_DELAY_S,
    DetectionAggregator,
    check_heartbeat,
    check_operation_bound,
    check_process_liveness,
    check_screen_identity,
)
from civsim_harness.resilience.heartbeat_monitor import (
    DEFAULT_HEARTBEAT_TIMEOUT_S,
    HeartbeatMonitor,
    HeartbeatOutcome,
)
from civsim_harness.resilience.liveness import ProcessLivenessMonitor, is_process_alive
from civsim_harness.resilience.operation_bounds import (
    DEFAULT_OPERATION_BOUNDS_S,
    OperationKind,
    OperationTimedOut,
    run_bounded,
)
from civsim_harness.resilience.recovery import RecoveryEngine

_T0 = datetime(2026, 9, 20, 8, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Shared test doubles
# --------------------------------------------------------------------------


class _FakeHeartbeatClient:
    """Duck-typed stand-in for `NexusClient`: only what `probe_heartbeat` touches."""

    def __init__(self, *, outcome: Exception | bool) -> None:
        self.state_indices = StateIndices(
            by_name={"GameCore_Tuner": 0, "InGame": 1}, game_core_tuner=0, in_game=1
        )
        self._outcome = outcome
        self.calls = 0

    async def execute_command(
        self, *, state_index: int, lua_body: str, timeout_s: float | None = None
    ) -> Any:
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _run(lifecycle_state: LifecycleState, **overrides: object) -> Run:
    fields: dict[str, object] = {
        "run_id": RunId("run-1"),
        "config_id": ConfigId("cfg-1"),
        "lifecycle_state": lifecycle_state,
        "record_completeness_status": RecordCompletenessStatus.UNKNOWN,
        "comparability_status": ComparabilityStatus.COMPARABLE,
        "observation_catalog_version": CatalogVersionRef(version="1.0.0", content_hash="obs-hash"),
        "action_catalog_version": CatalogVersionRef(version="1.0.0", content_hash="act-hash"),
        "game_build": "win/1.0.12.9",
        "host_support_tier": HostSupportTier.VALIDATED,
        "capture_path": CapturePath.NONE,
    }
    fields.update(overrides)
    return Run.model_validate(fields)


def _save(turn_number: int, *, retained: bool = True) -> SavePoint:
    return SavePoint(
        save_point_id=SavePointId(f"sp-{turn_number}"),
        run_id=RunId("run-1"),
        turn_number=turn_number,
        save_name=f"civsim__run-1__t{turn_number:04d}",
        taken_at=_T0,
        verified=True,
        retention_status=RetentionStatus.RETAINED if retained else RetentionStatus.REMOVED,
    )


@dataclass
class _FakeStore:
    """Minimal duck-typed `MatchStore`: only the four operations recovery.py calls."""

    run: Run
    last_known_good: SavePoint | None = None
    events: list[RunEvent] = field(default_factory=list)

    def write_run_event(self, event: RunEvent) -> Any:
        self.events.append(event)
        return event.event_id

    def update_run(self, run_id: RunId, **fields: Any) -> None:
        merged = self.run.model_dump()
        merged.update(fields)
        self.run = Run.model_validate(merged)

    def get_run(self, run_id: RunId) -> Run | None:
        return self.run

    def get_last_known_good(self, run_id: RunId) -> SavePoint | None:
        return self.last_known_good

    def event_types(self) -> list[RunEventType]:
        return [e.event_type for e in self.events]


@dataclass
class _FakeLoader:
    """Fails its first `fail_times` calls, then succeeds."""

    fail_times: int = 0
    calls: int = 0
    loaded: list[SavePoint] = field(default_factory=list)

    async def load(self, save: SavePoint) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"load attempt {self.calls} failed")
        self.loaded.append(save)


class _ScreenProbe:
    def __init__(self, *, known: bool) -> None:
        self.known = known
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self.known


@dataclass
class _FlakyHeartbeatClient:
    """Duck-typed `NexusClient` stand-in that raises *outcome* on its first `fail_times` calls,
    then reports a healthy round-trip -- for the heartbeat's connection-refusal re-probe path
    (mirrors `_FakeLoader`'s own fail-then-succeed shape, used below for recovery)."""

    outcome: Exception
    fail_times: int = 1
    calls: int = 0
    state_indices: StateIndices = field(
        default_factory=lambda: StateIndices(
            by_name={"GameCore_Tuner": 0, "InGame": 1}, game_core_tuner=0, in_game=1
        )
    )

    async def execute_command(
        self, *, state_index: int, lua_body: str, timeout_s: float | None = None
    ) -> Any:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.outcome
        return True


async def _instant_sleep(_delay_s: float) -> None:
    """Stand-in for `asyncio.sleep` so the re-probe corroboration tests below run instantly
    instead of actually pausing `reprobe_delay_s` -- the delay value itself is exercised
    separately (`test_default_reconnect_reprobe_delay_fits_well_inside_the_60s_budget`)."""
    return None


# --------------------------------------------------------------------------
# T147 -- process liveness
# --------------------------------------------------------------------------


def test_is_process_alive_true_for_the_current_process() -> None:
    import os

    assert is_process_alive(os.getpid()) is True


def test_is_process_alive_false_for_a_pid_that_does_not_exist() -> None:
    import psutil

    # Find a PID guaranteed not to exist right now.
    dead_pid = 1
    while psutil.pid_exists(dead_pid):
        dead_pid += 1
    assert is_process_alive(dead_pid) is False


def test_is_process_alive_false_for_a_zombie(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zombie is present (`pid_exists`) but not doing anything -- not alive."""
    import psutil

    class _ZombieProcess:
        def status(self) -> str:
            return psutil.STATUS_ZOMBIE

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(psutil, "Process", lambda pid: _ZombieProcess())
    assert is_process_alive(4242) is False


def test_process_liveness_monitor_is_a_stateless_point_in_time_check() -> None:
    import os

    monitor = ProcessLivenessMonitor(pid=os.getpid())
    assert monitor.check() is True
    assert monitor.check() is True  # repeated calls are independent, not cached


# --------------------------------------------------------------------------
# T148 -- heartbeat monitoring
# --------------------------------------------------------------------------


async def test_heartbeat_monitor_ok_on_successful_round_trip() -> None:
    client = _FakeHeartbeatClient(outcome=True)
    monitor = HeartbeatMonitor(client=client)  # type: ignore[arg-type]
    assert await monitor.check() is HeartbeatOutcome.OK


async def test_heartbeat_monitor_hang_on_timeout() -> None:
    client = _FakeHeartbeatClient(
        outcome=NexusError("timed out", detail={"reason": REASON_TIMEOUT})
    )
    monitor = HeartbeatMonitor(client=client)  # type: ignore[arg-type]
    assert await monitor.check() is HeartbeatOutcome.HANG


async def test_heartbeat_monitor_connection_lost_is_distinct_from_hang() -> None:
    """research R12 files 'dropped tuner connection' under the heartbeat signal too,
    but this module keeps the sub-cause visible rather than collapsing it."""
    client = _FakeHeartbeatClient(
        outcome=NexusError("closed", detail={"reason": REASON_CONNECTION_CLOSED})
    )
    monitor = HeartbeatMonitor(client=client)  # type: ignore[arg-type]
    assert await monitor.check() is HeartbeatOutcome.CONNECTION_LOST


# --------------------------------------------------------------------------
# T149 -- per-operation bounds
# --------------------------------------------------------------------------


async def test_run_bounded_returns_the_result_on_success() -> None:
    async def op() -> str:
        return "ok"

    result = await run_bounded(op, kind=OperationKind.NEXUS_COMMAND, bound_s=1.0)
    assert result == "ok"


async def test_run_bounded_reports_timeout_via_asyncio_wait_for() -> None:
    import asyncio

    async def slow() -> str:
        await asyncio.sleep(0.5)
        return "too slow"

    result = await run_bounded(slow, kind=OperationKind.CAPTURE, bound_s=0.01)
    assert isinstance(result, OperationTimedOut)
    assert result.kind is OperationKind.CAPTURE
    assert result.bound_s == 0.01


async def test_run_bounded_reports_timeout_from_a_nexus_error_reason() -> None:
    async def op() -> str:
        raise NexusError("cmd timed out", detail={"reason": REASON_TIMEOUT})

    result = await run_bounded(op, kind=OperationKind.POST_ACTION_READBACK, bound_s=5.0)
    assert isinstance(result, OperationTimedOut)
    assert result.kind is OperationKind.POST_ACTION_READBACK


async def test_run_bounded_propagates_non_timeout_nexus_errors() -> None:
    async def op() -> str:
        raise NexusError("closed", detail={"reason": REASON_CONNECTION_CLOSED})

    with pytest.raises(NexusError):
        await run_bounded(op, kind=OperationKind.NEXUS_COMMAND, bound_s=5.0)


def test_every_operation_kind_has_a_default_bound_within_the_60s_budget() -> None:
    for kind in OperationKind:
        assert kind in DEFAULT_OPERATION_BOUNDS_S
        assert 0 < DEFAULT_OPERATION_BOUNDS_S[kind] <= 60.0


# --------------------------------------------------------------------------
# T150 -- the detection aggregator, and the four event types
# --------------------------------------------------------------------------


def test_check_process_liveness_trips_crash_detected() -> None:
    import psutil

    dead_pid = 1
    while psutil.pid_exists(dead_pid):
        dead_pid += 1
    event = check_process_liveness(
        ProcessLivenessMonitor(pid=dead_pid), run_id=RunId("run-1"), occurred_at=_T0
    )
    assert event is not None
    assert event.event_type is RunEventType.CRASH_DETECTED
    assert event.detail["pid"] == dead_pid


def test_check_process_liveness_healthy_trips_nothing() -> None:
    import os

    event = check_process_liveness(
        ProcessLivenessMonitor(pid=os.getpid()), run_id=RunId("run-1"), occurred_at=_T0
    )
    assert event is None


async def test_check_heartbeat_trips_hang_detected() -> None:
    client = _FakeHeartbeatClient(
        outcome=NexusError("timed out", detail={"reason": REASON_TIMEOUT})
    )
    event = await check_heartbeat(
        HeartbeatMonitor(client=client),  # type: ignore[arg-type]
        run_id=RunId("run-1"),
        occurred_at=_T0,
    )
    assert event is not None
    assert event.event_type is RunEventType.HANG_DETECTED


async def test_check_heartbeat_healthy_trips_nothing() -> None:
    client = _FakeHeartbeatClient(outcome=True)
    event = await check_heartbeat(
        HeartbeatMonitor(client=client),  # type: ignore[arg-type]
        run_id=RunId("run-1"),
        occurred_at=_T0,
    )
    assert event is None


# --------------------------------------------------------------------------
# Defect fix: a connection refusal is corroborated before it is ever
# reported as crash_detected -- Civ VI accepts one tuner client at a time
# and can transiently refuse a rapid reconnect while a healthy client is
# still releasing the previous socket (contracts/nexus-protocol.md). This
# is the exact shape found against a live client: a naive detector would
# have manufactured crash_detected on a perfectly healthy game.
# --------------------------------------------------------------------------


async def test_connection_refusal_with_pid_alive_does_not_produce_crash_detected() -> None:
    """A dropped/refused connection that is still down after one re-probe, but whose PID is
    still alive, is an honest 'unresponsive', never a fabricated crash (process liveness is the
    strongest signal -- FR-set behind this fix)."""
    import os

    client = _FakeHeartbeatClient(
        outcome=NexusError("closed", detail={"reason": REASON_CONNECTION_CLOSED})
    )
    event = await check_heartbeat(
        HeartbeatMonitor(client=client),  # type: ignore[arg-type]
        run_id=RunId("run-1"),
        occurred_at=_T0,
        liveness=ProcessLivenessMonitor(pid=os.getpid()),
        sleep=_instant_sleep,
    )
    assert event is not None
    assert event.event_type is not RunEventType.CRASH_DETECTED
    assert event.event_type is RunEventType.UNRESPONSIVE_DETECTED
    assert event.detail["reprobed"] is True
    # exactly one initial reading plus exactly one bounded re-probe -- not an open-ended retry loop
    assert client.calls == 2


async def test_connection_refusal_persisting_with_pid_gone_produces_crash_detected() -> None:
    """A connection refusal that is *still* unreachable after the re-probe, corroborated by a
    PID that is genuinely gone, is exactly what crash_detected exists for -- this fix must not
    swallow a real crash while it stops fabricating one."""
    import psutil

    dead_pid = 1
    while psutil.pid_exists(dead_pid):
        dead_pid += 1
    client = _FakeHeartbeatClient(
        outcome=NexusError("closed", detail={"reason": REASON_CONNECTION_CLOSED})
    )
    event = await check_heartbeat(
        HeartbeatMonitor(client=client),  # type: ignore[arg-type]
        run_id=RunId("run-1"),
        occurred_at=_T0,
        liveness=ProcessLivenessMonitor(pid=dead_pid),
        sleep=_instant_sleep,
    )
    assert event is not None
    assert event.event_type is RunEventType.CRASH_DETECTED
    assert event.detail["pid"] == dead_pid
    assert client.calls == 2

    # SC-010's 60s budget still comfortably holds: two heartbeat-timeout-bounded reads plus one
    # fixed re-probe pause is a small, fixed fraction of the full budget, never a wall-clock
    # deadline of the detector's own.
    worst_case_s = 2 * DEFAULT_HEARTBEAT_TIMEOUT_S + DEFAULT_RECONNECT_REPROBE_DELAY_S
    assert worst_case_s < 60.0


async def test_single_transient_connection_refusal_that_recovers_trips_no_event_at_all() -> None:
    """The first reading is refused, the re-probe succeeds -- this was never a fault, and there
    is no event of any kind, not even a downgraded one."""
    client = _FlakyHeartbeatClient(
        outcome=NexusError("closed", detail={"reason": REASON_CONNECTION_CLOSED}), fail_times=1
    )
    event = await check_heartbeat(
        HeartbeatMonitor(client=client),  # type: ignore[arg-type]
        run_id=RunId("run-1"),
        occurred_at=_T0,
        sleep=_instant_sleep,
    )
    assert event is None
    assert client.calls == 2


async def test_default_reconnect_reprobe_delay_fits_well_inside_the_60s_budget() -> None:
    """The re-probe pause itself, plus the two heartbeat reads it straddles, must leave generous
    room inside SC-010's 60s crash/hang/unresponsiveness budget."""
    assert 0 < DEFAULT_RECONNECT_REPROBE_DELAY_S <= 5.0
    assert 2 * DEFAULT_HEARTBEAT_TIMEOUT_S + DEFAULT_RECONNECT_REPROBE_DELAY_S < 60.0


async def test_detection_aggregator_corroborates_a_dropped_connection_with_its_own_liveness() -> (
    None
):
    """`DetectionAggregator.check_once` wires its own liveness monitor into the heartbeat check
    automatically -- a caller that has both signals gets the corroborated classification without
    doing anything extra."""
    import os

    client = _FakeHeartbeatClient(
        outcome=NexusError("closed", detail={"reason": REASON_CONNECTION_CLOSED})
    )
    aggregator = DetectionAggregator(
        liveness=ProcessLivenessMonitor(pid=os.getpid()),
        heartbeat=HeartbeatMonitor(client=client),  # type: ignore[arg-type]
        sleep=_instant_sleep,
    )
    events = await aggregator.check_once(run_id=RunId("run-1"), occurred_at=_T0)
    assert [e.event_type for e in events] == [RunEventType.UNRESPONSIVE_DETECTED]


async def test_check_operation_bound_trips_unresponsive_detected() -> None:
    async def op() -> str:
        raise NexusError("timed out", detail={"reason": REASON_TIMEOUT})

    result, event = await check_operation_bound(
        op,
        kind=OperationKind.NEXUS_COMMAND,
        run_id=RunId("run-1"),
        occurred_at=_T0,
        bound_s=1.0,
    )
    assert result is None
    assert event is not None
    assert event.event_type is RunEventType.UNRESPONSIVE_DETECTED
    assert event.detail["operation_kind"] == OperationKind.NEXUS_COMMAND.value


async def test_check_operation_bound_healthy_trips_nothing() -> None:
    async def op() -> int:
        return 7

    result, event = await check_operation_bound(
        op, kind=OperationKind.CAPTURE, run_id=RunId("run-1"), occurred_at=_T0
    )
    assert result == 7
    assert event is None


async def test_check_screen_identity_trips_unknown_screen() -> None:
    event = await check_screen_identity(
        _ScreenProbe(known=False), run_id=RunId("run-1"), occurred_at=_T0
    )
    assert event is not None
    assert event.event_type is RunEventType.UNKNOWN_SCREEN


async def test_check_screen_identity_known_screen_trips_nothing() -> None:
    event = await check_screen_identity(
        _ScreenProbe(known=True), run_id=RunId("run-1"), occurred_at=_T0
    )
    assert event is None


async def test_detection_aggregator_combines_only_the_signals_it_was_given() -> None:
    import os

    aggregator = DetectionAggregator(
        liveness=ProcessLivenessMonitor(pid=os.getpid()),
        heartbeat=None,
        screen_identity_probe=_ScreenProbe(known=False),
    )
    events = await aggregator.check_once(run_id=RunId("run-1"), occurred_at=_T0)
    assert [e.event_type for e in events] == [RunEventType.UNKNOWN_SCREEN]


async def test_detection_aggregator_reports_every_signal_that_tripped() -> None:
    import psutil

    dead_pid = 1
    while psutil.pid_exists(dead_pid):
        dead_pid += 1
    client = _FakeHeartbeatClient(
        outcome=NexusError("timed out", detail={"reason": REASON_TIMEOUT})
    )
    aggregator = DetectionAggregator(
        liveness=ProcessLivenessMonitor(pid=dead_pid),
        heartbeat=HeartbeatMonitor(client=client),  # type: ignore[arg-type]
        screen_identity_probe=_ScreenProbe(known=True),
    )
    events = await aggregator.check_once(run_id=RunId("run-1"), occurred_at=_T0)
    assert {e.event_type for e in events} == {
        RunEventType.CRASH_DETECTED,
        RunEventType.HANG_DETECTED,
    }


async def test_detection_aggregator_healthy_trips_nothing() -> None:
    import os

    aggregator = DetectionAggregator(
        liveness=ProcessLivenessMonitor(pid=os.getpid()),
        heartbeat=HeartbeatMonitor(client=_FakeHeartbeatClient(outcome=True)),  # type: ignore[arg-type]
        screen_identity_probe=_ScreenProbe(known=True),
    )
    events = await aggregator.check_once(run_id=RunId("run-1"), occurred_at=_T0)
    assert events == []


def test_stall_event_type_is_gone_for_good() -> None:
    """The removed `stall` outcome must never reappear (data-model.md SS14)."""
    values = {member.value for member in RunEventType}
    assert "stall" not in values
    assert not hasattr(RunEventType, "STALL")


# --------------------------------------------------------------------------
# T144 -- the centerpiece: no signal keys off elapsed turn time
# --------------------------------------------------------------------------


async def test_long_productive_turn_trips_nothing() -> None:
    """Scripts several simulated hours across hundreds of decision steps, all
    signals healthy throughout, and asserts detection stays silent the whole
    way -- a long turn is not evidence of a fault (SC-010, FR-014, R12).
    """
    import os

    liveness = ProcessLivenessMonitor(pid=os.getpid())
    heartbeat = HeartbeatMonitor(client=_FakeHeartbeatClient(outcome=True))  # type: ignore[arg-type]
    screen = _ScreenProbe(known=True)
    aggregator = DetectionAggregator(
        liveness=liveness, heartbeat=heartbeat, screen_identity_probe=screen  # type: ignore[arg-type]
    )

    all_events: list[RunEvent] = []
    step_count = 400
    for step in range(step_count):
        # Each step's own timestamp is hours after the last -- an
        # deliberately unrealistic wall-clock spread that would trip any
        # signal secretly keying off elapsed time (a turn timer, a step
        # duration ceiling, anything of that shape).
        occurred_at = _T0 + timedelta(hours=3 * step)
        # Each decision step's own operation is bounded individually and
        # comfortably within its bound -- also never a fault.
        async def fast_op(step: int = step) -> int:
            return step

        result, op_event = await check_operation_bound(
            fast_op,
            kind=OperationKind.NEXUS_COMMAND,
            run_id=RunId("run-1"),
            occurred_at=occurred_at,
        )
        assert result == step
        assert op_event is None
        all_events.extend(
            await aggregator.check_once(
                run_id=RunId("run-1"), occurred_at=occurred_at, step_index=step
            )
        )

    assert all_events == []
    # The simulated turn ran for 1200 hours of "wall clock" and hundreds of
    # steps -- confirming the emptiness above is not a coincidence of a
    # short test, but genuinely independent of both axes.
    final_occurred_at = _T0 + timedelta(hours=3 * (step_count - 1))
    assert (final_occurred_at - _T0) > timedelta(hours=1000)


def test_no_detection_function_takes_a_duration_or_elapsed_time_argument() -> None:
    """Structural guard: none of the public check functions accept anything
    shaped like 'how long has this been going' (research R12's own framing
    of the rule this task exists to enforce)."""
    import inspect

    from civsim_harness.resilience import detector

    banned_substrings = ("elapsed", "duration", "turn_started", "since", "budget_s", "deadline")
    for name in (
        "check_process_liveness",
        "check_heartbeat",
        "check_operation_bound",
        "check_screen_identity",
    ):
        func = getattr(detector, name)
        params = set(inspect.signature(func).parameters)
        for banned in banned_substrings:
            assert not any(banned in p for p in params), f"{name} has a time-budget-shaped param"


# --------------------------------------------------------------------------
# T151, T153, T154 -- recovery
# --------------------------------------------------------------------------


async def test_recover_abandons_and_resumes_as_the_same_continuous_run() -> None:
    run = _run(LifecycleState.PLAYING)
    store = _FakeStore(run=run)
    loader = _FakeLoader()
    engine = RecoveryEngine(
        run_id=run.run_id, store=store, loader=loader, recovery_attempt_limit=3
    )
    save = _save(5)

    result = await engine.recover(
        run,
        turn_number=5,
        turn_start_save=save,
        trigger_event_type=RunEventType.CRASH_DETECTED,
        trigger_detail={"pid": 4242},
    )

    assert result.run.lifecycle_state is LifecycleState.PLAYING
    assert result.resumed_from is save
    assert loader.calls == 1
    assert loader.loaded == [save]
    assert engine.consecutive_failures == 0

    # Nothing pre-interruption leaks into the result (FR-046).
    assert not hasattr(result, "observation")
    assert not hasattr(result, "capture")

    event_types = store.event_types()
    assert RunEventType.TURN_ABANDONED in event_types
    assert RunEventType.RESUMED in event_types
    # interrupted, resuming, playing
    assert event_types.count(RunEventType.LIFECYCLE_TRANSITION) == 3
    abandoned = next(e for e in store.events if e.event_type is RunEventType.TURN_ABANDONED)
    assert abandoned.detail["trigger"] == RunEventType.CRASH_DETECTED.value
    assert abandoned.detail["pid"] == 4242


async def test_recover_rejects_a_save_from_the_wrong_turn() -> None:
    run = _run(LifecycleState.PLAYING)
    store = _FakeStore(run=run)
    engine = RecoveryEngine(
        run_id=run.run_id, store=store, loader=_FakeLoader(), recovery_attempt_limit=3
    )
    with pytest.raises(ValueError):
        await engine.recover(
            run,
            turn_number=5,
            turn_start_save=_save(4),
            trigger_event_type=RunEventType.CRASH_DETECTED,
        )


async def test_recover_from_observation_assembly_error_uses_the_same_path() -> None:
    """T153: an ObservationAssemblyError takes the same abandon-and-replay path as a crash."""
    run = _run(LifecycleState.PLAYING)
    store = _FakeStore(run=run)
    loader = _FakeLoader()
    engine = RecoveryEngine(
        run_id=run.run_id, store=store, loader=loader, recovery_attempt_limit=3
    )
    save = _save(2)
    error = ObservationAssemblyError("stale board mid-turn", detail={"step_index": 4})

    result = await engine.recover_from_observation_assembly_error(
        run, turn_number=2, turn_start_save=save, error=error
    )

    assert result.run.lifecycle_state is LifecycleState.PLAYING
    assert loader.calls == 1
    event_types = store.event_types()
    assert RunEventType.OBSERVATION_ASSEMBLY_FAILED in event_types
    assert RunEventType.TURN_ABANDONED in event_types
    obs_event = next(
        e for e in store.events if e.event_type is RunEventType.OBSERVATION_ASSEMBLY_FAILED
    )
    assert obs_event.detail["step_index"] == 4


async def test_a_failed_attempt_is_retryable_without_repeating_the_abandon_step() -> None:
    run = _run(LifecycleState.PLAYING)
    store = _FakeStore(run=run)
    loader = _FakeLoader(fail_times=1)
    engine = RecoveryEngine(
        run_id=run.run_id, store=store, loader=loader, recovery_attempt_limit=3
    )
    save = _save(7)

    with pytest.raises(RuntimeError):
        await engine.recover(
            run, turn_number=7, turn_start_save=save, trigger_event_type=RunEventType.HANG_DETECTED
        )
    assert engine.consecutive_failures == 1
    # The failed attempt still leaves the run at `resuming`, not stuck at
    # `playing` -- FR-048's bound is meaningful only if a failed attempt is
    # visibly not "back to normal".
    assert store.run.lifecycle_state is LifecycleState.RESUMING
    abandon_count_after_first_failure = store.event_types().count(RunEventType.TURN_ABANDONED)
    assert abandon_count_after_first_failure == 1

    # Retry with the now-current (resuming) run snapshot -- succeeds, and
    # does not re-abandon a turn that was already abandoned.
    result = await engine.recover(
        store.run,
        turn_number=7,
        turn_start_save=save,
        trigger_event_type=RunEventType.HANG_DETECTED,
    )
    assert result.run.lifecycle_state is LifecycleState.PLAYING
    assert engine.consecutive_failures == 0
    assert store.event_types().count(RunEventType.TURN_ABANDONED) == 1


async def test_recovery_limit_reached_stops_the_run_failed_and_names_last_known_good() -> None:
    """T154: after recovery_attempt_limit consecutive failures, stop in a
    recorded failed state identifying the last-known-good save -- no
    indefinite retry loop (FR-048, SC-021)."""
    run = _run(LifecycleState.PLAYING)
    last_known_good = _save(3)
    store = _FakeStore(run=run, last_known_good=last_known_good)
    loader = _FakeLoader(fail_times=1000)  # never succeeds
    engine = RecoveryEngine(
        run_id=run.run_id, store=store, loader=loader, recovery_attempt_limit=2
    )
    save = _save(9)

    with pytest.raises(RuntimeError):
        await engine.recover(
            run,
            turn_number=9,
            turn_start_save=save,
            trigger_event_type=RunEventType.UNRESPONSIVE_DETECTED,
        )
    assert engine.consecutive_failures == 1
    assert store.run.lifecycle_state is LifecycleState.RESUMING

    with pytest.raises(RecoveryLimitReached) as excinfo:
        await engine.recover(
            store.run,
            turn_number=9,
            turn_start_save=save,
            trigger_event_type=RunEventType.UNRESPONSIVE_DETECTED,
        )
    assert engine.consecutive_failures == 2

    assert store.run.lifecycle_state is LifecycleState.FAILED
    assert store.run.stop_resolution is StopResolution.UNRECOVERABLE_FAILURE
    assert excinfo.value.detail["last_known_good_save_point_id"] == last_known_good.save_point_id

    limit_event = next(
        e for e in store.events if e.event_type is RunEventType.RECOVERY_LIMIT_REACHED
    )
    assert limit_event.detail["last_known_good_save_point_id"] == last_known_good.save_point_id
    assert limit_event.detail["consecutive_failures"] == 2

    # No indefinite retry loop: the run is now terminal, and a further
    # recovery attempt against it is illegal (there is nowhere left for it
    # to legally transition to), rather than looping forever.
    from civsim_harness.errors import HarnessError

    with pytest.raises(HarnessError):
        await engine.recover(
            store.run,
            turn_number=9,
            turn_start_save=save,
            trigger_event_type=RunEventType.UNRESPONSIVE_DETECTED,
        )


def test_recovery_engine_rejects_a_non_positive_attempt_limit() -> None:
    run = _run(LifecycleState.PLAYING)
    store = _FakeStore(run=run)
    with pytest.raises(ValueError):
        RecoveryEngine(
            run_id=run.run_id, store=store, loader=_FakeLoader(), recovery_attempt_limit=0
        )


# --------------------------------------------------------------------------
# T247 -- a zombie tuner (dead-forever port, live process) must be detectable.
#
# A refused Network.HostGame leaves the client alive and painting frames with a
# permanently dead tuner port (live finding, reproduced twice, 2026-09-20 issue
# #1). Process/window liveness see health forever; a single timed-out detection
# pass cannot tell a dead-forever port from a busy client (FR-014). The fix:
# DetectionWatch counts *consecutive* eaten passes and, once the streak reaches
# its threshold, classifies the pattern as unresponsive and routes it to
# recovery. One eaten pass still reports nothing; only the sustained pattern
# trips. The revert (drop the streak logic) is confirmed below to never detect
# the zombie within the SC-010 window in the test clock.
# --------------------------------------------------------------------------


class _NeverAnsweringAggregator:
    """Stands in for a `DetectionAggregator` whose pass never completes -- the zombie tuner whose
    heartbeat probe hangs on a dead-forever port, so `run_bounded` eats every pass."""

    def __init__(self) -> None:
        self.calls = 0

    async def check_once(
        self,
        *,
        run_id: RunId,
        occurred_at: Any,
        turn_number: int | None = None,
        step_index: int | None = None,
    ) -> list[RunEvent]:
        import asyncio

        self.calls += 1
        await asyncio.sleep(3600)  # never returns within any sane pass bound
        return []  # pragma: no cover - unreachable


@dataclass
class _FlakyAggregator:
    """Hangs on the listed 1-based call ordinals, completes healthily on the rest -- a client that
    is busy on some passes but services the watchdog on others (never the zombie)."""

    hang_calls: set[int]
    calls: int = 0

    async def check_once(
        self,
        *,
        run_id: RunId,
        occurred_at: Any,
        turn_number: int | None = None,
        step_index: int | None = None,
    ) -> list[RunEvent]:
        import asyncio

        self.calls += 1
        if self.calls in self.hang_calls:
            await asyncio.sleep(3600)
        return []


def _zombie_watch(store: _FakeStore, aggregator: Any, *, threshold: int = 2) -> Any:
    from civsim_harness.run.detection import DetectionWatch

    return DetectionWatch(
        aggregator=aggregator,
        store=store,  # type: ignore[arg-type]
        run_id=RunId("run-1"),
        clock=lambda: _T0,
        liveness=None,  # healthy: no liveness trip, so every pass reaches the aggregate pass
        pass_bound_s=0.02,  # tiny, so the never-answering pass is eaten near-instantly in test
        sustained_failure_threshold=threshold,
    )


async def test_a_single_eaten_detection_pass_is_not_a_fault() -> None:
    """FR-014: one pass that exceeds its own bound is a busy client, not a faulty one -- nothing."""
    store = _FakeStore(run=_run(LifecycleState.PLAYING))
    watch = _zombie_watch(store, _NeverAnsweringAggregator(), threshold=2)

    events = await watch.check_now(turn_number=1)

    assert events == ()
    assert store.events == []  # nothing recorded, nothing to route


async def test_sustained_eaten_passes_trip_unresponsive_and_route_to_recovery() -> None:
    """N consecutive eaten passes are the zombie; they trip unresponsive and route into recovery.

    Reverting `DetectionWatch._on_pass_timed_out` to a bare `return ()` (the pre-T247 behaviour)
    makes the second assertion fail: the zombie is never detected within the SC-010 window.
    """
    from civsim_harness.run.detection import primary_fault

    store = _FakeStore(run=_run(LifecycleState.PLAYING))
    watch = _zombie_watch(store, _NeverAnsweringAggregator(), threshold=2)

    first = await watch.check_now(turn_number=1)
    assert first == ()  # one eaten pass: still nothing (FR-014)

    second = await watch.check_now(turn_number=1)
    assert len(second) == 1
    event = second[0]
    assert event.event_type is RunEventType.UNRESPONSIVE_DETECTED
    assert event.detail["reason"] == "sustained_heartbeat_failure"
    assert event.detail["consecutive_pass_timeouts"] == 2
    # It routes into the same recovery path a tripped detection takes.
    assert primary_fault(list(second)) is RunEventType.UNRESPONSIVE_DETECTED
    # Durably recorded before being returned (SC-010: detected *and recorded*).
    assert [e.event_type for e in store.events] == [RunEventType.UNRESPONSIVE_DETECTED]


async def test_a_completed_pass_resets_the_sustained_failure_streak() -> None:
    """A busy-but-servicing client never trips: any completed pass resets the streak (FR-014).

    Passes go eaten, healthy, eaten -- the streak never reaches two in a row, so nothing trips,
    even though two of the three passes were eaten.
    """
    store = _FakeStore(run=_run(LifecycleState.PLAYING))
    aggregator = _FlakyAggregator(hang_calls={1, 3})
    watch = _zombie_watch(store, aggregator, threshold=2)

    assert await watch.check_now(turn_number=1) == ()  # eaten -> streak 1
    assert await watch.check_now(turn_number=1) == ()  # healthy -> streak reset to 0
    assert await watch.check_now(turn_number=1) == ()  # eaten -> streak 1 again, not sustained

    assert store.events == []
    assert aggregator.calls == 3


async def test_a_zombie_tuner_is_detected_by_sustained_heartbeat_failure() -> None:
    """End to end through the real heartbeat and the fake's permanently-silent-tuner mode (T247).

    The fake answers the handshake and then services no command ever again -- the live zombie. A
    real `HeartbeatMonitor` probing it can never complete inside a pass bound smaller than the
    probe's own timeout, so every pass is eaten; the second consecutive eaten pass is the
    sustained pattern that trips.
    """
    import os

    from civsim_harness.nexus.client import NexusClient
    from fakes.fake_nexus import FakeNexusServer, loaded_game_state_table

    async with FakeNexusServer(state_table=loaded_game_state_table()) as server:
        client = NexusClient(host="127.0.0.1", port=server.port, command_timeout_s=5.0)
        try:
            await client.connect()  # the handshake succeeds -- the client looks alive
            server.go_silent_after_handshake()  # ...and now the tuner services nothing

            store = _FakeStore(run=_run(LifecycleState.PLAYING))
            liveness = ProcessLivenessMonitor(pid=os.getpid())  # process is alive forever
            watch = _zombie_watch(
                store,
                DetectionAggregator(
                    liveness=liveness,
                    heartbeat=HeartbeatMonitor(client=client, timeout_s=5.0),
                ),
                threshold=2,
            )
            # pass_bound_s (0.02) < the heartbeat's 5s probe -> the pass is eaten every time.
            first = await watch.check_now(turn_number=1)
            assert first == ()
            assert store.events == []

            second = await watch.check_now(turn_number=1)
            assert [e.event_type for e in second] == [RunEventType.UNRESPONSIVE_DETECTED]
            assert second[0].detail["reason"] == "sustained_heartbeat_failure"
        finally:
            await client.close()


def test_sustained_failure_default_is_the_smallest_meaningful_streak_within_the_budget() -> None:
    """The default streak (2), the pass bound, and the cadence are consistent with SC-010.

    - Two is the smallest "sustained": one eaten pass must not be a fault (FR-014).
    - The pass bound must exceed the heartbeat's own bound, or a real heartbeat could never
      complete within a pass and normal (first-pass) detection would break.
    - The common zombie is caught on the *first* pass -- the heartbeat times out at its own bound
      and the pass completes with hang_detected once the blocking command self-times-out -- landing
      inside SC-010's 60s; the streak is the backstop for the residual eaten-forever case.
    """
    from civsim_harness.nexus.client import DEFAULT_COMMAND_TIMEOUT_S
    from civsim_harness.run.detection import (
        DEFAULT_DETECTION_INTERVAL_S,
        DEFAULT_DETECTION_PASS_BOUND_S,
        DEFAULT_SUSTAINED_HEARTBEAT_FAILURES,
    )

    assert DEFAULT_SUSTAINED_HEARTBEAT_FAILURES == 2
    assert DEFAULT_DETECTION_PASS_BOUND_S > DEFAULT_HEARTBEAT_TIMEOUT_S
    # The pass bound is floored by FR-014: it must survive a wait on the longest command bound plus
    # the heartbeat's own bound, so a busy-but-terminating command does not eat the pass.
    assert DEFAULT_DETECTION_PASS_BOUND_S >= DEFAULT_COMMAND_TIMEOUT_S + DEFAULT_HEARTBEAT_TIMEOUT_S
    # Common-case detection (pass completes with hang_detected) lands inside SC-010's 60s.
    common_case_s = (
        DEFAULT_DETECTION_INTERVAL_S + DEFAULT_COMMAND_TIMEOUT_S + DEFAULT_HEARTBEAT_TIMEOUT_S
    )
    assert common_case_s < 60.0
