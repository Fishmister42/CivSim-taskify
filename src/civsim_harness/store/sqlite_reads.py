"""The read half of the SQLite store: connection, transaction helpers, the private
``_…_body`` reads every write transaction reuses, and every read deliverable 3 publishes
(``contracts/match-tracking-store.md`` R1-R8, T1-T4, V4; ``store/contract.py``).

:class:`SqliteReadBase` is the base of ``sqlite_adapter.SqliteMatchStore``. Splitting the file
this way keeps the 002 write path readable in one module while the 003 reads live here, and it
makes one thing structural: a **read-only** store (``read_only=True``, research R10) is this class
plus nothing -- every write in the subclass refuses before it touches the connection.

Reads that run *inside* a write transaction (completeness derivation, the clash checks) call the
``_…_body(conn, …)`` functions directly on the transaction's connection; the public methods wrap
the same bodies in the lock. The lock is an ``RLock`` so a body may call a body.

**The blob directory is reached from exactly one place**, :meth:`get_capture_image` (R8): nothing
else in the store, and nothing outside it, resolves a ``blob_ref`` to a path.
"""

from __future__ import annotations

import json
import platform
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civsim_harness.errors import StoreReadError, StoreWriteError
from civsim_harness.models.common import CaptureId, RunId
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.records import (
    CallOutcome,
    ModelCall,
    RetentionStatus,
    RunEvent,
    RunEventType,
    SavePoint,
)
from civsim_harness.models.run import LifecycleState, RecordCompletenessStatus, Run
from civsim_harness.models.turn import Observation, ScreenCapture, ScreeningStatus, TurnCycle
from civsim_harness.store import schema as store_schema
from civsim_harness.store.completeness import first_owed_turn
from civsim_harness.store.contract import (
    CaptureImage,
    CaptureImageStatus,
    DivergenceReport,
    ExcludedRun,
    ExclusionReason,
    MetricPoint,
    MetricSeries,
    MigrationRecord,
    ModelCallRow,
    ModelCallTotals,
    RunPage,
    RunQuery,
    RunRecordSet,
    RunSort,
    StoreInfo,
    StoreSchemaVersion,
    TrendQuery,
    TrendResponse,
    TurnAttemptSummary,
)
from civsim_harness.store.port import DecisionStepBundle, StoreHealth, TurnCycleRecord
from civsim_harness.store.trends import (
    exclusion_for,
    first_divergence,
    turn_fingerprint,
    turn_metrics_from,
)
from civsim_harness.telemetry.redaction import redact_text

_TERMINAL_LIFECYCLE_STATES = frozenset({LifecycleState.FINISHED, LifecycleState.FAILED})

#: The lifecycle states in which a run is actively cycling through its own turn loop -- see
#: ``turn_gaps`` below and contracts/match-store-port.md's ``turn_gaps`` row: a trailing
#: quicksave with no ``TurnCycle`` behind it is the normal in-flight shape here and a gap
#: everywhere else.
_ACTIVELY_PLAYING_LIFECYCLE_STATES = frozenset(
    {LifecycleState.PLAYING, LifecycleState.WAITING_ON_MODEL, LifecycleState.WAITING_ON_GAME}
)
_ACTIVELY_PLAYING_LIFECYCLE_STATE_VALUES = frozenset(
    state.value for state in _ACTIVELY_PLAYING_LIFECYCLE_STATES
)

_ORDER_BY: dict[RunSort, str] = {
    RunSort.STARTED_AT_DESC: "started_at IS NULL, started_at DESC, run_id ASC",
    RunSort.STARTED_AT_ASC: "started_at IS NULL, started_at ASC, run_id ASC",
    RunSort.ENDED_AT_DESC: "ended_at IS NULL, ended_at DESC, run_id ASC",
    RunSort.RUN_ID_ASC: "run_id ASC",
}

_COUNTED_TABLES: tuple[str, ...] = (
    "runs",
    "turn_cycles",
    "decision_steps",
    "run_events",
    "model_calls",
    "save_points",
    "captures",
)


class SqliteReadBase:
    """Connection ownership, transaction helpers and every published read."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        blob_dir: str | Path | None = None,
        read_only: bool = False,
        host_platform: str | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._read_only = read_only
        self._host_platform = host_platform or platform.system() or "unknown"
        self._blob_dir = Path(blob_dir) if blob_dir is not None else self._db_path.parent / "blobs"
        if not read_only:
            self._blob_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn, self._schema_version, self._migrations_applied = store_schema.open_store_file(
            self._db_path, read_only=read_only, host_platform=self._host_platform
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def schema_version(self) -> StoreSchemaVersion:
        return self._schema_version

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def migrations_applied_on_open(self) -> tuple[MigrationRecord, ...]:
        """The migrations *this* opening performed (empty unless a 1.0 file was migrated)."""
        return self._migrations_applied

    # ----------------------------------------------------------------
    # Transaction helpers
    # ----------------------------------------------------------------

    def _run_in_transaction[T](self, body: Callable[[sqlite3.Connection], T]) -> T:
        """Run *body* inside one atomic, durable transaction (D1-D3).

        Commits only if *body* returns normally; any exception rolls the transaction back and
        is re-raised as a `StoreWriteError` (unless it already is one), so a caller can never
        observe a write as having both partially applied and returned successfully (D2). A
        read-only store refuses before touching the connection (W5).
        """
        if self._read_only:
            raise StoreWriteError(
                "match store was opened read-only; no write is permitted through it (W5)",
                detail={"path": str(self._db_path)},
            )
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
    # Blob addressing -- the one place a blob_ref becomes a path (R8)
    # ----------------------------------------------------------------

    def _blob_path(self, digest: str) -> Path:
        return self._blob_dir / digest[:2] / digest

    # ----------------------------------------------------------------
    # Private bodies -- callable inside a transaction on the same connection
    # ----------------------------------------------------------------

    def _load_turn_cycle_record(
        self, conn: sqlite3.Connection, turn_cycle_id: str
    ) -> TurnCycleRecord:
        row = conn.execute(
            "SELECT turn_json FROM turn_cycles WHERE turn_cycle_id = ?", (turn_cycle_id,)
        ).fetchone()
        if row is None:
            raise StoreWriteError("no such turn_cycle_id", detail={"turn_cycle_id": turn_cycle_id})
        turn_cycle = TurnCycle.model_validate_json(row[0])
        step_rows = conn.execute(
            "SELECT bundle_json FROM decision_steps WHERE turn_cycle_id = ? "
            "ORDER BY step_index ASC",
            (turn_cycle_id,),
        ).fetchall()
        steps = [DecisionStepBundle.model_validate_json(step_row[0]) for step_row in step_rows]
        return TurnCycleRecord(turn_cycle=turn_cycle, steps=steps)

    def _get_run_body(self, conn: sqlite3.Connection, run_id: str) -> Run | None:
        row = conn.execute("SELECT run_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return Run.model_validate_json(row[0]) if row is not None else None

    def _get_run_configuration_body(
        self, conn: sqlite3.Connection, run_id: str
    ) -> RunConfiguration | None:
        row = conn.execute("SELECT config_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return RunConfiguration.model_validate_json(row[0]) if row is not None else None

    def _authoritative_turn_cycle_id(
        self, conn: sqlite3.Connection, run_id: str, turn: int
    ) -> str | None:
        row = conn.execute(
            "SELECT turn_cycle_id FROM turn_cycles "
            "WHERE run_id = ? AND turn_number = ? AND is_authoritative = 1",
            (run_id, turn),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def _list_save_points_body(self, conn: sqlite3.Connection, run_id: str) -> list[SavePoint]:
        rows = conn.execute(
            "SELECT save_json FROM save_points WHERE run_id = ? ORDER BY turn_number ASC",
            (run_id,),
        ).fetchall()
        return [SavePoint.model_validate_json(row[0]) for row in rows]

    def _turn_gaps_body(self, conn: sqlite3.Connection, run_id: str) -> list[int]:
        rows = conn.execute(
            "SELECT turn_number FROM turn_cycles WHERE run_id = ? AND is_authoritative = 1",
            (run_id,),
        ).fetchall()
        present = {row[0] for row in rows}
        highest_recorded = max(present) if present else 0

        # A turn that was attempted -- it has an FR-007 quicksave -- but never produced a
        # TurnCycle at all is invisible to `present` above. That is exactly right while the run
        # is actively playing: the quicksave for the turn in progress legitimately lands before
        # that turn's TurnCycle does (FR-007). Once the run has *stopped* advancing -- paused,
        # interrupted, resuming, or terminal -- a trailing attempted turn with no record is a
        # genuine gap Principle III requires this method to surface, so the checked range is
        # extended up to the highest *attempted* turn (from save_points).
        run_row = conn.execute(
            "SELECT lifecycle_state FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is not None and run_row[0] not in _ACTIVELY_PLAYING_LIFECYCLE_STATE_VALUES:
            attempted_row = conn.execute(
                "SELECT MAX(turn_number) FROM save_points WHERE run_id = ?", (run_id,)
            ).fetchone()
            highest_attempted = attempted_row[0] if attempted_row is not None else None
            if highest_attempted is not None and highest_attempted > highest_recorded:
                highest_recorded = highest_attempted

        if highest_recorded == 0:
            return []
        return sorted(set(range(1, highest_recorded + 1)) - present)

    def _step_gaps_body(self, conn: sqlite3.Connection, run_id: str, turn: int) -> list[int]:
        turn_cycle_id = self._authoritative_turn_cycle_id(conn, run_id, turn)
        if turn_cycle_id is None:
            return []
        rows = conn.execute(
            "SELECT step_index FROM decision_steps WHERE turn_cycle_id = ?", (turn_cycle_id,)
        ).fetchall()
        present = {row[0] for row in rows}
        if not present:
            return []
        return sorted(set(range(1, max(present) + 1)) - present)

    def _highest_recorded_turn_body(self, conn: sqlite3.Connection, run_id: str) -> int:
        row = conn.execute(
            "SELECT MAX(turn_number) FROM ("
            "SELECT turn_number FROM turn_cycles WHERE run_id = ? "
            "UNION ALL SELECT turn_number FROM save_points WHERE run_id = ?)",
            (run_id, run_id),
        ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    def _derive_completeness_body(
        self, conn: sqlite3.Connection, run_id: str
    ) -> RecordCompletenessStatus:
        """``store/completeness.py``'s rules, on this connection (W3, FR-010).

        One definition, two call sites (plan Complexity Tracking C2): the harness's
        ``refresh_run_completeness`` derives through the port's public reads; the store derives
        here inside its own write transactions. Same save-point floor, same branch floor, same
        turn-then-step order.
        """
        attempted_row = conn.execute(
            "SELECT COUNT(*), MAX(turn_number) FROM save_points WHERE run_id = ?", (run_id,)
        ).fetchone()
        if attempted_row is None or not attempted_row[0]:
            return RecordCompletenessStatus.UNKNOWN
        floor = first_owed_turn(self._get_run_body(conn, run_id))
        if any(gap >= floor for gap in self._turn_gaps_body(conn, run_id)):
            return RecordCompletenessStatus.HAS_GAPS
        # Step gaps, in one query rather than one `step_gaps` per turn: an authoritative
        # attempt whose recorded step indices are not exactly 1..max has a hole (SC-003).
        # Same answer `_step_gaps_body` gives, without an O(turns) round trip per write.
        holed = conn.execute(
            "SELECT tc.turn_number FROM turn_cycles AS tc "
            "LEFT JOIN decision_steps AS ds ON ds.turn_cycle_id = tc.turn_cycle_id "
            "WHERE tc.run_id = ? AND tc.is_authoritative = 1 AND tc.turn_number >= ? "
            "GROUP BY tc.turn_cycle_id "
            "HAVING COUNT(ds.step_index) > 0 AND COUNT(ds.step_index) <> MAX(ds.step_index) "
            "LIMIT 1",
            (run_id, floor),
        ).fetchone()
        if holed is not None:
            return RecordCompletenessStatus.HAS_GAPS
        return RecordCompletenessStatus.COMPLETE

    def _capture_image_body(self, conn: sqlite3.Connection, capture_id: str) -> CaptureImage:
        row = conn.execute(
            "SELECT capture_json FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        if row is None:
            return CaptureImage(
                status=CaptureImageStatus.NO_SUCH_CAPTURE, capture_id=CaptureId(capture_id)
            )
        capture = ScreenCapture.model_validate_json(row[0])
        if capture.screening_status is ScreeningStatus.WITHHELD or capture.blob_ref is None:
            return CaptureImage(
                status=CaptureImageStatus.WITHHELD,
                capture_id=capture.capture_id,
                withheld_reason=capture.withheld_reason,
            )
        path = self._blob_path(capture.blob_ref)
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            return CaptureImage(
                status=CaptureImageStatus.MISSING,
                capture_id=capture.capture_id,
                blob_ref=capture.blob_ref,
            )
        return CaptureImage(
            status=CaptureImageStatus.AVAILABLE,
            capture_id=capture.capture_id,
            content=content,
            blob_ref=capture.blob_ref,
        )

    def _last_observation(self, conn: sqlite3.Connection, turn_cycle_id: str) -> Observation | None:
        row = conn.execute(
            "SELECT bundle_json FROM decision_steps WHERE turn_cycle_id = ? "
            "ORDER BY step_index DESC LIMIT 1",
            (turn_cycle_id,),
        ).fetchone()
        if row is None:
            return None
        return DecisionStepBundle.model_validate_json(row[0]).observation

    # ----------------------------------------------------------------
    # 002 reads that the write adapter re-exports (kept here so the
    # read-only store is complete without the subclass)
    # ----------------------------------------------------------------

    def get_run(self, run_id: RunId) -> Run | None:
        return self._with_lock(lambda conn: self._get_run_body(conn, run_id))

    def get_run_configuration(self, run_id: RunId) -> RunConfiguration | None:
        """FR-033 / E5: read back what `create_run` stored, keyed by ``run_id`` and nothing else.

        ``config_json`` is written ``by_alias=True`` (see ``create_run``), which is the wire
        shape ``RunConfiguration`` validates from, so this round-trips exactly what was
        persisted -- no re-derivation, no defaults applied on top of a stored value.
        """
        return self._with_lock(lambda conn: self._get_run_configuration_body(conn, run_id))

    def get_turn_cycle(
        self, run_id: RunId, turn: int, *, authoritative_only: bool = True
    ) -> TurnCycleRecord | None:
        def body(conn: sqlite3.Connection) -> TurnCycleRecord | None:
            if authoritative_only:
                turn_cycle_id = self._authoritative_turn_cycle_id(conn, run_id, turn)
            else:
                row = conn.execute(
                    "SELECT turn_cycle_id FROM turn_cycles "
                    "WHERE run_id = ? AND turn_number = ? ORDER BY attempt_index DESC LIMIT 1",
                    (run_id, turn),
                ).fetchone()
                turn_cycle_id = str(row[0]) if row is not None else None
            if turn_cycle_id is None:
                return None
            return self._load_turn_cycle_record(conn, turn_cycle_id)

        return self._with_lock(body)

    def list_save_points(self, run_id: RunId) -> list[SavePoint]:
        return self._with_lock(lambda conn: self._list_save_points_body(conn, run_id))

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
        return self._with_lock(lambda conn: self._turn_gaps_body(conn, run_id))

    def step_gaps(self, run_id: RunId, turn: int) -> list[int]:
        return self._with_lock(lambda conn: self._step_gaps_body(conn, run_id, turn))

    def list_eligible_save_points(self) -> list[SavePoint]:
        def body(conn: sqlite3.Connection) -> list[SavePoint]:
            rows = conn.execute(
                "SELECT save_json FROM save_points WHERE retention_status = ? "
                "ORDER BY run_id, turn_number",
                (RetentionStatus.ELIGIBLE.value,),
            ).fetchall()
            return [SavePoint.model_validate_json(row[0]) for row in rows]

        return self._with_lock(body)

    def get_capture(self, capture_id: CaptureId) -> ScreenCapture | None:
        def body(conn: sqlite3.Connection) -> ScreenCapture | None:
            row = conn.execute(
                "SELECT capture_json FROM captures WHERE capture_id = ?", (capture_id,)
            ).fetchone()
            return ScreenCapture.model_validate_json(row[0]) if row is not None else None

        return self._with_lock(body)

    def list_run_events(
        self, run_id: RunId, *, event_types: Sequence[RunEventType] | None = None
    ) -> list[RunEvent]:
        def body(conn: sqlite3.Connection) -> list[RunEvent]:
            rows = conn.execute(
                "SELECT event_json FROM run_events WHERE run_id = ? ORDER BY occurred_at ASC",
                (run_id,),
            ).fetchall()
            events = [RunEvent.model_validate_json(row[0]) for row in rows]
            if event_types is not None:
                allowed = set(event_types)
                events = [event for event in events if event.event_type in allowed]
            return events

        return self._with_lock(body)

    def ping(self) -> StoreHealth:
        now = datetime.now(UTC)
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return StoreHealth(ok=True, checked_at=now)
        except sqlite3.Error as exc:
            return StoreHealth(ok=False, checked_at=now, detail=redact_text(str(exc)))

    # ----------------------------------------------------------------
    # E1: the exact names the web interface probes for
    # ----------------------------------------------------------------

    def list_runs(self) -> list[Run]:
        """Every run, every state, archived included (R1)."""

        def body(conn: sqlite3.Connection) -> list[Run]:
            rows = conn.execute(
                f"SELECT run_json FROM runs ORDER BY {_ORDER_BY[RunSort.STARTED_AT_DESC]}"
            ).fetchall()
            return [Run.model_validate_json(row[0]) for row in rows]

        return self._with_lock(body)

    def get_capture_blob(self, capture_id: CaptureId) -> bytes | None:
        """E4: bytes only when the image is available; ``None`` otherwise."""
        return self.get_capture_image(capture_id).content

    def get_turn_cycle_attempt(
        self, run_id: RunId, turn: int, attempt: int
    ) -> TurnCycleRecord | None:
        def body(conn: sqlite3.Connection) -> TurnCycleRecord | None:
            row = conn.execute(
                "SELECT turn_cycle_id FROM turn_cycles "
                "WHERE run_id = ? AND turn_number = ? AND attempt_index = ?",
                (run_id, turn, attempt),
            ).fetchone()
            if row is None:
                return None
            return self._load_turn_cycle_record(conn, str(row[0]))

        return self._with_lock(body)

    # ----------------------------------------------------------------
    # US2: listing, attempts, images, model calls
    # ----------------------------------------------------------------

    def query_runs(self, query: RunQuery) -> RunPage:
        clauses: list[str] = []
        params: list[Any] = []
        if query.seed_set_id is not None:
            clauses.append("seed_set_id = ?")
            params.append(query.seed_set_id)
        for column, values in (
            ("lifecycle_state", query.lifecycle_states),
            ("record_completeness_status", query.completeness),
            ("comparability_status", query.comparability),
        ):
            if values is not None:
                members = sorted(str(value.value) for value in values)
                if not members:
                    clauses.append("0")
                    continue
                clauses.append(f"{column} IN ({', '.join('?' for _ in members)})")
                params.extend(members)
        if query.archived is True:
            clauses.append("archived_at IS NOT NULL")
        elif query.archived is False:
            clauses.append("archived_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        offset = (query.page - 1) * query.page_size

        def body(conn: sqlite3.Connection) -> RunPage:
            total_row = conn.execute(f"SELECT COUNT(*) FROM runs{where}", params).fetchone()
            total = int(total_row[0]) if total_row is not None else 0
            rows = conn.execute(
                f"SELECT run_json FROM runs{where} ORDER BY {_ORDER_BY[query.sort]} "
                "LIMIT ? OFFSET ?",
                (*params, query.page_size, offset),
            ).fetchall()
            return RunPage(
                runs=tuple(Run.model_validate_json(row[0]) for row in rows),
                total=total,
                page=query.page,
                page_size=query.page_size,
                sort=query.sort,
            )

        return self._with_lock(body)

    def highest_recorded_turn(self, run_id: RunId) -> int:
        return self._with_lock(lambda conn: self._highest_recorded_turn_body(conn, run_id))

    def list_turn_attempts(self, run_id: RunId, turn: int) -> list[TurnAttemptSummary]:
        def body(conn: sqlite3.Connection) -> list[TurnAttemptSummary]:
            rows = conn.execute(
                "SELECT turn_json FROM turn_cycles WHERE run_id = ? AND turn_number = ? "
                "ORDER BY attempt_index ASC",
                (run_id, turn),
            ).fetchall()
            summaries: list[TurnAttemptSummary] = []
            for (turn_json,) in rows:
                cycle = TurnCycle.model_validate_json(turn_json)
                summaries.append(
                    TurnAttemptSummary(
                        turn_number=cycle.turn_number,
                        attempt_index=cycle.attempt_index,
                        is_authoritative=cycle.is_authoritative,
                        outcome=cycle.outcome,
                        step_count=cycle.step_count,
                        turn_cycle_id=cycle.turn_cycle_id,
                    )
                )
            return summaries

        return self._with_lock(body)

    def get_capture_image(self, capture_id: CaptureId) -> CaptureImage:
        return self._with_lock(lambda conn: self._capture_image_body(conn, capture_id))

    def list_model_calls(
        self, run_id: RunId, *, turn: int | None = None, step: int | None = None
    ) -> list[ModelCallRow]:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if turn is not None:
            clauses.append("turn_number = ?")
            params.append(turn)
        if step is not None:
            clauses.append("step_index = ?")
            params.append(step)

        def body(conn: sqlite3.Connection) -> list[ModelCallRow]:
            rows = conn.execute(
                "SELECT call_json, turn_number, step_index FROM model_calls "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY turn_number IS NULL, turn_number ASC, step_index IS NULL, "
                "step_index ASC, model_call_id ASC",
                params,
            ).fetchall()
            return [
                ModelCallRow(
                    call=ModelCall.model_validate_json(call_json),
                    turn_number=turn_number,
                    step_index=step_index,
                )
                for call_json, turn_number, step_index in rows
            ]

        return self._with_lock(body)

    def model_call_totals(self, run_id: RunId) -> ModelCallTotals:
        def body(conn: sqlite3.Connection) -> ModelCallTotals:
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(json_extract(call_json, '$.cost.amount_usd') IS NOT NULL), "
                "SUM(json_extract(call_json, '$.cost.amount_usd')), "
                "SUM(json_extract(call_json, '$.cost.input_tokens')), "
                "SUM(json_extract(call_json, '$.cost.output_tokens')), "
                "SUM(json_extract(call_json, '$.cost.total_tokens')), "
                "SUM(json_extract(call_json, '$.latency_ms')), "
                "SUM(json_extract(call_json, '$.fallback_occurred')), "
                "SUM(json_extract(call_json, '$.retry_count')) "
                "FROM model_calls WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            outcome_rows = conn.execute(
                "SELECT json_extract(call_json, '$.outcome'), COUNT(*) FROM model_calls "
                "WHERE run_id = ? GROUP BY 1",
                (run_id,),
            ).fetchall()
            count = int(row[0] or 0)
            priced = int(row[1] or 0)
            return ModelCallTotals(
                run_id=run_id,
                call_count=count,
                priced_call_count=priced,
                cost_usd=float(row[2]) if priced else None,
                input_tokens=int(row[3]) if row[3] is not None else None,
                output_tokens=int(row[4]) if row[4] is not None else None,
                total_tokens=int(row[5]) if row[5] is not None else None,
                latency_ms_total=int(row[6] or 0),
                fallback_count=int(row[7] or 0),
                retry_count=int(row[8] or 0),
                calls_by_outcome={
                    CallOutcome(str(outcome)): int(n) for outcome, n in outcome_rows if outcome
                },
            )

        return self._with_lock(body)

    # ----------------------------------------------------------------
    # W3 / FR-010: completeness as the store accounts it
    # ----------------------------------------------------------------

    def record_completeness(self, run_id: RunId) -> RecordCompletenessStatus:
        return self._with_lock(lambda conn: self._derive_completeness_body(conn, run_id))

    # ----------------------------------------------------------------
    # US3: trends and divergence
    # ----------------------------------------------------------------

    def _resolve_trend_runs(
        self, conn: sqlite3.Connection, query: TrendQuery
    ) -> tuple[list[Run], list[ExcludedRun]]:
        runs: list[Run] = []
        excluded: list[ExcludedRun] = []
        if query.run_ids is not None:
            for run_id in query.run_ids:
                run = self._get_run_body(conn, run_id)
                if run is None:
                    excluded.append(
                        ExcludedRun(
                            run_id=run_id,
                            reason=ExclusionReason.NO_SUCH_RUN,
                            detail="no run with this id is recorded",
                        )
                    )
                else:
                    runs.append(run)
            return runs, excluded
        rows = conn.execute(
            f"SELECT run_json FROM runs WHERE seed_set_id = ? "
            f"ORDER BY {_ORDER_BY[RunSort.STARTED_AT_ASC]}",
            (query.seed_set_id,),
        ).fetchall()
        return [Run.model_validate_json(row[0]) for row in rows], excluded

    def _admit(
        self, conn: sqlite3.Connection, run: Run, *, include_visually_degraded: bool
    ) -> ExcludedRun | None:
        completeness = self._derive_completeness_body(conn, run.run_id)
        gaps = self._turn_gaps_body(conn, run.run_id)
        return exclusion_for(
            run, completeness, gaps, include_visually_degraded=include_visually_degraded
        )

    def _metrics_by_turn(self, conn: sqlite3.Connection, run: Run) -> dict[int, dict[str, float]]:
        """``{turn: metrics}`` over the run's authoritative turns from its owed floor (T2)."""
        rows = conn.execute(
            "SELECT turn_number, turn_cycle_id, turn_json FROM turn_cycles "
            "WHERE run_id = ? AND is_authoritative = 1 AND turn_number >= ? "
            "ORDER BY turn_number ASC",
            (run.run_id, first_owed_turn(run)),
        ).fetchall()
        collected: dict[int, dict[str, float]] = {}
        for turn_number, turn_cycle_id, turn_json in rows:
            yields = json.loads(turn_json).get("yields") or {}
            collected[int(turn_number)] = turn_metrics_from(
                yields, self._last_observation(conn, str(turn_cycle_id))
            )
        return collected

    def metric_series(self, query: TrendQuery) -> TrendResponse:
        def body(conn: sqlite3.Connection) -> TrendResponse:
            runs, excluded = self._resolve_trend_runs(conn, query)
            included: list[tuple[Run, dict[int, dict[str, float]]]] = []
            for run in runs:
                exclusion = self._admit(
                    conn, run, include_visually_degraded=query.include_visually_degraded
                )
                if exclusion is not None:
                    excluded.append(exclusion)
                    continue
                included.append((run, self._metrics_by_turn(conn, run)))

            observed: set[str] = set()
            for _, by_turn in included:
                for metrics in by_turn.values():
                    observed.update(metrics)
            requested = (
                tuple(query.metrics) if query.metrics is not None else tuple(sorted(observed))
            )

            series: list[MetricSeries] = []
            for run, by_turn in included:
                in_progress = run.lifecycle_state not in _TERMINAL_LIFECYCLE_STATES
                for metric in requested:
                    points = tuple(
                        MetricPoint(turn=turn, value=metrics[metric])
                        for turn, metrics in sorted(by_turn.items())
                        if metric in metrics
                    )
                    series.append(
                        MetricSeries(
                            run_id=run.run_id,
                            metric=metric,
                            points=points,
                            in_progress=in_progress,
                            comparability_status=run.comparability_status,
                            unavailable_reason=(
                                None
                                if points
                                else f"the record of run {run.run_id} carries no metric "
                                f"{metric!r} on any authoritative turn"
                            ),
                        )
                    )
            return TrendResponse(
                series=tuple(series),
                excluded=tuple(excluded),
                metric_names=tuple(sorted(observed)),
                included_visually_degraded=query.include_visually_degraded,
            )

        return self._with_lock(body)

    def _fingerprints(self, conn: sqlite3.Connection, run: Run) -> list[Any]:
        rows = conn.execute(
            "SELECT turn_cycle_id FROM turn_cycles "
            "WHERE run_id = ? AND is_authoritative = 1 ORDER BY turn_number ASC",
            (run.run_id,),
        ).fetchall()
        return [turn_fingerprint(self._load_turn_cycle_record(conn, str(row[0]))) for row in rows]

    def divergence(self, run_a: RunId, run_b: RunId) -> DivergenceReport:
        def body(conn: sqlite3.Connection) -> DivergenceReport:
            excluded: list[ExcludedRun] = []
            runs: list[Run] = []
            for run_id in (run_a, run_b):
                run = self._get_run_body(conn, run_id)
                if run is None:
                    excluded.append(
                        ExcludedRun(
                            run_id=run_id,
                            reason=ExclusionReason.NO_SUCH_RUN,
                            detail="no run with this id is recorded",
                        )
                    )
                    continue
                # Degradation concerns images, not the actions compared (contract T4).
                exclusion = self._admit(conn, run, include_visually_degraded=True)
                if exclusion is not None:
                    excluded.append(exclusion)
                    continue
                runs.append(run)
            config_a = self._get_run_configuration_body(conn, run_a)
            config_b = self._get_run_configuration_body(conn, run_b)
            same_seed_set = (
                config_a is not None
                and config_b is not None
                and config_a.seed_set_id is not None
                and config_a.seed_set_id == config_b.seed_set_id
            )
            if len(runs) < 2:
                return DivergenceReport(
                    run_a=run_a,
                    run_b=run_b,
                    same_seed_set=same_seed_set,
                    compared_turns=(),
                    first_divergent_turn=None,
                    differed=(),
                    excluded=tuple(excluded),
                )
            floor = max(first_owed_turn(runs[0]), first_owed_turn(runs[1]))
            prints_a = [fp for fp in self._fingerprints(conn, runs[0]) if fp.turn >= floor]
            prints_b = [fp for fp in self._fingerprints(conn, runs[1]) if fp.turn >= floor]
            shared = sorted({fp.turn for fp in prints_a} & {fp.turn for fp in prints_b})
            first, differed = first_divergence(prints_a, prints_b)
            return DivergenceReport(
                run_a=run_a,
                run_b=run_b,
                same_seed_set=same_seed_set,
                compared_turns=tuple(shared),
                first_divergent_turn=first,
                differed=differed,
                excluded=tuple(excluded),
            )

        return self._with_lock(body)

    # ----------------------------------------------------------------
    # US4: store information and export
    # ----------------------------------------------------------------

    def store_info(self) -> StoreInfo:
        def body(conn: sqlite3.Connection) -> StoreInfo:
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in _COUNTED_TABLES
            }
            meta = store_schema.read_meta(conn)
            dangling = conn.execute(
                "SELECT r.run_id FROM runs AS r WHERE r.parent_run_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM runs AS p WHERE p.run_id = r.parent_run_id) "
                "ORDER BY r.run_id"
            ).fetchall()
            return StoreInfo(
                schema_version=self._schema_version,
                store_id=meta.get("store_id", ""),
                host_platform=meta.get("host_platform", self._host_platform),
                read_only=self._read_only,
                counts=counts,
                migrations=store_schema.list_migrations(conn),
                runs_with_absent_parent=tuple(RunId(str(row[0])) for row in dangling),
            )

        return self._with_lock(body)

    def export_run(self, run_id: RunId) -> RunRecordSet:
        def body(conn: sqlite3.Connection) -> RunRecordSet:
            run = self._get_run_body(conn, run_id)
            configuration = self._get_run_configuration_body(conn, run_id)
            if run is None or configuration is None:
                raise StoreReadError("export_run: no such run", detail={"run_id": run_id})
            cycle_rows = conn.execute(
                "SELECT turn_cycle_id FROM turn_cycles WHERE run_id = ? "
                "ORDER BY turn_number ASC, attempt_index ASC",
                (run_id,),
            ).fetchall()
            turn_cycles = tuple(
                self._load_turn_cycle_record(conn, str(row[0])) for row in cycle_rows
            )
            event_rows = conn.execute(
                "SELECT event_json FROM run_events WHERE run_id = ? "
                "ORDER BY occurred_at ASC, event_id ASC",
                (run_id,),
            ).fetchall()
            call_rows = conn.execute(
                "SELECT call_json FROM model_calls WHERE run_id = ? "
                "ORDER BY turn_number IS NULL, turn_number ASC, step_index IS NULL, "
                "step_index ASC, model_call_id ASC",
                (run_id,),
            ).fetchall()
            capture_rows = conn.execute(
                "SELECT capture_json FROM captures WHERE run_id = ? ORDER BY capture_id ASC",
                (run_id,),
            ).fetchall()
            captures = tuple(ScreenCapture.model_validate_json(row[0]) for row in capture_rows)
            images: dict[str, bytes] = {}
            for capture in captures:
                if capture.screening_status is not ScreeningStatus.SCREENED_CLEAN:
                    continue
                image = self._capture_image_body(conn, capture.capture_id)
                if image.status is not CaptureImageStatus.AVAILABLE or image.content is None:
                    raise StoreReadError(
                        "export_run: a kept capture's image is missing on disk; the record is "
                        "intact but the run cannot be exported byte-identically (SC-006)",
                        detail={
                            "run_id": run_id,
                            "capture_id": capture.capture_id,
                            "blob_ref": capture.blob_ref,
                            "status": image.status.value,
                        },
                    )
                if capture.blob_ref is not None:
                    images[capture.blob_ref] = image.content
            return RunRecordSet(
                run=run,
                configuration=configuration,
                turn_cycles=turn_cycles,
                run_events=tuple(RunEvent.model_validate_json(row[0]) for row in event_rows),
                model_calls=tuple(ModelCall.model_validate_json(row[0]) for row in call_rows),
                save_points=tuple(self._list_save_points_body(conn, run_id)),
                captures=captures,
                images=images,
            )

        return self._with_lock(body)


__all__ = ["SqliteReadBase"]
