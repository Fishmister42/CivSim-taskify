"""The SQLite + content-addressed blob reference adapter (T039, T040).

Implements `civsim_harness.store.port.MatchStore` -- the interim store the
harness is built and tested against until deliverable 3's real
match-tracking store lands (contracts/match-store-port.md, plan Complexity
Tracking C2). It is a storage *implementation* of the port, not a second
bypassing path: every write the harness needs goes through the same nine
methods, and nothing here writes a record through a route the port does
not expose.

Durability strategy (D1, D3, D6): a single persistent `sqlite3` connection,
opened once with `journal_mode=WAL` and `synchronous=FULL` -- the strongest
durability pragma SQLite offers, so a committed transaction is fsynced
before the write call returns. Every write runs inside an explicit
`BEGIN IMMEDIATE ... COMMIT` transaction; any exception rolls the
transaction back and re-raises as `StoreWriteError`, so a write is never
half-applied and never returns without either succeeding durably or
raising (D2, D3). A single `threading.Lock` serialises all access to the
one connection -- the same "one shared resource, one lock" shape
`nexus/client.py` already uses for its socket.

Archival (T040, FR-036, A1-A4): `archive_run` is the *only* method in this
entire module that ever writes `retention_status = 'eligible'` into
`save_points`, or `archived_at` into `runs`. `write_save_point` rejects
`retention_status = eligible` unconditionally, from any caller, and
`update_run` rejects any attempt to set `archived_at`. There is no TTL, no
age check, no quota, no retention window, and no thinning anywhere in this
file -- search it; the only place `RetentionStatus.ELIGIBLE` or
`archived_at=` appears as something being *written* is inside
`archive_run` itself.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from civsim_harness.errors import StoreWriteError
from civsim_harness.models.common import (
    CaptureId,
    EventId,
    ModelCallId,
    RunId,
    SavePointId,
    Timestamp,
    TurnCycleId,
)
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.records import (
    ModelCall,
    RetentionStatus,
    RunEvent,
    RunEventType,
    SavePoint,
)
from civsim_harness.models.run import LifecycleState, Run
from civsim_harness.models.turn import ScreenCapture, ScreeningStatus, TurnCycle, TurnOutcome
from civsim_harness.store.port import DecisionStepBundle, StoreHealth, TurnCycleRecord
from civsim_harness.telemetry.redaction import redact_text

_TERMINAL_LIFECYCLE_STATES = frozenset({LifecycleState.FINISHED, LifecycleState.FAILED})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    parent_run_id TEXT,
    archived_at TEXT,
    run_json TEXT NOT NULL,
    config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turn_cycles (
    turn_cycle_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    attempt_index INTEGER NOT NULL,
    is_authoritative INTEGER NOT NULL,
    step_count INTEGER NOT NULL,
    turn_json TEXT NOT NULL,
    UNIQUE (run_id, turn_number, attempt_index)
);
CREATE INDEX IF NOT EXISTS idx_turn_cycles_run_turn ON turn_cycles(run_id, turn_number);

CREATE TABLE IF NOT EXISTS decision_steps (
    decision_step_id TEXT PRIMARY KEY,
    turn_cycle_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    bundle_json TEXT NOT NULL,
    UNIQUE (turn_cycle_id, step_index)
);
CREATE INDEX IF NOT EXISTS idx_decision_steps_turn_cycle ON decision_steps(turn_cycle_id);

CREATE TABLE IF NOT EXISTS run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id);

CREATE TABLE IF NOT EXISTS model_calls (
    model_call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    decision_step_id TEXT NOT NULL,
    call_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_calls_run ON model_calls(run_id);

CREATE TABLE IF NOT EXISTS save_points (
    save_point_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    retention_status TEXT NOT NULL,
    save_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_save_points_run ON save_points(run_id);
CREATE INDEX IF NOT EXISTS idx_save_points_retention ON save_points(retention_status);

CREATE TABLE IF NOT EXISTS captures (
    capture_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    decision_step_id TEXT NOT NULL,
    blob_ref TEXT,
    capture_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_captures_run ON captures(run_id);
"""


class SqliteMatchStore:
    """SQLite + content-addressed blob `MatchStore` reference adapter.

    `db_path` is the SQLite database file (created, along with its parent
    directory, if absent). `blob_dir` defaults to a `blobs/` sibling of
    `db_path` and holds capture image bytes, addressed by the sha256 of
    their content (`blob_dir/<hash[:2]>/<hash>`), written via a
    temp-file-then-`os.replace` sequence with an explicit `fsync` so a
    blob is durable on disk before the row referencing it is committed.
    """

    def __init__(self, db_path: str | Path, *, blob_dir: str | Path | None = None) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._blob_dir = Path(blob_dir) if blob_dir is not None else self._db_path.parent / "blobs"
        self._blob_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------------
    # Transaction helpers
    # ----------------------------------------------------------------

    def _run_in_transaction[T](self, body: Callable[[sqlite3.Connection], T]) -> T:
        """Run *body* inside one atomic, durable transaction (D1-D3).

        Commits only if *body* returns normally; any exception rolls the
        transaction back and is re-raised as a `StoreWriteError` (unless it
        already is one), so a caller can never observe a write as having
        both partially applied and returned successfully (D2).
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                result = body(self._conn)
                self._conn.execute("COMMIT")
                return result
            except Exception as exc:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(exc, StoreWriteError):
                    raise
                raise StoreWriteError(
                    "match store write failed and was rolled back",
                    detail={"error": str(exc), "error_type": type(exc).__name__},
                ) from exc

    def _with_lock[T](self, body: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            return body(self._conn)

    # ----------------------------------------------------------------
    # Blob storage (content-addressed)
    # ----------------------------------------------------------------

    def _store_blob(self, digest: str, blob: bytes) -> None:
        subdir = self._blob_dir / digest[:2]
        subdir.mkdir(parents=True, exist_ok=True)
        final_path = subdir / digest
        if final_path.exists():
            return  # content-addressed: identical content is already durable
        tmp_path = subdir / f".{digest}.tmp-{uuid.uuid4().hex}"
        with open(tmp_path, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, final_path)

    # ----------------------------------------------------------------
    # Reconstruction helpers
    # ----------------------------------------------------------------

    def _load_turn_cycle_record(
        self, conn: sqlite3.Connection, turn_cycle_id: str
    ) -> TurnCycleRecord:
        row = conn.execute(
            "SELECT turn_json FROM turn_cycles WHERE turn_cycle_id = ?", (turn_cycle_id,)
        ).fetchone()
        if row is None:
            raise StoreWriteError(
                "no such turn_cycle_id", detail={"turn_cycle_id": turn_cycle_id}
            )
        turn_cycle = TurnCycle.model_validate_json(row[0])
        step_rows = conn.execute(
            "SELECT bundle_json FROM decision_steps WHERE turn_cycle_id = ? "
            "ORDER BY step_index ASC",
            (turn_cycle_id,),
        ).fetchall()
        steps = [DecisionStepBundle.model_validate_json(step_row[0]) for step_row in step_rows]
        return TurnCycleRecord(turn_cycle=turn_cycle, steps=steps)

    # ----------------------------------------------------------------
    # Writes
    # ----------------------------------------------------------------

    def create_run(self, run: Run, config: RunConfiguration) -> RunId:
        def body(conn: sqlite3.Connection) -> RunId:
            row = conn.execute(
                "SELECT run_json FROM runs WHERE run_id = ?", (run.run_id,)
            ).fetchone()
            if row is not None:
                if Run.model_validate_json(row[0]) != run:
                    raise StoreWriteError(
                        "create_run called twice for the same run_id with different content",
                        detail={"run_id": run.run_id},
                    )
                return run.run_id
            conn.execute(
                "INSERT INTO runs (run_id, config_id, lifecycle_state, parent_run_id, "
                "archived_at, run_json, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.config_id,
                    run.lifecycle_state.value,
                    run.parent_run_id,
                    run.archived_at.isoformat() if run.archived_at else None,
                    run.model_dump_json(),
                    config.model_dump_json(by_alias=True),
                ),
            )
            return run.run_id

        return self._run_in_transaction(body)

    def update_run(self, run_id: RunId, **fields: Any) -> None:
        if "archived_at" in fields:
            raise StoreWriteError(
                "update_run may not set archived_at; only archive_run may (FR-036, A2)",
                detail={"run_id": run_id},
            )
        if not fields:
            return

        def body(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT run_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise StoreWriteError("update_run: no such run", detail={"run_id": run_id})
            current = Run.model_validate_json(row[0])
            merged = current.model_dump()
            merged.update(fields)
            try:
                updated = Run.model_validate(merged)
            except ValidationError as exc:
                raise StoreWriteError(
                    "update_run produced an invalid Run",
                    detail={"run_id": run_id, "error": str(exc)},
                ) from exc
            conn.execute(
                "UPDATE runs SET config_id=?, lifecycle_state=?, parent_run_id=?, "
                "archived_at=?, run_json=? WHERE run_id=?",
                (
                    updated.config_id,
                    updated.lifecycle_state.value,
                    updated.parent_run_id,
                    updated.archived_at.isoformat() if updated.archived_at else None,
                    updated.model_dump_json(),
                    run_id,
                ),
            )

        self._run_in_transaction(body)

    def write_turn_cycle(self, record: TurnCycleRecord) -> TurnCycleId:
        tc = record.turn_cycle

        def body(conn: sqlite3.Connection) -> TurnCycleId:
            existing = conn.execute(
                "SELECT turn_cycle_id FROM turn_cycles "
                "WHERE run_id = ? AND turn_number = ? AND attempt_index = ?",
                (tc.run_id, tc.turn_number, tc.attempt_index),
            ).fetchone()
            if existing is not None:
                existing_id: str = existing[0]
                existing_record = self._load_turn_cycle_record(conn, existing_id)
                if existing_record != record:
                    raise StoreWriteError(
                        "write_turn_cycle: retried write for the same "
                        "(run_id, turn_number, attempt_index) carries different content (D4)",
                        detail={
                            "run_id": tc.run_id,
                            "turn_number": tc.turn_number,
                            "attempt_index": tc.attempt_index,
                        },
                    )
                return TurnCycleId(existing_id)  # idempotent no-op (D4)

            if tc.is_authoritative:
                clash = conn.execute(
                    "SELECT turn_cycle_id FROM turn_cycles "
                    "WHERE run_id = ? AND turn_number = ? AND is_authoritative = 1",
                    (tc.run_id, tc.turn_number),
                ).fetchone()
                if clash is not None:
                    raise StoreWriteError(
                        "write_turn_cycle: another attempt is already authoritative for "
                        "this (run_id, turn_number); call mark_turn_superseded on it first "
                        "(FR-047, invariant I9) -- this also guards parent-immutability, "
                        "since an already-written attempt (a branch's parent turn included) "
                        "can never simply be overwritten (FR-034, I12)",
                        detail={
                            "run_id": tc.run_id,
                            "turn_number": tc.turn_number,
                            "existing_turn_cycle_id": clash[0],
                        },
                    )

            run_row = conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (tc.run_id,)
            ).fetchone()
            if run_row is None:
                raise StoreWriteError(
                    "write_turn_cycle: no such run", detail={"run_id": tc.run_id}
                )

            conn.execute(
                "INSERT INTO turn_cycles (turn_cycle_id, run_id, turn_number, attempt_index, "
                "is_authoritative, step_count, turn_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tc.turn_cycle_id,
                    tc.run_id,
                    tc.turn_number,
                    tc.attempt_index,
                    int(tc.is_authoritative),
                    tc.step_count,
                    tc.model_dump_json(),
                ),
            )
            for bundle in record.steps:
                conn.execute(
                    "INSERT INTO decision_steps (decision_step_id, turn_cycle_id, step_index, "
                    "bundle_json) VALUES (?, ?, ?, ?)",
                    (
                        bundle.step.decision_step_id,
                        tc.turn_cycle_id,
                        bundle.step.step_index,
                        bundle.model_dump_json(),
                    ),
                )
            return tc.turn_cycle_id

        return self._run_in_transaction(body)

    def write_run_event(self, event: RunEvent) -> EventId:
        def body(conn: sqlite3.Connection) -> EventId:
            row = conn.execute(
                "SELECT event_json FROM run_events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if row is not None:
                if RunEvent.model_validate_json(row[0]) != event:
                    raise StoreWriteError(
                        "write_run_event: event_id reused with different content",
                        detail={"event_id": event.event_id},
                    )
                return event.event_id
            run_row = conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (event.run_id,)
            ).fetchone()
            if run_row is None:
                raise StoreWriteError(
                    "write_run_event: no such run", detail={"run_id": event.run_id}
                )
            conn.execute(
                "INSERT INTO run_events (event_id, run_id, occurred_at, event_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    event.event_id,
                    event.run_id,
                    event.occurred_at.isoformat(),
                    event.model_dump_json(),
                ),
            )
            return event.event_id

        return self._run_in_transaction(body)

    def write_model_call(self, call: ModelCall) -> ModelCallId:
        def body(conn: sqlite3.Connection) -> ModelCallId:
            row = conn.execute(
                "SELECT call_json FROM model_calls WHERE model_call_id = ?",
                (call.model_call_id,),
            ).fetchone()
            if row is not None:
                if ModelCall.model_validate_json(row[0]) != call:
                    raise StoreWriteError(
                        "write_model_call: model_call_id reused with different content",
                        detail={"model_call_id": call.model_call_id},
                    )
                return call.model_call_id
            conn.execute(
                "INSERT INTO model_calls (model_call_id, run_id, decision_step_id, call_json) "
                "VALUES (?, ?, ?, ?)",
                (call.model_call_id, call.run_id, call.decision_step_id, call.model_dump_json()),
            )
            return call.model_call_id

        return self._run_in_transaction(body)

    def write_save_point(self, save: SavePoint) -> SavePointId:
        if save.retention_status == RetentionStatus.ELIGIBLE:
            raise StoreWriteError(
                "write_save_point rejects retention_status=eligible outright: eligibility "
                "has exactly one cause, archive_run -- no other path may produce it "
                "(FR-036, A2, invariant I17)",
                detail={"save_point_id": save.save_point_id},
            )

        def body(conn: sqlite3.Connection) -> SavePointId:
            # A mutable resource (missing/retention_status evolve over a
            # save's lifecycle): an upsert, not an idempotent-insert-only
            # write like the immutable historical facts below.
            conn.execute(
                "INSERT INTO save_points (save_point_id, run_id, turn_number, "
                "retention_status, save_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(save_point_id) DO UPDATE SET "
                "run_id=excluded.run_id, turn_number=excluded.turn_number, "
                "retention_status=excluded.retention_status, save_json=excluded.save_json",
                (
                    save.save_point_id,
                    save.run_id,
                    save.turn_number,
                    save.retention_status.value,
                    save.model_dump_json(),
                ),
            )
            return save.save_point_id

        return self._run_in_transaction(body)

    def write_capture(self, capture: ScreenCapture, blob: bytes | None) -> CaptureId:
        digest: str | None = None
        if capture.screening_status == ScreeningStatus.SCREENED_CLEAN:
            if blob is None:
                raise StoreWriteError(
                    "write_capture: screening_status=screened_clean requires a blob -- "
                    "nothing a record depends on may exist only in local/ephemeral form (D5)",
                    detail={"capture_id": capture.capture_id},
                )
            digest = hashlib.sha256(blob).hexdigest()
            if capture.blob_ref is not None and capture.blob_ref != digest:
                raise StoreWriteError(
                    "write_capture: capture.blob_ref does not match the content hash of blob",
                    detail={
                        "capture_id": capture.capture_id,
                        "declared": capture.blob_ref,
                        "computed": digest,
                    },
                )
        elif blob is not None:
            raise StoreWriteError(
                "write_capture: a withheld capture must not carry a blob (SC-019)",
                detail={"capture_id": capture.capture_id},
            )

        def body(conn: sqlite3.Connection) -> CaptureId:
            row = conn.execute(
                "SELECT capture_json FROM captures WHERE capture_id = ?", (capture.capture_id,)
            ).fetchone()
            if row is not None:
                if ScreenCapture.model_validate_json(row[0]) != capture:
                    raise StoreWriteError(
                        "write_capture: capture_id reused with different content",
                        detail={"capture_id": capture.capture_id},
                    )
                return capture.capture_id
            if blob is not None and digest is not None:
                self._store_blob(digest, blob)
            conn.execute(
                "INSERT INTO captures (capture_id, run_id, decision_step_id, blob_ref, "
                "capture_json) VALUES (?, ?, ?, ?, ?)",
                (
                    capture.capture_id,
                    capture.run_id,
                    capture.decision_step_id,
                    capture.blob_ref,
                    capture.model_dump_json(),
                ),
            )
            return capture.capture_id

        return self._run_in_transaction(body)

    def mark_turn_superseded(self, run_id: RunId, turn: int, attempt: int) -> None:
        def body(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT turn_cycle_id, turn_json FROM turn_cycles "
                "WHERE run_id = ? AND turn_number = ? AND attempt_index = ?",
                (run_id, turn, attempt),
            ).fetchone()
            if row is None:
                raise StoreWriteError(
                    "mark_turn_superseded: no such (run_id, turn_number, attempt_index)",
                    detail={"run_id": run_id, "turn": turn, "attempt": attempt},
                )
            turn_cycle_id, turn_json = row
            existing_tc = TurnCycle.model_validate_json(turn_json)
            updated_tc = existing_tc.model_copy(
                update={"is_authoritative": False, "outcome": TurnOutcome.ABANDONED}
            )
            conn.execute(
                "UPDATE turn_cycles SET is_authoritative = 0, turn_json = ? "
                "WHERE turn_cycle_id = ?",
                (updated_tc.model_dump_json(), turn_cycle_id),
            )

        self._run_in_transaction(body)

    def archive_run(self, run_id: RunId, *, by: str, at: Timestamp) -> None:
        def body(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT run_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise StoreWriteError("archive_run: no such run", detail={"run_id": run_id})
            current = Run.model_validate_json(row[0])
            if current.archived_at is not None:
                return  # archival is monotonic: already archived is a no-op, not an error

            try:
                archived = Run.model_validate({**current.model_dump(), "archived_at": at})
            except ValidationError as exc:
                raise StoreWriteError(
                    "archive_run rejected: run is not in a terminal lifecycle state "
                    "(A3, FR-036)",
                    detail={"run_id": run_id, "error": str(exc)},
                ) from exc

            conn.execute(
                "UPDATE runs SET archived_at = ?, run_json = ? WHERE run_id = ?",
                (at.isoformat(), archived.model_dump_json(), run_id),
            )

            event = RunEvent(
                event_id=EventId(str(uuid.uuid4())),
                run_id=run_id,
                event_type=RunEventType.RUN_ARCHIVED,
                occurred_at=at,
                detail={"by": by},
            )
            conn.execute(
                "INSERT INTO run_events (event_id, run_id, occurred_at, event_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    event.event_id,
                    event.run_id,
                    event.occurred_at.isoformat(),
                    event.model_dump_json(),
                ),
            )

            # A1: transition this run's *retained* save points to eligible.
            # This UPDATE is the only place in this entire module that ever
            # writes retention_status='eligible' (A2, invariant I17) -- it
            # deliberately bypasses write_save_point, which rejects that
            # value from every other caller.
            sp_rows = conn.execute(
                "SELECT save_point_id, save_json FROM save_points "
                "WHERE run_id = ? AND retention_status = ?",
                (run_id, RetentionStatus.RETAINED.value),
            ).fetchall()
            for save_point_id, save_json in sp_rows:
                sp = SavePoint.model_validate_json(save_json)
                eligible = sp.model_copy(update={"retention_status": RetentionStatus.ELIGIBLE})
                conn.execute(
                    "UPDATE save_points SET retention_status = ?, save_json = ? "
                    "WHERE save_point_id = ?",
                    (RetentionStatus.ELIGIBLE.value, eligible.model_dump_json(), save_point_id),
                )

        self._run_in_transaction(body)

    # ----------------------------------------------------------------
    # Reads
    # ----------------------------------------------------------------

    def get_run(self, run_id: RunId) -> Run | None:
        def body(conn: sqlite3.Connection) -> Run | None:
            row = conn.execute(
                "SELECT run_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return Run.model_validate_json(row[0]) if row is not None else None

        return self._with_lock(body)

    def get_turn_cycle(
        self, run_id: RunId, turn: int, *, authoritative_only: bool = True
    ) -> TurnCycleRecord | None:
        def body(conn: sqlite3.Connection) -> TurnCycleRecord | None:
            if authoritative_only:
                row = conn.execute(
                    "SELECT turn_cycle_id FROM turn_cycles "
                    "WHERE run_id = ? AND turn_number = ? AND is_authoritative = 1",
                    (run_id, turn),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT turn_cycle_id FROM turn_cycles "
                    "WHERE run_id = ? AND turn_number = ? ORDER BY attempt_index DESC LIMIT 1",
                    (run_id, turn),
                ).fetchone()
            if row is None:
                return None
            return self._load_turn_cycle_record(conn, row[0])

        return self._with_lock(body)

    def list_save_points(self, run_id: RunId) -> list[SavePoint]:
        def body(conn: sqlite3.Connection) -> list[SavePoint]:
            rows = conn.execute(
                "SELECT save_json FROM save_points WHERE run_id = ? ORDER BY turn_number ASC",
                (run_id,),
            ).fetchall()
            return [SavePoint.model_validate_json(row[0]) for row in rows]

        return self._with_lock(body)

    def get_last_known_good(self, run_id: RunId) -> SavePoint | None:
        def body(conn: sqlite3.Connection) -> SavePoint | None:
            rows = conn.execute(
                "SELECT save_json FROM save_points WHERE run_id = ? ORDER BY turn_number DESC",
                (run_id,),
            ).fetchall()
            for (save_json,) in rows:
                sp = SavePoint.model_validate_json(save_json)
                if not sp.missing and sp.retention_status != RetentionStatus.REMOVED:
                    return sp
            return None

        return self._with_lock(body)

    def list_active_runs(self) -> list[Run]:
        def body(conn: sqlite3.Connection) -> list[Run]:
            rows = conn.execute("SELECT run_json FROM runs").fetchall()
            runs = [Run.model_validate_json(row[0]) for row in rows]
            return [r for r in runs if r.lifecycle_state not in _TERMINAL_LIFECYCLE_STATES]

        return self._with_lock(body)

    def turn_gaps(self, run_id: RunId) -> list[int]:
        def body(conn: sqlite3.Connection) -> list[int]:
            rows = conn.execute(
                "SELECT turn_number FROM turn_cycles WHERE run_id = ? AND is_authoritative = 1",
                (run_id,),
            ).fetchall()
            present = {row[0] for row in rows}
            if not present:
                return []
            return sorted(set(range(1, max(present) + 1)) - present)

        return self._with_lock(body)

    def step_gaps(self, run_id: RunId, turn: int) -> list[int]:
        def body(conn: sqlite3.Connection) -> list[int]:
            tc_row = conn.execute(
                "SELECT turn_cycle_id FROM turn_cycles "
                "WHERE run_id = ? AND turn_number = ? AND is_authoritative = 1",
                (run_id, turn),
            ).fetchone()
            if tc_row is None:
                return []
            rows = conn.execute(
                "SELECT step_index FROM decision_steps WHERE turn_cycle_id = ?",
                (tc_row[0],),
            ).fetchall()
            present = {row[0] for row in rows}
            if not present:
                return []
            return sorted(set(range(1, max(present) + 1)) - present)

        return self._with_lock(body)

    def list_eligible_save_points(self) -> list[SavePoint]:
        def body(conn: sqlite3.Connection) -> list[SavePoint]:
            rows = conn.execute(
                "SELECT save_json FROM save_points WHERE retention_status = ? "
                "ORDER BY run_id, turn_number",
                (RetentionStatus.ELIGIBLE.value,),
            ).fetchall()
            return [SavePoint.model_validate_json(row[0]) for row in rows]

        return self._with_lock(body)

    # ----------------------------------------------------------------
    # Health
    # ----------------------------------------------------------------

    def ping(self) -> StoreHealth:
        now = datetime.now(UTC)
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return StoreHealth(ok=True, checked_at=now)
        except sqlite3.Error as exc:
            return StoreHealth(ok=False, checked_at=now, detail=redact_text(str(exc)))
