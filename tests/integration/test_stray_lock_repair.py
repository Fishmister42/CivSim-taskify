"""A run-identity lock that outlived its run, through the real lock directory and the CLI
(`run/orphans.py`'s lock-side scan, 2026-09-22).

**The wreck this is built from.** A live run reached `finished`, its driver process exited,
and its lock file stayed behind: `run-09110770797041989212fe6559f57900`, `client_pid 2459457`,
acquired `12:45:58Z`. The run was never written to any store. The next run could not start --
`RunIdentityLock.acquire` refuses a second run on a client PID some other lock already names,
and the Civ VI client is long-lived, so that dead run's lock went on naming a *live* PID -- and
nothing in the harness could say why. The whole orphan machinery starts from a run row and asks
about its lock; a lock with no run row is outside its reach by construction.

`test_the_run_side_scan_cannot_see_a_lock_whose_run_never_reached_the_store` is the negative
control for exactly that: it asserts the blindness directly, next to the lock-side scan that
ends it, so the asymmetry cannot quietly come back.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from civsim_harness.models.common import RunId
from civsim_harness.models.run import LifecycleState
from civsim_harness.operator.cli import app
from civsim_harness.run.identity_lock import RunIdentityLock
from civsim_harness.run.orphans import (
    LOCK_FILE_SUFFIX,
    StrayLockDisposition,
    clean_stray_locks,
    scan_orphans,
    scan_stray_locks,
    sweep_stray_locks,
)
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import record_run

#: A run this store has in flight, whose lock names this very live process.
LIVE = "run-live"
#: A run recorded as `playing` with no lock at all -- the run-side orphan (2026-09-21).
ORPHAN = "run-orphan"
#: Today's wreck: a lock file whose run id the store has never heard of, naming a PID that
#: is still alive (the game client outlives the driver).
STRAY = "run-stray"
#: A run the store records as `finished` whose lock was never released.
FINISHED = "run-finished"
#: A second lock the store has no row for, written by hand onto the live client PID.
UNRECORDED_TWO = "run-unrecorded-two"
#: A lock whose recorded client process is gone. High enough that no such process exists.
DEAD_PID = 2_147_483_600

_runner = CliRunner()


def _invoke(*args: str) -> tuple[int, str]:
    result = _runner.invoke(app, list(args))
    return result.exit_code, result.stdout


@pytest.fixture
def locks(tmp_path: Path) -> Path:
    return tmp_path / "locks"


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    path = tmp_path / "s.db"
    store = SqliteMatchStore(path, orphan_sweep=False)
    record_run(store, LIVE, turns=1, lifecycle_state="playing", started_minutes=30)
    record_run(store, ORPHAN, turns=1, lifecycle_state="playing", started_minutes=30)
    record_run(
        store,
        FINISHED,
        turns=1,
        lifecycle_state="finished",
        stop_resolution="turn_reached",
        started_minutes=30,
    )
    store.close()
    return path


@pytest.fixture
def lock_dir(locks: Path) -> RunIdentityLock:
    """The four locks the tests reason over, all older than any grace window.

    `LIVE` and `STRAY` name different live PIDs on purpose: `acquire` refuses two run
    identities on one client PID, which is precisely how the stray lock blocked the next
    run.
    """
    lock = RunIdentityLock(locks)
    old = datetime.now(UTC) - timedelta(hours=1)
    lock.acquire(run_id=RunId(LIVE), client_pid=os.getpid(), now=old)
    lock.acquire(run_id=RunId(STRAY), client_pid=os.getppid(), now=old)
    lock.acquire(run_id=RunId(FINISHED), client_pid=DEAD_PID, now=old)
    # A fourth, written by hand: a second unrecorded run id on the live client PID, which
    # `acquire` would have refused -- the shape the lock directory is in once one run's
    # lock outlives it.
    (locks / f"{UNRECORDED_TWO}.lock.json").write_text(
        json.dumps(
            {"run_id": UNRECORDED_TWO, "client_pid": os.getpid(), "acquired_at": old.isoformat()}
        ),
        encoding="utf-8",
    )
    return lock


def _names(directory: Path) -> set[str]:
    return {p.name for p in directory.glob("*.lock.json")}


def test_the_lock_file_naming_this_module_mirrors_is_the_one_the_lock_writes(
    locks: Path,
) -> None:
    """`LOCK_FILE_SUFFIX` is a copy of a private convention, so it is pinned here.

    A drift would not raise anywhere: the glob would simply match nothing and the scan
    would go quietly blind again -- the exact failure this file exists to end.
    """
    lock = RunIdentityLock(locks)
    lock.acquire(run_id=RunId(STRAY), client_pid=DEAD_PID, now=datetime.now(UTC))
    (written,) = locks.iterdir()
    assert written.name == f"{STRAY}{LOCK_FILE_SUFFIX}"
    assert lock.inspect(RunId(STRAY)).lock_path == written


def test_a_lock_probe_that_cannot_name_its_directory_is_not_an_error(store_path: Path) -> None:
    """The store-open sweep takes whatever probe it was given, including a test fake."""

    class _NoDir:
        def inspect(self, run_id: RunId) -> None:  # pragma: no cover - never called
            raise AssertionError("not reached")

    store = SqliteMatchStore(store_path, read_only=True)
    try:
        assert sweep_stray_locks(store, lock=_NoDir(), now=datetime.now(UTC)) == []
    finally:
        store.close()


# --------------------------------------------------------------------------
# the negative control
# --------------------------------------------------------------------------


def test_the_run_side_scan_cannot_see_a_lock_whose_run_never_reached_the_store(
    store_path: Path, lock_dir: RunIdentityLock, locks: Path
) -> None:
    """`scan_orphans` is blind to `STRAY`, and `scan_stray_locks` is what sees it.

    The first two assertions are the defect, asserted rather than described: every run-side
    finding is a run the store has a row for, so a lock whose run was never recorded cannot
    be among them at any grace window. The rest is the close.
    """
    now = datetime.now(UTC)
    store = SqliteMatchStore(store_path, read_only=True)
    try:
        run_side = scan_orphans(store, lock=lock_dir, now=now, grace_seconds=0)
        assert [str(f.run_id) for f in run_side] == [ORPHAN]
        assert STRAY not in {str(f.run_id) for f in run_side}

        strays = scan_stray_locks(store, lock=lock_dir, now=now, grace_seconds=0)
    finally:
        store.close()

    by_id = {str(f.run_id): f for f in strays}
    assert by_id[STRAY].disposition is StrayLockDisposition.UNRECORDED
    assert by_id[STRAY].lock.pid_alive is True
    assert by_id[STRAY].cleanable is False  # live PID, no row: reported, not deleted
    assert LIVE not in by_id  # a live claim on a run this store is playing is healthy


def test_a_lock_is_classified_by_what_the_store_and_the_process_table_say(
    store_path: Path, lock_dir: RunIdentityLock
) -> None:
    now = datetime.now(UTC)
    store = SqliteMatchStore(store_path, read_only=True)
    try:
        strays = scan_stray_locks(store, lock=lock_dir, now=now, grace_seconds=0)
    finally:
        store.close()
    by_id = {str(f.run_id): f for f in strays}
    # `FINISHED`'s lock names a dead PID *and* a run recorded as finished. Both would make
    # it cleanable; the run row is reported because it is the stronger fact -- "the run it
    # names is recorded as finished" survives a PID table that has moved on.
    assert by_id[FINISHED].disposition is StrayLockDisposition.TERMINAL_RUN
    assert by_id[FINISHED].run_state is LifecycleState.FINISHED
    assert by_id[FINISHED].cleanable is True
    assert by_id[STRAY].cleanable is False
    assert by_id[UNRECORDED_TWO].disposition is StrayLockDisposition.UNRECORDED
    assert by_id[FINISHED].age_seconds is not None and by_id[FINISHED].age_seconds > 3000
    assert by_id[FINISHED].age_source == "lock.acquired_at"


def test_a_lock_younger_than_the_grace_window_is_never_a_finding(
    store_path: Path, locks: Path
) -> None:
    lock = RunIdentityLock(locks)
    lock.acquire(run_id=RunId(STRAY), client_pid=DEAD_PID, now=datetime.now(UTC))
    store = SqliteMatchStore(store_path, read_only=True)
    try:
        assert scan_stray_locks(store, lock=lock, now=datetime.now(UTC)) == []
    finally:
        store.close()


def test_a_lock_for_a_run_the_store_records_as_terminal_is_cleanable(
    store_path: Path, locks: Path
) -> None:
    """Even with a live client PID: the run is over, so the lock is a fact about nothing."""
    lock = RunIdentityLock(locks)
    old = datetime.now(UTC) - timedelta(hours=1)
    lock.acquire(run_id=RunId(FINISHED), client_pid=os.getpid(), now=old)
    store = SqliteMatchStore(store_path, read_only=True)
    try:
        (finding,) = scan_stray_locks(store, lock=lock, now=datetime.now(UTC))
        assert finding.disposition is StrayLockDisposition.TERMINAL_RUN
        assert finding.cleanable is True
        assert clean_stray_locks([finding], lock=lock) == [finding]
    finally:
        store.close()
    assert _names(locks) == set()


def test_a_lock_re_acquired_since_the_scan_is_left_alone(store_path: Path, locks: Path) -> None:
    """The scan is a candidate list; the lock file read inside the clean is the authority."""
    lock = RunIdentityLock(locks)
    old = datetime.now(UTC) - timedelta(hours=1)
    lock.acquire(run_id=RunId(FINISHED), client_pid=DEAD_PID, now=old)
    store = SqliteMatchStore(store_path, read_only=True)
    try:
        (finding,) = scan_stray_locks(store, lock=lock, now=datetime.now(UTC))
    finally:
        store.close()
    lock.release(RunId(FINISHED))
    lock.acquire(run_id=RunId(FINISHED), client_pid=os.getpid(), now=datetime.now(UTC))
    assert clean_stray_locks([finding], lock=lock) == []
    assert _names(locks) == {f"{FINISHED}.lock.json"}


# --------------------------------------------------------------------------
# the operator surface
# --------------------------------------------------------------------------


def test_store_repair_dry_run_names_the_stray_lock_and_removes_nothing(
    store_path: Path, lock_dir: RunIdentityLock, locks: Path
) -> None:
    before = _names(locks)
    code, out = _invoke(
        "store",
        "repair",
        "--store",
        str(store_path),
        "--lock-dir",
        str(locks),
        "--grace-seconds",
        "0",
        "--dry-run",
    )
    assert code == 0, out
    assert "stray run-identity locks: 3" in out
    assert STRAY in out and "run_not_in_store" in out
    assert "nothing changed" in out
    assert _names(locks) == before


def test_store_repair_removes_the_provably_dead_locks_and_reports_the_rest(
    store_path: Path, lock_dir: RunIdentityLock, locks: Path
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
    assert "removed 1 stray run-identity lock(s)" in out
    assert "--clean-unrecorded-locks" in out  # the ambiguous ones are named, not deleted
    assert _names(locks) == {
        f"{LIVE}.lock.json",
        f"{STRAY}.lock.json",
        f"{UNRECORDED_TWO}.lock.json",
    }


def test_clean_unrecorded_locks_removes_the_lock_that_blocked_the_next_run(
    store_path: Path, lock_dir: RunIdentityLock, locks: Path
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
        "--clean-unrecorded-locks",
    )
    assert code == 0, out
    assert "removed 3 stray run-identity lock(s)" in out
    # The live run's lock survives: it is a claim on a run this store has in flight.
    assert _names(locks) == {f"{LIVE}.lock.json"}


def test_a_write_mode_open_reports_the_stray_lock_but_never_deletes_one(
    store_path: Path, lock_dir: RunIdentityLock, locks: Path
) -> None:
    before = _names(locks)
    store = SqliteMatchStore(store_path, orphan_lock=lock_dir, orphan_grace_seconds=0)
    try:
        found = {str(f.run_id) for f in store.stray_locks_on_open}
    finally:
        store.close()
    assert STRAY in found
    assert _names(locks) == before
