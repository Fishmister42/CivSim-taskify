"""The SQLite + content-addressed blob reference adapter (002 T039, T040, T168; 003 W1-W5).

Implements ``civsim_harness.store.port.MatchStore`` -- and, since deliverable 3, the wider
``civsim_harness.store.contract.MatchTrackingStore`` (``specs/003-match-tracking-store/
contracts/match-tracking-store.md``). It is a storage *implementation* of the port, not a second
bypassing path: every write the harness needs goes through the same nine methods, and nothing
here writes a record through a route the port does not expose.

Durability strategy (D1, D3, D6): a single persistent `sqlite3` connection, opened once with
`journal_mode=WAL` and `synchronous=FULL` -- the strongest durability pragma SQLite offers, so a
committed transaction is fsynced before the write call returns. Every write runs inside an
explicit `BEGIN IMMEDIATE ... COMMIT` transaction; any exception rolls the transaction back and
re-raises as `StoreWriteError`, so a write is never half-applied and never returns without either
succeeding durably or raising (D2, D3). A single `threading.RLock` serialises all access to the
one connection.

Deliverable 3 additions, each in the **same transaction** as the write it belongs to:

- ``write_turn_cycle`` records every step's ``ModelCall`` as a ``model_calls`` row (W1, W2,
  research R3) -- the rows the 2026-09-21 file was missing.
- Every write that can change a run's completeness re-derives and persists it
  (``_persist_completeness``, W3, FR-010) with ``store/completeness.py``'s rules.
- ``create_run`` / ``update_run`` / ``archive_run`` keep the indexed projection columns in
  step with the authoritative JSON (data-model.md SS2, I-A).
- ``import_run`` is the one privileged, verbatim write (W4, plan Complexity Tracking C1).

Archival (T040, FR-036, A1-A4): `archive_run` is the *only* method in this module that ever
writes `retention_status = 'eligible'` into `save_points`, or `archived_at` into `runs`, **for a
run that lives here** -- `import_run` reproduces those values verbatim for a run that was archived
elsewhere, which is not a new archival decision. `write_save_point` rejects `retention_status =
eligible` unconditionally, from any caller, and `update_run` rejects any attempt to set
`archived_at`. There is no TTL, no age check, no quota, no retention window, and no thinning
anywhere in this file.

Parent immutability (T168, FR-034, invariant I12): `write_turn_cycle` refuses to add a second
authoritative attempt at a turn number that already has one, regardless of whose write it is.
`mark_turn_superseded` additionally refuses to strip authoritative status from a turn that some
child run has recorded as its lineage point. `write_save_point` refuses to reassign an existing
`save_point_id` to a different `run_id`. The port has no delete operation on any turn or save
record at all, and none is added here.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from typing import Any

from pydantic import ValidationError

from civsim_harness.errors import BundleError, StoreWriteError
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
from civsim_harness.models.run import Run
from civsim_harness.models.turn import ScreenCapture, ScreeningStatus, TurnCycle, TurnOutcome
from civsim_harness.store.contract import RunRecordSet
from civsim_harness.store.port import TurnCycleRecord
from civsim_harness.store.schema import run_projection
from civsim_harness.store.sqlite_reads import SqliteReadBase


class SqliteMatchStore(SqliteReadBase):
    """SQLite + content-addressed blob `MatchStore` / `MatchTrackingStore` reference adapter.

    `db_path` is the SQLite database file (created, along with its parent directory, if
    absent; migrated from schema 1.0 with a copy taken first -- `store/schema.py`).
    `blob_dir` defaults to a `blobs/` sibling of `db_path` and holds capture image bytes,
    addressed by the sha256 of their content (`blob_dir/<hash[:2]>/<hash>`), written via a
    temp-file-then-`os.replace` sequence with an explicit `fsync` so a blob is durable on disk
    before the row referencing it is committed. `read_only=True` opens the file for readers
    (the web interface): every write raises, and a 1.0 file is refused rather than migrated.
    """

    # ----------------------------------------------------------------
    # Blob storage (content-addressed)
    # ----------------------------------------------------------------

    def _store_blob(self, digest: str, blob: bytes) -> None:
        final_path = self._blob_path(digest)
        subdir = final_path.parent
        subdir.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            return  # content-addressed: identical content is already durable
        tmp_path = subdir / f".{digest}.tmp-{uuid.uuid4().hex}"
        with open(tmp_path, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, final_path)

    # ----------------------------------------------------------------
    # Row helpers
    # ----------------------------------------------------------------

    @staticmethod
    def _write_run_row(
        conn: sqlite3.Connection, run: Run, *, config_json: str | None = None
    ) -> None:
        """Rewrite a run's authoritative JSON and every projection column from it (I-A)."""
        run_json = run.model_dump_json()
        stored_config = config_json
        if stored_config is None:
            row = conn.execute(
                "SELECT config_json FROM runs WHERE run_id = ?", (run.run_id,)
            ).fetchone()
            if row is None:
                raise StoreWriteError("no such run", detail={"run_id": run.run_id})
            stored_config = str(row[0])
        started_at, ended_at, seed_set_id, completeness, comparability = run_projection(
            run_json, stored_config
        )
        conn.execute(
            "UPDATE runs SET config_id=?, lifecycle_state=?, parent_run_id=?, archived_at=?, "
            "run_json=?, started_at=?, ended_at=?, seed_set_id=?, "
            "record_completeness_status=?, comparability_status=? WHERE run_id=?",
            (
                run.config_id,
                run.lifecycle_state.value,
                run.parent_run_id,
                run.archived_at.isoformat() if run.archived_at else None,
                run_json,
                started_at,
                ended_at,
                seed_set_id,
                completeness,
                comparability,
                run.run_id,
            ),
        )

    def _persist_completeness(self, conn: sqlite3.Connection, run_id: str) -> None:
        """W3: re-derive this run's completeness and persist it when it changed."""
        run = self._get_run_body(conn, run_id)
        if run is None:
            return
        status = self._derive_completeness_body(conn, run_id)
        if run.record_completeness_status is status:
            return
        self._write_run_row(conn, run.model_copy(update={"record_completeness_status": status}))

    @staticmethod
    def _insert_model_call_row(
        conn: sqlite3.Connection,
        call: ModelCall,
        *,
        turn_number: int | None,
        step_index: int | None,
    ) -> None:
        """One ``model_calls`` row, idempotent by id with content checked (W2, D4)."""
        row = conn.execute(
            "SELECT call_json FROM model_calls WHERE model_call_id = ?", (call.model_call_id,)
        ).fetchone()
        if row is not None:
            if ModelCall.model_validate_json(row[0]) != call:
                raise StoreWriteError(
                    "model_call_id reused with different content (W2, D4)",
                    detail={"model_call_id": call.model_call_id},
                )
            if turn_number is not None:
                conn.execute(
                    "UPDATE model_calls SET turn_number = ?, step_index = ? "
                    "WHERE model_call_id = ? AND turn_number IS NULL",
                    (turn_number, step_index, call.model_call_id),
                )
            return
        conn.execute(
            "INSERT INTO model_calls (model_call_id, run_id, decision_step_id, call_json, "
            "turn_number, step_index) VALUES (?, ?, ?, ?, ?, ?)",
            (
                call.model_call_id,
                call.run_id,
                call.decision_step_id,
                call.model_dump_json(),
                turn_number,
                step_index,
            ),
        )

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
            run_json = run.model_dump_json()
            config_json = config.model_dump_json(by_alias=True)
            started_at, ended_at, seed_set_id, completeness, comparability = run_projection(
                run_json, config_json
            )
            conn.execute(
                "INSERT INTO runs (run_id, config_id, lifecycle_state, parent_run_id, "
                "archived_at, run_json, config_json, started_at, ended_at, seed_set_id, "
                "record_completeness_status, comparability_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.config_id,
                    run.lifecycle_state.value,
                    run.parent_run_id,
                    run.archived_at.isoformat() if run.archived_at else None,
                    run_json,
                    config_json,
                    started_at,
                    ended_at,
                    seed_set_id,
                    completeness,
                    comparability,
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
            current = self._get_run_body(conn, run_id)
            if current is None:
                raise StoreWriteError("update_run: no such run", detail={"run_id": run_id})
            merged = current.model_dump()
            merged.update(fields)
            try:
                updated = Run.model_validate(merged)
            except ValidationError as exc:
                raise StoreWriteError(
                    "update_run produced an invalid Run",
                    detail={"run_id": run_id, "error": str(exc)},
                ) from exc
            self._write_run_row(conn, updated)
            if "lifecycle_state" in fields:
                # A run that stops advancing may have just turned a trailing quicksave into
                # a gap (contracts/match-store-port.md, turn_gaps) -- the store says so now.
                self._persist_completeness(conn, run_id)

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

            run_row = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (tc.run_id,)).fetchone()
            if run_row is None:
                raise StoreWriteError("write_turn_cycle: no such run", detail={"run_id": tc.run_id})

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
                # W1 (003 FR-008): the call that produced this step is a row of its own, in
                # this same transaction -- the turn and its calls are durable together.
                self._insert_model_call_row(
                    conn,
                    bundle.model_call,
                    turn_number=tc.turn_number,
                    step_index=bundle.step.step_index,
                )
            self._persist_completeness(conn, tc.run_id)
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
            turn_row = conn.execute(
                "SELECT turn_number FROM turn_cycles WHERE turn_cycle_id = ?",
                (call.turn_cycle_id,),
            ).fetchone()
            step_row = conn.execute(
                "SELECT step_index FROM decision_steps WHERE decision_step_id = ?",
                (call.decision_step_id,),
            ).fetchone()
            self._insert_model_call_row(
                conn,
                call,
                turn_number=int(turn_row[0]) if turn_row is not None else None,
                step_index=int(step_row[0]) if step_row is not None else None,
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
            # Parent immutability (FR-034, invariant I12): a save_point_id is an identity,
            # not a slot to repoint. Rejecting a write that would reassign an existing save
            # point to a different run_id closes the one path by which a write could
            # otherwise reach into another run's -- including a parent's -- records under
            # cover of this method's ordinary (and otherwise legitimate) upsert semantics.
            existing_owner = conn.execute(
                "SELECT run_id FROM save_points WHERE save_point_id = ?",
                (save.save_point_id,),
            ).fetchone()
            if existing_owner is not None and existing_owner[0] != save.run_id:
                raise StoreWriteError(
                    "write_save_point rejected: save_point_id already belongs to a "
                    "different run_id -- a write may never reassign an existing save "
                    "point's ownership (FR-034, invariant I12)",
                    detail={
                        "save_point_id": save.save_point_id,
                        "existing_run_id": existing_owner[0],
                        "attempted_run_id": save.run_id,
                    },
                )

            # A mutable resource (missing/retention_status evolve over a save's lifecycle):
            # an upsert, not an idempotent-insert-only write like the immutable facts.
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
            self._persist_completeness(conn, save.run_id)
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
            # Parent immutability (FR-034, invariant I12): a turn recorded as some child
            # run's lineage point (child.parent_run_id == run_id and child.parent_turn ==
            # turn) may never be stripped of its authoritative status. Without this guard,
            # mark_turn_superseded could un-authoritative the exact turn a branch claims to
            # have started from, and a *new* authoritative write_turn_cycle at that same
            # turn number would then slip past write_turn_cycle's clash check -- silently
            # changing what the branch's parent position was.
            child_rows = conn.execute(
                "SELECT run_id, run_json FROM runs WHERE parent_run_id = ?", (run_id,)
            ).fetchall()
            for child_run_id, child_json in child_rows:
                child = Run.model_validate_json(child_json)
                if child.parent_turn == turn:
                    raise StoreWriteError(
                        "mark_turn_superseded rejected: this turn is the lineage point of "
                        "an existing branch and its parent's record may never be modified "
                        "or invalidated (FR-034, invariant I12)",
                        detail={"run_id": run_id, "turn": turn, "child_run_id": child_run_id},
                    )

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
            self._persist_completeness(conn, run_id)

        self._run_in_transaction(body)

    def archive_run(self, run_id: RunId, *, by: str, at: Timestamp) -> None:
        def body(conn: sqlite3.Connection) -> None:
            current = self._get_run_body(conn, run_id)
            if current is None:
                raise StoreWriteError("archive_run: no such run", detail={"run_id": run_id})
            if current.archived_at is not None:
                return  # archival is monotonic: already archived is a no-op, not an error

            try:
                archived = Run.model_validate({**current.model_dump(), "archived_at": at})
            except ValidationError as exc:
                raise StoreWriteError(
                    "archive_run rejected: run is not in a terminal lifecycle state (A3, FR-036)",
                    detail={"run_id": run_id, "error": str(exc)},
                ) from exc

            self._write_run_row(conn, archived)

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

            # A1: transition this run's *retained* save points to eligible. This UPDATE is
            # the only place in this module that ever writes retention_status='eligible' for
            # a run archived *here* (A2, invariant I17) -- it deliberately bypasses
            # write_save_point, which rejects that value from every other caller.
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
    # W4: the privileged verbatim write
    # ----------------------------------------------------------------

    def import_run(self, records: RunRecordSet) -> RunId:
        run_id = records.run.run_id

        # Closure checks first, outside the transaction: a set that cannot be imported
        # whole is refused before a byte is written (V6).
        for record in records.turn_cycles:
            if record.turn_cycle.run_id != run_id:
                raise BundleError(
                    "import_run: a turn cycle belongs to a different run",
                    detail={"run_id": run_id, "turn_cycle_id": record.turn_cycle.turn_cycle_id},
                )
        for owned, kind in (
            (records.run_events, "run_event"),
            (records.model_calls, "model_call"),
            (records.save_points, "save_point"),
            (records.captures, "capture"),
        ):
            for item in owned:
                if item.run_id != run_id:
                    raise BundleError(
                        f"import_run: a {kind} belongs to a different run",
                        detail={"run_id": run_id, "other_run_id": item.run_id},
                    )
        if records.configuration.config_id != records.run.config_id:
            raise BundleError(
                "import_run: configuration does not match the run's config_id",
                detail={"run_id": run_id},
            )
        for capture in records.captures:
            if capture.screening_status is ScreeningStatus.SCREENED_CLEAN:
                content = records.images.get(capture.blob_ref or "")
                if content is None:
                    raise BundleError(
                        "import_run: a kept capture's image is not in the record set",
                        detail={"capture_id": capture.capture_id, "blob_ref": capture.blob_ref},
                    )
                if hashlib.sha256(content).hexdigest() != capture.blob_ref:
                    raise BundleError(
                        "import_run: image bytes do not hash to the capture's blob_ref",
                        detail={"capture_id": capture.capture_id, "blob_ref": capture.blob_ref},
                    )

        def body(conn: sqlite3.Connection) -> RunId:
            if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone():
                raise BundleError(
                    "import_run refused: a run with this id already exists in this store; "
                    "a bundle is never merged and never renamed (FR-027)",
                    detail={"run_id": run_id},
                )
            for digest, content in records.images.items():
                self._store_blob(digest, content)

            run_json = records.run.model_dump_json()
            config_json = records.configuration.model_dump_json(by_alias=True)
            started_at, ended_at, seed_set_id, completeness, comparability = run_projection(
                run_json, config_json
            )
            conn.execute(
                "INSERT INTO runs (run_id, config_id, lifecycle_state, parent_run_id, "
                "archived_at, run_json, config_json, started_at, ended_at, seed_set_id, "
                "record_completeness_status, comparability_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    records.run.config_id,
                    records.run.lifecycle_state.value,
                    records.run.parent_run_id,
                    records.run.archived_at.isoformat() if records.run.archived_at else None,
                    run_json,
                    config_json,
                    started_at,
                    ended_at,
                    seed_set_id,
                    completeness,
                    comparability,
                ),
            )
            for record in records.turn_cycles:
                tc = record.turn_cycle
                conn.execute(
                    "INSERT INTO turn_cycles (turn_cycle_id, run_id, turn_number, "
                    "attempt_index, is_authoritative, step_count, turn_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
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
                        "INSERT INTO decision_steps (decision_step_id, turn_cycle_id, "
                        "step_index, bundle_json) VALUES (?, ?, ?, ?)",
                        (
                            bundle.step.decision_step_id,
                            tc.turn_cycle_id,
                            bundle.step.step_index,
                            bundle.model_dump_json(),
                        ),
                    )
            for event in records.run_events:
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
            for call in records.model_calls:
                turn_row = conn.execute(
                    "SELECT turn_number FROM turn_cycles WHERE turn_cycle_id = ?",
                    (call.turn_cycle_id,),
                ).fetchone()
                step_row = conn.execute(
                    "SELECT step_index FROM decision_steps WHERE decision_step_id = ?",
                    (call.decision_step_id,),
                ).fetchone()
                self._insert_model_call_row(
                    conn,
                    call,
                    turn_number=int(turn_row[0]) if turn_row is not None else None,
                    step_index=int(step_row[0]) if step_row is not None else None,
                )
            # W1 holds for imported turns too: every embedded call has a row, even when the
            # exporting store predates the rows.
            for record in records.turn_cycles:
                for bundle in record.steps:
                    self._insert_model_call_row(
                        conn,
                        bundle.model_call,
                        turn_number=record.turn_cycle.turn_number,
                        step_index=bundle.step.step_index,
                    )
            for save in records.save_points:
                conn.execute(
                    "INSERT INTO save_points (save_point_id, run_id, turn_number, "
                    "retention_status, save_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        save.save_point_id,
                        save.run_id,
                        save.turn_number,
                        save.retention_status.value,
                        save.model_dump_json(),
                    ),
                )
            for capture in records.captures:
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
            return run_id

        try:
            return self._run_in_transaction(body)
        except StoreWriteError as exc:
            cause = exc.__cause__
            if isinstance(cause, BundleError):
                raise cause from exc
            raise


__all__ = ["SqliteMatchStore"]
