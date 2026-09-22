"""`run/orphans.py`: an orphaned run is diagnosed from its lock and its last activity,
paused with the evidence on the event, and a live one is never touched (2026-09-21).

Pure-Python fakes: the scan needs two store reads and one lock read, so the fakes here
are exactly those, and every assertion is about what the module decided and wrote.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.models.common import EventId, ModelRef, RunId
from civsim_harness.models.config import (
    ModelConfig,
    RunConfiguration,
    TurnReachedStopCondition,
)
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.run import LifecycleState, Run
from civsim_harness.run.identity_lock import LockInspection
from civsim_harness.run.orphans import (
    ORPHAN_CANDIDATE_STATES,
    ORPHAN_REASON,
    OrphanFinding,
    pause_orphan,
    repair_orphans,
    scan_orphans,
    sweep_orphans,
)

_NOW = datetime(2026, 9, 21, 23, 0, tzinfo=UTC)
_LOCKS = Path("/tmp/civsim-test-locks")


def _run(
    run_id: str,
    lifecycle_state: LifecycleState = LifecycleState.PLAYING,
    *,
    started_at: datetime | None = None,
) -> Run:
    return Run.model_validate(
        {
            "run_id": run_id,
            "config_id": "cfg-" + run_id,
            "lifecycle_state": lifecycle_state.value,
            "record_completeness_status": "unknown",
            "comparability_status": "comparable",
            "observation_catalog_version": {"version": "t", "content_hash": "t"},
            "action_catalog_version": {"version": "t", "content_hash": "t"},
            "game_build": "linux/1.0.12.9",
            "host_support_tier": "validated",
            "capture_path": "none",
            "started_at": started_at,
        }
    )


def _event(run_id: str, at: datetime, kind: RunEventType = RunEventType.SAVE_TAKEN) -> RunEvent:
    return RunEvent(
        event_id=EventId(uuid.uuid4().hex),
        run_id=RunId(run_id),
        event_type=kind,
        occurred_at=at,
        detail={},
    )


def _inspection(
    run_id: str,
    *,
    present: bool = False,
    readable: bool = False,
    pid: int | None = None,
    alive: bool = False,
) -> LockInspection:
    return LockInspection(
        run_id=RunId(run_id),
        lock_path=_LOCKS / f"{run_id}.lock.json",
        present=present,
        readable=readable,
        client_pid=pid,
        pid_alive=alive,
        acquired_at="2026-09-21T22:00:00+00:00" if readable else None,
    )


class _Lock:
    """A lock probe answering from a table; absent for anything not in it."""

    def __init__(self, **by_run: LockInspection) -> None:
        self._by_run = by_run

    def inspect(self, run_id: RunId) -> LockInspection:
        return self._by_run.get(str(run_id), _inspection(str(run_id)))


class _Store:
    """Exactly the reads the scan needs plus the two writes the pause performs,
    recording the order the writes arrived in."""

    def __init__(
        self,
        *runs: Run,
        events: dict[str, list[RunEvent]] | None = None,
        configs: dict[str, RunConfiguration] | None = None,
    ) -> None:
        self.runs: dict[str, Run] = {str(r.run_id): r for r in runs}
        self.events: dict[str, list[RunEvent]] = events or {}
        self.configs: dict[str, RunConfiguration] = configs or {}
        self.log: list[tuple[str, Any]] = []

    def get_run_configuration(self, run_id: RunId) -> RunConfiguration | None:
        return self.configs.get(str(run_id))

    def list_active_runs(self) -> list[Run]:
        terminal = {LifecycleState.FINISHED, LifecycleState.FAILED}
        return [r for r in self.runs.values() if r.lifecycle_state not in terminal]

    def list_run_events(self, run_id: RunId, **kwargs: Any) -> list[RunEvent]:
        return list(self.events.get(str(run_id), []))

    def get_run(self, run_id: RunId) -> Run | None:
        return self.runs.get(str(run_id))

    def write_run_event(self, event: RunEvent) -> EventId:
        self.log.append(("event", event))
        self.events.setdefault(str(event.run_id), []).append(event)
        return event.event_id

    def update_run(self, run_id: RunId, **fields: Any) -> None:
        self.log.append(("update", (str(run_id), fields)))
        run = self.runs[str(run_id)]
        self.runs[str(run_id)] = Run.model_validate({**run.model_dump(), **fields})


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


def test_a_playing_run_with_no_lock_and_stale_activity_is_orphaned() -> None:
    store = _Store(_run("run-a", started_at=_NOW - timedelta(hours=2)))
    findings = scan_orphans(store, lock=_Lock(), now=_NOW)
    assert [f.run_id for f in findings] == ["run-a"]
    finding = findings[0]
    assert finding.why == "identity lock absent"
    assert finding.last_activity_source == "run.started_at"
    assert finding.idle_seconds == pytest.approx(7200.0)
    assert finding.lifecycle_state is LifecycleState.PLAYING


def test_a_run_whose_lock_holder_is_alive_is_never_a_candidate() -> None:
    store = _Store(_run("run-a", started_at=_NOW - timedelta(hours=2)))
    lock = _Lock(**{"run-a": _inspection("run-a", present=True, readable=True, pid=1, alive=True)})
    assert scan_orphans(store, lock=lock, now=_NOW) == []


def test_a_dead_lock_holder_is_orphaned_and_named() -> None:
    store = _Store(_run("run-a", started_at=_NOW - timedelta(hours=2)))
    lock = _Lock(
        **{"run-a": _inspection("run-a", present=True, readable=True, pid=4242, alive=False)}
    )
    (finding,) = scan_orphans(store, lock=lock, now=_NOW)
    assert finding.why == "lock holder pid 4242 is not alive"
    assert finding.lock.client_pid == 4242


def test_an_unreadable_lock_file_is_orphaned_and_named() -> None:
    store = _Store(_run("run-a", started_at=_NOW - timedelta(hours=2)))
    lock = _Lock(**{"run-a": _inspection("run-a", present=True, readable=False)})
    (finding,) = scan_orphans(store, lock=lock, now=_NOW)
    assert finding.why == "identity lock unreadable"


def test_the_grace_window_protects_a_run_that_was_just_active() -> None:
    store = _Store(_run("run-a", started_at=_NOW - timedelta(seconds=30)))
    assert scan_orphans(store, lock=_Lock(), now=_NOW, grace_seconds=120) == []
    (finding,) = scan_orphans(store, lock=_Lock(), now=_NOW, grace_seconds=0)
    assert finding.idle_seconds == pytest.approx(30.0)


def test_the_latest_event_dates_the_last_activity() -> None:
    started = _NOW - timedelta(hours=2)
    store = _Store(
        _run("run-a", started_at=started),
        events={"run-a": [_event("run-a", _NOW - timedelta(minutes=10))]},
    )
    (finding,) = scan_orphans(store, lock=_Lock(), now=_NOW)
    assert finding.last_activity_source == "run_event.save_taken"
    assert finding.idle_seconds == pytest.approx(600.0)


def test_a_run_with_no_recorded_activity_and_no_lock_is_orphaned_on_the_lock_alone() -> None:
    store = _Store(_run("run-a", LifecycleState.PREPARING))
    (finding,) = scan_orphans(store, lock=_Lock(), now=_NOW)
    assert finding.last_activity_source == "none"
    assert finding.idle_seconds is None
    assert finding.lifecycle_state is LifecycleState.PREPARING


def _config(run_id: str, created_at: datetime) -> RunConfiguration:
    return RunConfiguration(
        config_id="cfg-" + run_id,
        map_seed="seed",
        civilization="civ",
        leader="leader",
        ruleset="ruleset",
        difficulty="prince",
        stop_condition=TurnReachedStopCondition(turn=5),
        agent_model_config=ModelConfig(primary=ModelRef(provider="test", model="m")),
        no_progress_step_limit=5,
        recovery_attempt_limit=3,
        min_free_disk_gb=1.0,
        created_at=created_at,
    )


def test_a_run_that_never_started_is_dated_by_its_configuration_and_the_grace_protects_it() -> None:
    fresh = _Store(
        _run("run-new", LifecycleState.PREPARING),
        configs={"run-new": _config("run-new", _NOW - timedelta(seconds=5))},
    )
    assert scan_orphans(fresh, lock=_Lock(), now=_NOW) == []
    stale = _Store(
        _run("run-old", LifecycleState.PREPARING),
        configs={"run-old": _config("run-old", _NOW - timedelta(hours=3))},
    )
    (finding,) = scan_orphans(stale, lock=_Lock(), now=_NOW)
    assert finding.last_activity_source == "run_configuration.created_at"
    assert finding.idle_seconds == pytest.approx(3 * 3600.0)


def test_the_transient_wait_states_are_not_candidates() -> None:
    assert ORPHAN_CANDIDATE_STATES == {LifecycleState.PREPARING, LifecycleState.PLAYING}
    store = _Store(
        _run("run-w", LifecycleState.WAITING_ON_MODEL, started_at=_NOW - timedelta(hours=1)),
        _run("run-p", LifecycleState.PAUSED, started_at=_NOW - timedelta(hours=1)),
    )
    assert scan_orphans(store, lock=_Lock(), now=_NOW) == []


def test_excluded_runs_are_never_candidates() -> None:
    store = _Store(
        _run("run-mine", started_at=_NOW - timedelta(hours=1)),
        _run("run-other", started_at=_NOW - timedelta(hours=1)),
    )
    findings = scan_orphans(store, lock=_Lock(), now=_NOW, exclude=frozenset({RunId("run-mine")}))
    assert [f.run_id for f in findings] == ["run-other"]


def test_a_failing_event_read_weakens_the_evidence_but_does_not_raise() -> None:
    class _Flaky(_Store):
        def list_run_events(self, run_id: RunId, **kwargs: Any) -> list[RunEvent]:
            raise OSError("events unreadable")

    store = _Flaky(_run("run-a", started_at=_NOW - timedelta(hours=2)))
    (finding,) = scan_orphans(store, lock=_Lock(), now=_NOW)
    assert finding.last_activity_source == "run.started_at"


# --------------------------------------------------------------------------
# finding
# --------------------------------------------------------------------------


def test_the_finding_renders_the_evidence_and_carries_it_as_event_detail() -> None:
    store = _Store(_run("run-a", started_at=_NOW - timedelta(hours=2)))
    (finding,) = scan_orphans(store, lock=_Lock(), now=_NOW)
    line = finding.render()
    assert "run-a" in line and "-> paused" in line and "identity lock absent" in line
    detail = finding.as_detail()
    assert detail["reason"] == ORPHAN_REASON
    assert detail["orphan_cause"] == "identity lock absent"
    assert detail["lock_present"] is False
    assert detail["lock_path"].endswith("run-a.lock.json")
    assert detail["last_activity_source"] == "run.started_at"
    assert detail["idle_seconds"] == pytest.approx(7200.0)


# --------------------------------------------------------------------------
# pause / repair / sweep
# --------------------------------------------------------------------------


def test_pause_writes_the_event_before_the_run_row_and_records_why() -> None:
    store = _Store(_run("run-a", started_at=_NOW - timedelta(hours=2)))
    (finding,) = scan_orphans(store, lock=_Lock(), now=_NOW)
    event = pause_orphan(store, finding, now=_NOW)
    assert event is not None
    assert event.event_type is RunEventType.LIFECYCLE_TRANSITION
    assert event.detail["from"] == "playing" and event.detail["to"] == "paused"
    assert event.detail["reason"] == ORPHAN_REASON
    assert event.detail["lock_present"] is False
    assert [kind for kind, _ in store.log[:2]] == ["event", "update"]
    assert store.runs["run-a"].lifecycle_state is LifecycleState.PAUSED


def test_pause_is_a_no_op_when_the_run_moved_on_since_the_scan() -> None:
    store = _Store(_run("run-a", started_at=_NOW - timedelta(hours=2)))
    (finding,) = scan_orphans(store, lock=_Lock(), now=_NOW)
    store.update_run(RunId("run-a"), lifecycle_state=LifecycleState.PAUSED)
    store.log.clear()
    assert pause_orphan(store, finding, now=_NOW) is None
    assert store.log == []


def test_a_preparing_orphan_is_paused_too() -> None:
    store = _Store(_run("run-p", LifecycleState.PREPARING))
    (finding,) = scan_orphans(store, lock=_Lock(), now=_NOW)
    event = pause_orphan(store, finding, now=_NOW)
    assert event is not None and event.detail["from"] == "preparing"
    assert store.runs["run-p"].lifecycle_state is LifecycleState.PAUSED


def test_repair_returns_only_what_it_paused() -> None:
    store = _Store(
        _run("run-a", started_at=_NOW - timedelta(hours=2)),
        _run("run-b", started_at=_NOW - timedelta(hours=2)),
    )
    findings = scan_orphans(store, lock=_Lock(), now=_NOW)
    store.update_run(RunId("run-b"), lifecycle_state=LifecycleState.PAUSED)
    applied = repair_orphans(store, findings, now=_NOW)
    assert [f.run_id for f in applied] == ["run-a"]


def test_sweep_never_raises_and_leaves_the_store_as_it_was() -> None:
    class _Broken(_Store):
        def list_active_runs(self) -> list[Run]:
            raise RuntimeError("store unreadable")

    store = _Broken(_run("run-a", started_at=_NOW - timedelta(hours=2)))
    assert sweep_orphans(store, lock=_Lock(), now=_NOW) == []
    assert store.log == []
    assert store.runs["run-a"].lifecycle_state is LifecycleState.PLAYING


def test_sweep_pauses_the_orphans_and_honours_the_exclusion() -> None:
    store = _Store(
        _run("run-mine", started_at=_NOW - timedelta(hours=1)),
        _run("run-dead", started_at=_NOW - timedelta(hours=1)),
    )
    applied = sweep_orphans(store, lock=_Lock(), now=_NOW, exclude=frozenset({RunId("run-mine")}))
    assert [f.run_id for f in applied] == ["run-dead"]
    assert store.runs["run-mine"].lifecycle_state is LifecycleState.PLAYING
    assert store.runs["run-dead"].lifecycle_state is LifecycleState.PAUSED
    assert isinstance(applied[0], OrphanFinding)
