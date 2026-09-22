"""Orphaned-run repair through the real store, the real lock directory, the CLI and the
runner's start path (`run/orphans.py`, 2026-09-21).

The blemish: a killed driver left `run-02168773…` in `playing` for hours. Here a run is
recorded as playing with no lock file behind it, and every entry point must pause it with
the evidence on the event -- while a run whose lock names *this* live process is left alone.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from civsim_harness.models.common import RunId
from civsim_harness.models.records import RunEventType
from civsim_harness.models.run import LifecycleState
from civsim_harness.operator.cli import app
from civsim_harness.run.identity_lock import RunIdentityLock
from civsim_harness.run.orphans import ORPHAN_REASON
from civsim_harness.run.runner import Runner, RunnerDependencies
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import record_run

ORPHAN = "run-orphan"
LIVE = "run-live"
_runner = CliRunner()


def _invoke(*args: str) -> tuple[int, str]:
    result = _runner.invoke(app, list(args))
    return result.exit_code, result.stdout


@pytest.fixture
def locks(tmp_path: Path) -> Path:
    return tmp_path / "locks"


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    """A store holding one playing run with no lock (the orphan) and one playing run
    whose lock names this very process (live). Written without the open-time sweep so
    the fixture itself records exactly the playing state the tests start from."""
    path = tmp_path / "s.db"
    store = SqliteMatchStore(path, orphan_sweep=False)
    record_run(store, ORPHAN, turns=2, lifecycle_state="playing", started_minutes=30)
    record_run(store, LIVE, turns=1, lifecycle_state="playing", started_minutes=30)
    store.close()
    return path


@pytest.fixture
def live_lock(locks: Path) -> RunIdentityLock:
    lock = RunIdentityLock(locks)
    lock.acquire(run_id=RunId(LIVE), client_pid=os.getpid(), now=datetime.now(UTC))
    return lock


def _states(path: Path) -> dict[str, LifecycleState]:
    store = SqliteMatchStore(path, read_only=True)
    try:
        return {
            run_id: store.get_run(RunId(run_id)).lifecycle_state  # type: ignore[union-attr]
            for run_id in (ORPHAN, LIVE)
        }
    finally:
        store.close()


def _orphan_events(path: Path) -> list[dict[str, Any]]:
    store = SqliteMatchStore(path, read_only=True)
    try:
        return [
            e.detail
            for e in store.list_run_events(RunId(ORPHAN))
            if e.event_type is RunEventType.LIFECYCLE_TRANSITION
            and e.detail.get("reason") == ORPHAN_REASON
        ]
    finally:
        store.close()


# --------------------------------------------------------------------------
# write-mode open
# --------------------------------------------------------------------------


def test_a_write_mode_open_pauses_the_orphan_and_records_the_evidence(
    store_path: Path, live_lock: RunIdentityLock
) -> None:
    store = SqliteMatchStore(store_path, orphan_lock=live_lock, orphan_grace_seconds=0)
    try:
        paused = store.orphans_paused_on_open
    finally:
        store.close()
    assert [f.run_id for f in paused] == [ORPHAN]
    assert _states(store_path) == {ORPHAN: LifecycleState.PAUSED, LIVE: LifecycleState.PLAYING}
    (detail,) = _orphan_events(store_path)
    assert detail["from"] == "playing" and detail["to"] == "paused"
    assert detail["orphan_cause"] == "identity lock absent"
    assert detail["lock_present"] is False
    assert detail["lock_path"].endswith(f"{ORPHAN}.lock.json")
    assert detail["last_activity_source"].startswith("run")
    assert detail["idle_seconds"] is not None


def test_a_run_whose_lock_holder_is_this_live_process_is_never_touched(
    store_path: Path, live_lock: RunIdentityLock
) -> None:
    store = SqliteMatchStore(store_path, orphan_lock=live_lock, orphan_grace_seconds=0)
    store.close()
    assert _states(store_path)[LIVE] is LifecycleState.PLAYING
    store = SqliteMatchStore(store_path, read_only=True)
    try:
        assert all(
            e.detail.get("reason") != ORPHAN_REASON for e in store.list_run_events(RunId(LIVE))
        )
    finally:
        store.close()


def test_the_grace_window_and_the_opt_out_leave_everything_as_it_was(
    store_path: Path, live_lock: RunIdentityLock
) -> None:
    # The builder stamps the run's activity at a fixed date in the past, so the orphan reads as
    # idle for many hours; a grace wider than that idle must protect it exactly as the default
    # two minutes protect a run that was active seconds ago.
    SqliteMatchStore(store_path, orphan_lock=live_lock, orphan_grace_seconds=10**8).close()
    assert _states(store_path)[ORPHAN] is LifecycleState.PLAYING
    SqliteMatchStore(store_path, orphan_lock=live_lock, orphan_sweep=False).close()
    assert _states(store_path)[ORPHAN] is LifecycleState.PLAYING


def test_a_read_only_open_never_sweeps(store_path: Path, live_lock: RunIdentityLock) -> None:
    store = SqliteMatchStore(
        store_path, read_only=True, orphan_lock=live_lock, orphan_grace_seconds=0
    )
    try:
        assert store.orphans_paused_on_open == ()
    finally:
        store.close()
    assert _states(store_path)[ORPHAN] is LifecycleState.PLAYING


# --------------------------------------------------------------------------
# civsim store repair
# --------------------------------------------------------------------------


def test_repair_dry_run_lists_the_orphan_and_changes_nothing(
    store_path: Path, live_lock: RunIdentityLock, locks: Path
) -> None:
    code, out = _invoke(
        "store",
        "repair",
        "--store",
        str(store_path),
        "--dry-run",
        "--lock-dir",
        str(locks),
        "--grace-seconds",
        "0",
    )
    assert code == 0, out
    assert "orphaned runs: 1" in out
    assert ORPHAN in out and "identity lock absent" in out and "-> paused" in out
    assert LIVE not in out.split("orphaned runs")[1]
    # The lock side is scanned in the same pass (2026-09-22): here there is nothing to
    # report, LIVE's lock being a live claim on a run this store has in flight.
    assert "stray run-identity locks: none" in out
    assert "dry run: 1 run(s) would be paused, 0 lock(s) removed; nothing changed" in out
    assert _states(store_path) == {ORPHAN: LifecycleState.PLAYING, LIVE: LifecycleState.PLAYING}
    assert _orphan_events(store_path) == []


def test_repair_pauses_the_orphan_and_prints_the_evidence(
    store_path: Path, live_lock: RunIdentityLock, locks: Path
) -> None:
    code, out = _invoke(
        "store",
        "repair",
        "--store",
        str(store_path),
        "--lock-dir",
        str(locks),
        "--grace-seconds",
        "0",
    )
    assert code == 0, out
    assert "orphaned runs: 1" in out and ORPHAN in out
    assert "paused 1 orphaned run(s)" in out
    assert _states(store_path) == {ORPHAN: LifecycleState.PAUSED, LIVE: LifecycleState.PLAYING}
    (detail,) = _orphan_events(store_path)
    assert detail["reason"] == ORPHAN_REASON

    code, out = _invoke(
        "store",
        "repair",
        "--store",
        str(store_path),
        "--lock-dir",
        str(locks),
        "--grace-seconds",
        "0",
    )
    assert code == 0 and "orphaned runs: none" in out and "paused 0" in out


def test_repair_refuses_a_missing_store_with_exit_code_2(tmp_path: Path, locks: Path) -> None:
    code, _ = _invoke(
        "store", "repair", "--store", str(tmp_path / "none.db"), "--lock-dir", str(locks)
    )
    assert code == 2


# --------------------------------------------------------------------------
# the runner's start path
# --------------------------------------------------------------------------


def _make_runner(store: SqliteMatchStore, live_lock: RunIdentityLock) -> Runner:
    never = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("not reached"))  # noqa: E731
    return Runner(
        RunnerDependencies(
            store=store,
            prepare_run=never,
            build_turn_dependencies=never,
            evaluate_stop_facts=never,
            orphan_lock=live_lock,
            orphan_grace_seconds=0,
        )
    )


def test_the_runner_sweeps_orphans_before_starting_but_never_its_own_runs(
    store_path: Path, live_lock: RunIdentityLock
) -> None:
    store = SqliteMatchStore(store_path, orphan_sweep=False)
    runner = _make_runner(store, live_lock)
    try:
        own = store.get_run(RunId(ORPHAN))
        assert own is not None
        # Register the orphan as one of this runner's own in-flight runs: it must be excluded.
        runner._runs[RunId(ORPHAN)] = SimpleNamespace(run=own)  # type: ignore[assignment]
        runner._sweep_orphans()
        assert store.get_run(RunId(ORPHAN)).lifecycle_state is LifecycleState.PLAYING  # type: ignore[union-attr]
        runner._runs.clear()
        runner._sweep_orphans()
        assert store.get_run(RunId(ORPHAN)).lifecycle_state is LifecycleState.PAUSED  # type: ignore[union-attr]
        assert store.get_run(RunId(LIVE)).lifecycle_state is LifecycleState.PLAYING  # type: ignore[union-attr]
    finally:
        store.close()


def test_the_runner_sweep_can_be_switched_off(store_path: Path, live_lock: RunIdentityLock) -> None:
    store = SqliteMatchStore(store_path, orphan_sweep=False)
    never = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("not reached"))  # noqa: E731
    runner = Runner(
        RunnerDependencies(
            store=store,
            prepare_run=never,
            build_turn_dependencies=never,
            evaluate_stop_facts=never,
            orphan_sweep=False,
            orphan_lock=live_lock,
            orphan_grace_seconds=0,
        )
    )
    try:
        runner._sweep_orphans()
        assert store.get_run(RunId(ORPHAN)).lifecycle_state is LifecycleState.PLAYING  # type: ignore[union-attr]
    finally:
        store.close()
