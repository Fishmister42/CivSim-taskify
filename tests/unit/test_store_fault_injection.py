"""SC-001: interruptions at every write boundary (T016, T017; 003 FR-002, FR-003, FR-004).

Two layers (research R13):

1. A deterministic fault-injecting connection proxy raises on the *k*-th SQL statement of a
   write, for every *k* the write issues, in two modes -- **before** the statement runs (the
   write never lands) and, for ``COMMIT``, **after** it ran (the write landed but the
   acknowledgement was lost: the D4 ambiguous failure). After each fault the file is reopened
   through a fresh connection and checked: the turn is wholly present or wholly absent at turn
   *and* step granularity, its model-call rows match its steps exactly, an attempted-but-unwritten
   turn is a gap once the run stops, and the identical rewrite is accepted. The total number of
   injected interruptions is counted and asserted to be at least 200.

2. One real ``SIGKILL`` of a subprocess mid-``write_turn_cycle``, then the same integrity check.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.errors import StoreWriteError
from civsim_harness.models.run import RecordCompletenessStatus
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import (
    make_capture,
    make_config,
    make_event,
    make_failed_call,
    make_run,
    make_save_point,
    make_turn_cycle_record,
)

# `orphan_sweep=False` (2026-09-21): these runs are written with fixed past timestamps and no
# run-identity lock, which is exactly what `run/orphans.py` pauses on a write-mode open. This
# file measures write atomicity and retry idempotence, so the open-time sweep is switched off
# here; `tests/integration/test_orphan_repair.py` covers the sweep itself.


class _InjectedFault(sqlite3.OperationalError):
    pass


class _FaultingConnection:
    """Wraps a `sqlite3.Connection`; trips once, on the *k*-th `execute`."""

    def __init__(self, inner: sqlite3.Connection, *, fail_at: int, after: bool) -> None:
        self._inner = inner
        self._fail_at = fail_at
        self._after = after
        self.statements = 0
        self.tripped = False

    def execute(self, sql: str, params: Any = ()) -> Any:
        self.statements += 1
        if self.statements == self._fail_at and not self.tripped:
            self.tripped = True
            if not self._after:
                raise _InjectedFault("injected before statement")
            self._inner.execute(sql, params)
            raise _InjectedFault("injected after statement")
        return self._inner.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _check_invariants(path: Path) -> SqliteMatchStore:
    """Every turn present has all its steps and calls; no orphan rows anywhere."""
    store = SqliteMatchStore(path, orphan_sweep=False)
    conn = store._conn  # noqa: SLF001 -- white-box integrity check
    for turn_cycle_id, step_count in conn.execute(
        "SELECT turn_cycle_id, step_count FROM turn_cycles"
    ).fetchall():
        steps = conn.execute(
            "SELECT decision_step_id FROM decision_steps WHERE turn_cycle_id = ?",
            (turn_cycle_id,),
        ).fetchall()
        assert len(steps) == step_count, f"{turn_cycle_id}: {len(steps)} of {step_count} steps"
        for (step_id,) in steps:
            call = conn.execute(
                "SELECT 1 FROM model_calls WHERE decision_step_id = ?", (step_id,)
            ).fetchone()
            assert call is not None, f"{step_id} has no model_calls row (W1)"
    orphans = conn.execute(
        "SELECT COUNT(*) FROM decision_steps WHERE turn_cycle_id NOT IN "
        "(SELECT turn_cycle_id FROM turn_cycles)"
    ).fetchone()[0]
    assert orphans == 0
    return store


# --------------------------------------------------------------------------
# The write operations under test, each as (setup, operation, verify-after-retry)
# --------------------------------------------------------------------------

RUN = "r"
TURN = make_turn_cycle_record(RUN, 2, num_steps=6, cost_usd=0.01)
EVENT = make_event("e-1", RUN, "save_taken", turn_number=2)
FAILED = make_failed_call(RUN, "r-t3-a0", "r-t3-a0-step1", "r-failed")
SAVE3 = make_save_point("r-sp3-0", RUN, 3)
CAPTURE, BLOB = make_capture("c-1", RUN, 2, "r-t2-a0-step1", blob=b"frame")
AT = datetime(2026, 9, 21, 15, tzinfo=UTC)


def _baseline(store: SqliteMatchStore) -> None:
    store.create_run(make_run(RUN, "r-cfg"), make_config("r-cfg"))
    store.write_save_point(make_save_point("r-sp1-0", RUN, 1))
    store.write_turn_cycle(make_turn_cycle_record(RUN, 1, num_steps=2))
    store.write_save_point(make_save_point("r-sp2-0", RUN, 2))


def _op_write_turn(store: SqliteMatchStore) -> None:
    store.write_turn_cycle(TURN)


def _verify_turn(store: SqliteMatchStore, *, landed: bool) -> None:
    present = store.get_turn_cycle(RUN, 2)
    if present is None:
        assert store.list_model_calls(RUN, turn=2) == []
        store.update_run(RUN, lifecycle_state="paused")
        assert store.turn_gaps(RUN) == [2]  # attempted, unwritten, run stopped -> a gap
        assert store.record_completeness(RUN) is RecordCompletenessStatus.HAS_GAPS
        store.update_run(RUN, lifecycle_state="playing")
    else:
        assert present == TURN
        assert len(store.list_model_calls(RUN, turn=2)) == 6
    store.write_turn_cycle(TURN)  # the retry, identical: accepted, once
    assert store.get_turn_cycle(RUN, 2) == TURN
    assert len(store.list_turn_attempts(RUN, 2)) == 1
    assert len(store.list_model_calls(RUN, turn=2)) == 6


def _op_event(store: SqliteMatchStore) -> None:
    store.write_run_event(EVENT)


def _verify_event(store: SqliteMatchStore, *, landed: bool) -> None:
    store.write_run_event(EVENT)
    assert [e.event_id for e in store.list_run_events(RUN)] == ["e-1"]


def _op_model_call(store: SqliteMatchStore) -> None:
    store.write_model_call(FAILED)


def _verify_model_call(store: SqliteMatchStore, *, landed: bool) -> None:
    store.write_model_call(FAILED)
    assert [r.call.model_call_id for r in store.list_model_calls(RUN) if r.turn_number is None] == [
        "r-failed"
    ]


def _op_save_point(store: SqliteMatchStore) -> None:
    store.write_save_point(SAVE3)


def _verify_save_point(store: SqliteMatchStore, *, landed: bool) -> None:
    store.write_save_point(SAVE3)
    assert [s.turn_number for s in store.list_save_points(RUN)] == [1, 2, 3]


def _op_capture(store: SqliteMatchStore) -> None:
    store.write_capture(CAPTURE, BLOB)


def _verify_capture(store: SqliteMatchStore, *, landed: bool) -> None:
    store.write_capture(CAPTURE, BLOB)
    assert store.get_capture_image("c-1").content == b"frame"


def _op_supersede(store: SqliteMatchStore) -> None:
    store.mark_turn_superseded(RUN, 1, 0)


def _verify_supersede(store: SqliteMatchStore, *, landed: bool) -> None:
    store.mark_turn_superseded(RUN, 1, 0)
    summaries = store.list_turn_attempts(RUN, 1)
    assert [(s.attempt_index, s.is_authoritative) for s in summaries] == [(0, False)]
    # While playing, a run with no authoritative turn is simply replaying turn 1 (the 002
    # turn_gaps rule); once it stops, both attempted turns are gaps.
    store.update_run(RUN, lifecycle_state="paused")
    assert store.turn_gaps(RUN) == [1, 2]
    assert store.get_run(RUN).record_completeness_status is RecordCompletenessStatus.HAS_GAPS
    store.update_run(RUN, lifecycle_state="playing")


BIG_TURN = make_turn_cycle_record(RUN, 2, num_steps=20, cost_usd=0.01)


def _op_write_big_turn(store: SqliteMatchStore) -> None:
    store.write_turn_cycle(BIG_TURN)


def _verify_big_turn(store: SqliteMatchStore, *, landed: bool) -> None:
    present = store.get_turn_cycle(RUN, 2)
    assert present is None or present == BIG_TURN
    store.write_turn_cycle(BIG_TURN)
    assert store.get_turn_cycle(RUN, 2) == BIG_TURN
    assert len(store.list_model_calls(RUN, turn=2)) == 20


NEW_RUN = make_run("s", "s-cfg")
NEW_CONFIG = make_config("s-cfg")


def _op_create_run(store: SqliteMatchStore) -> None:
    store.create_run(NEW_RUN, NEW_CONFIG)


def _verify_create_run(store: SqliteMatchStore, *, landed: bool) -> None:
    store.create_run(NEW_RUN, NEW_CONFIG)
    assert store.get_run("s") == NEW_RUN
    assert store.get_run_configuration("s") == NEW_CONFIG
    assert len([r for r in store.list_runs() if r.run_id == "s"]) == 1


def _op_update(store: SqliteMatchStore) -> None:
    store.update_run(RUN, lifecycle_state="paused")


def _verify_update(store: SqliteMatchStore, *, landed: bool) -> None:
    store.update_run(RUN, lifecycle_state="paused")
    assert store.get_run(RUN).lifecycle_state.value == "paused"


def _op_archive(store: SqliteMatchStore) -> None:
    store.update_run(RUN, lifecycle_state="finished", stop_resolution="turn_reached")
    store.archive_run(RUN, by="t", at=AT)


def _verify_archive(store: SqliteMatchStore, *, landed: bool) -> None:
    run = store.get_run(RUN)
    if run.lifecycle_state.value != "finished":
        store.update_run(RUN, lifecycle_state="finished", stop_resolution="turn_reached")
    store.archive_run(RUN, by="t", at=AT)
    assert store.get_run(RUN).archived_at is not None
    assert [e.event_type.value for e in store.list_run_events(RUN)].count("run_archived") == 1
    assert all(s.retention_status.value == "eligible" for s in store.list_save_points(RUN))


OPERATIONS: list[tuple[str, Callable[[SqliteMatchStore], None], Callable[..., None]]] = [
    ("write_turn_cycle", _op_write_turn, _verify_turn),
    ("write_turn_cycle_20_steps", _op_write_big_turn, _verify_big_turn),
    ("create_run", _op_create_run, _verify_create_run),
    ("write_run_event", _op_event, _verify_event),
    ("write_model_call", _op_model_call, _verify_model_call),
    ("write_save_point", _op_save_point, _verify_save_point),
    ("write_capture", _op_capture, _verify_capture),
    ("mark_turn_superseded", _op_supersede, _verify_supersede),
    ("update_run", _op_update, _verify_update),
    ("archive_run", _op_archive, _verify_archive),
]


def _statement_count(tmp_path: Path, operation: Callable[[SqliteMatchStore], None]) -> int:
    path = tmp_path / "count.db"
    store = SqliteMatchStore(path, orphan_sweep=False)
    _baseline(store)
    proxy = _FaultingConnection(store._conn, fail_at=0, after=False)  # noqa: SLF001
    store._conn = proxy  # type: ignore[assignment]  # noqa: SLF001
    operation(store)
    store.close()
    for leftover in tmp_path.glob("count.db*"):
        leftover.unlink()
    return proxy.statements


@pytest.mark.parametrize(
    ("name", "operation", "verify"), OPERATIONS, ids=[o[0] for o in OPERATIONS]
)
def test_every_interruption_leaves_the_record_whole_or_absent_and_the_retry_idempotent(
    tmp_path: Path,
    name: str,
    operation: Callable[[SqliteMatchStore], None],
    verify: Callable[..., None],
) -> None:
    statements = _statement_count(tmp_path, operation)
    assert statements >= 2
    interruptions = 0
    for after in (False, True):
        for k in range(1, statements + 1):
            path = tmp_path / f"{name}-{int(after)}-{k}.db"
            store = SqliteMatchStore(path, orphan_sweep=False)
            _baseline(store)
            proxy = _FaultingConnection(store._conn, fail_at=k, after=after)  # noqa: SLF001
            store._conn = proxy  # type: ignore[assignment]  # noqa: SLF001
            with pytest.raises(StoreWriteError):
                operation(store)
            assert proxy.tripped
            store.close()
            interruptions += 1

            reopened = _check_invariants(path)
            try:
                verify(reopened, landed=after)
            finally:
                reopened.close()
    # Recorded for the validation results: the number of boundaries this operation was cut at.
    print(f"SC-001 {name}: {interruptions} interruptions over {statements} statements x 2 modes")
    _INTERRUPTIONS[name] = interruptions


_INTERRUPTIONS: dict[str, int] = {}


def test_sc001_at_least_two_hundred_interruptions_were_injected() -> None:
    """Runs after the parametrised cases in file order; the count they accumulated is the
    evidence SC-001 asks for."""
    total = sum(_INTERRUPTIONS.values())
    print(f"SC-001 total interruptions: {total} ({_INTERRUPTIONS})")
    assert total >= 200, _INTERRUPTIONS


# --------------------------------------------------------------------------
# A real SIGKILL (T017)
# --------------------------------------------------------------------------

_CHILD = textwrap.dedent(
    """
    import sys
    from store_support.builders import make_config, make_run, make_save_point
    from store_support.builders import make_turn_cycle_record
    from civsim_harness.store.sqlite_adapter import SqliteMatchStore

    path = sys.argv[1]
    store = SqliteMatchStore(path, orphan_sweep=False)
    store.create_run(make_run("k", "k-cfg"), make_config("k-cfg"))
    turn = 1
    while True:
        store.write_save_point(make_save_point(f"k-sp{turn}-0", "k", turn))
        store.write_turn_cycle(make_turn_cycle_record("k", turn, num_steps=200, cost_usd=0.001))
        print(turn, flush=True)
        turn += 1
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL semantics differ on Windows")
def test_a_killed_writer_leaves_every_present_turn_whole_and_contiguous(tmp_path: Path) -> None:
    path = tmp_path / "killed.db"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).resolve().parents[1]), env_path]
            if (env_path := os.environ.get("PYTHONPATH"))
            else [str(Path(__file__).resolve().parents[1])]
        ),
    }
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert child.stdout is not None
        first = child.stdout.readline()
        assert first.strip() == "1", child.stderr.read() if child.stderr else first
        # Let it get into the middle of turn 2's 200-step write, then kill it hard.
        time.sleep(0.05)
        child.send_signal(signal.SIGKILL)
        child.wait(30)
    finally:
        if child.poll() is None:  # pragma: no cover - defensive
            child.kill()
    assert child.returncode == -signal.SIGKILL

    store = _check_invariants(path)
    try:
        turns = sorted(
            row[0]
            for row in store._conn.execute(  # noqa: SLF001
                "SELECT turn_number FROM turn_cycles WHERE run_id = 'k'"
            ).fetchall()
        )
        assert turns and turns == list(range(1, turns[-1] + 1))
        for turn in turns:
            record = store.get_turn_cycle("k", turn)
            assert record is not None and len(record.steps) == 200
            assert len(store.list_model_calls("k", turn=turn)) == 200
        store.update_run("k", lifecycle_state="paused")
        gaps = store.turn_gaps("k")
        assert gaps in ([], [turns[-1] + 1])  # at most the in-flight turn, and it is reported
    finally:
        store.close()
