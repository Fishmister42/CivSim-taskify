"""The store file's schema: version 1.1 DDL, version detection, and the copy-first migration
from the pre-feature 1.0 file (003 FR-024, FR-025, FR-026; contract V1-V3).

**1.0 is the file 002's reference adapter wrote**: seven tables and no metadata at all. That is
the 2026-09-21 ``civsim-match-store.db``. **1.1 is additive over it**: two new tables
(``store_meta``, ``schema_migrations``), indexed projection columns on ``runs`` and
``model_calls``, and one ``model_calls`` row derived per decision-step bundle whose call was
only ever embedded (research R2, R3). No row is rewritten, no column removed, no record kind
dropped -- a 1.0 reader could still read every record a 1.1 file holds.

**The copy comes first.** :func:`migrate_1_0_to_1_1` checkpoints the WAL and copies the file
through SQLite's own backup API to ``<file>.v1.0.bak-<UTC>`` *before* it changes a byte of the
original; the copy is the rollback (spec Assumptions). The migration itself is one
``BEGIN IMMEDIATE`` transaction, so an interrupted migration leaves a 1.0 file, not a half-1.1 one.

**Read-only never migrates.** :func:`open_store_file` in read-only mode refuses a 1.0 file with
the command that will migrate it, and refuses any file whose major version it does not know
(FR-026) -- naming both versions and reading nothing.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civsim_harness.errors import StoreReadError, StoreSchemaError
from civsim_harness.store.contract import MigrationRecord, StoreSchemaVersion

#: The version this module writes and fully understands.
STORE_SCHEMA_VERSION = StoreSchemaVersion(1, 1)

#: The version a file with no ``store_meta`` table is, by definition (data-model.md SS1.1).
LEGACY_SCHEMA_VERSION = StoreSchemaVersion(1, 0)

#: 002's seven tables -- what a 1.0 file has, and what every later version keeps.
LEGACY_TABLES: tuple[str, ...] = (
    "runs",
    "turn_cycles",
    "decision_steps",
    "run_events",
    "model_calls",
    "save_points",
    "captures",
)

#: The command an operator runs when a read-only opener meets a 1.0 file.
MIGRATE_COMMAND = "civsim store migrate"

# The 1.0 DDL, kept verbatim from 002's adapter so a test can build a genuine legacy file.
LEGACY_SCHEMA_SQL_1_0 = """
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

# Everything 1.1 adds on top of 1.0. Applied both to a fresh file (after the legacy DDL) and by
# the migration (inside its transaction). Every statement is idempotent except the ADD COLUMNs,
# which the migration guards by inspecting the live column list first.
_META_SQL_1_1 = """
CREATE TABLE IF NOT EXISTS store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    backup_path TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""

_INDEX_SQL_1_1 = """
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_seed_set ON runs(seed_set_id);
CREATE INDEX IF NOT EXISTS idx_runs_lifecycle ON runs(lifecycle_state);
CREATE INDEX IF NOT EXISTS idx_model_calls_address ON model_calls(run_id, turn_number, step_index);
"""

#: (table, column, declared type) -- the projection columns 1.1 adds (data-model.md SS2).
ADDED_COLUMNS_1_1: tuple[tuple[str, str, str], ...] = (
    ("runs", "started_at", "TEXT"),
    ("runs", "ended_at", "TEXT"),
    ("runs", "seed_set_id", "TEXT"),
    ("runs", "record_completeness_status", "TEXT"),
    ("runs", "comparability_status", "TEXT"),
    ("model_calls", "turn_number", "INTEGER"),
    ("model_calls", "step_index", "INTEGER"),
)


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def detect_version(conn: sqlite3.Connection) -> StoreSchemaVersion | None:
    """The file's schema version; ``None`` for an empty file (no tables at all)."""
    tables = table_names(conn)
    if not tables & set(LEGACY_TABLES):
        return None
    if "store_meta" not in tables:
        return LEGACY_SCHEMA_VERSION
    rows = dict(
        conn.execute(
            "SELECT key, value FROM store_meta WHERE key IN ('schema_major', 'schema_minor')"
        ).fetchall()
    )
    try:
        return StoreSchemaVersion(int(rows["schema_major"]), int(rows["schema_minor"]))
    except (KeyError, ValueError) as exc:
        raise StoreSchemaError(
            "store_meta exists but carries no readable schema version",
            detail={"rows": {str(k): str(v) for k, v in rows.items()}},
        ) from exc


def read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    if "store_meta" not in table_names(conn):
        return {}
    return {
        str(key): str(value)
        for key, value in conn.execute("SELECT key, value FROM store_meta").fetchall()
    }


def list_migrations(conn: sqlite3.Connection) -> tuple[MigrationRecord, ...]:
    if "schema_migrations" not in table_names(conn):
        return ()
    rows = conn.execute(
        "SELECT migration_id, from_version, to_version, applied_at, backup_path, detail_json "
        "FROM schema_migrations ORDER BY applied_at ASC"
    ).fetchall()
    return tuple(
        MigrationRecord(
            migration_id=row[0],
            from_version=row[1],
            to_version=row[2],
            applied_at=datetime.fromisoformat(row[3]),
            backup_path=row[4],
            detail=json.loads(row[5]),
        )
        for row in rows
    )


# --------------------------------------------------------------------------
# Fresh files
# --------------------------------------------------------------------------


def _write_meta(conn: sqlite3.Connection, *, host_platform: str, now: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
        ("schema_major", str(STORE_SCHEMA_VERSION.major)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
        ("schema_minor", str(STORE_SCHEMA_VERSION.minor)),
    )
    conn.execute(
        "INSERT OR IGNORE INTO store_meta (key, value) VALUES (?, ?)",
        ("store_id", uuid.uuid4().hex),
    )
    conn.execute(
        "INSERT OR IGNORE INTO store_meta (key, value) VALUES (?, ?)",
        ("created_at", now.isoformat()),
    )
    conn.execute(
        "INSERT OR IGNORE INTO store_meta (key, value) VALUES (?, ?)",
        ("host_platform", host_platform),
    )


def initialise_new_store(
    conn: sqlite3.Connection, *, host_platform: str, now: datetime | None = None
) -> None:
    """Create the full 1.1 schema in an empty file and stamp its metadata (V1)."""
    stamp = now or datetime.now(UTC)
    conn.executescript(LEGACY_SCHEMA_SQL_1_0)
    conn.executescript(_META_SQL_1_1)
    present = {table: column_names(conn, table) for table in ("runs", "model_calls")}
    for table, column, declared in ADDED_COLUMNS_1_1:
        if column not in present[table]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declared}")
    conn.executescript(_INDEX_SQL_1_1)
    _write_meta(conn, host_platform=host_platform, now=stamp)


# --------------------------------------------------------------------------
# Projection helpers shared by the migration and the adapter
# --------------------------------------------------------------------------


def run_projection(run_json: str, config_json: str) -> tuple[Any, ...]:
    """``(started_at, ended_at, seed_set_id, record_completeness_status, comparability_status)``
    as the ``runs`` columns store them, from the authoritative JSON (data-model.md SS2 I-A)."""
    run = json.loads(run_json)
    config = json.loads(config_json)
    return (
        run.get("started_at"),
        run.get("ended_at"),
        config.get("seed_set_id"),
        run.get("record_completeness_status"),
        run.get("comparability_status"),
    )


# --------------------------------------------------------------------------
# Migration 1.0 -> 1.1
# --------------------------------------------------------------------------


def backup_path_for(db_path: Path, *, now: datetime) -> Path:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return db_path.with_name(f"{db_path.name}.v{LEGACY_SCHEMA_VERSION}.bak-{stamp}")


def copy_store_file(conn: sqlite3.Connection, destination: Path) -> None:
    """Copy the open database to *destination* through the backup API, after checkpointing the
    WAL so the copy is self-contained (research R2)."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(destination))
    try:
        conn.backup(target)
    finally:
        target.close()


def _derive_model_calls(conn: sqlite3.Connection) -> int:
    """Insert one ``model_calls`` row per decision-step bundle whose call has no row (V3)."""
    existing = {row[0] for row in conn.execute("SELECT model_call_id FROM model_calls").fetchall()}
    derived = 0
    rows = conn.execute(
        "SELECT ds.bundle_json, ds.step_index, tc.run_id, tc.turn_number "
        "FROM decision_steps AS ds JOIN turn_cycles AS tc ON tc.turn_cycle_id = ds.turn_cycle_id"
    ).fetchall()
    for bundle_json, step_index, run_id, turn_number in rows:
        bundle = json.loads(bundle_json)
        call = bundle.get("model_call")
        if not isinstance(call, dict):
            continue
        call_id = call.get("model_call_id")
        if not isinstance(call_id, str) or call_id in existing:
            continue
        conn.execute(
            "INSERT INTO model_calls (model_call_id, run_id, decision_step_id, call_json, "
            "turn_number, step_index) VALUES (?, ?, ?, ?, ?, ?)",
            (
                call_id,
                run_id,
                call.get("decision_step_id"),
                json.dumps(call, separators=(",", ":")),
                turn_number,
                step_index,
            ),
        )
        existing.add(call_id)
        derived += 1
    return derived


def _backfill_projection(conn: sqlite3.Connection) -> tuple[int, int]:
    runs = conn.execute("SELECT run_id, run_json, config_json FROM runs").fetchall()
    for run_id, run_json, config_json in runs:
        conn.execute(
            "UPDATE runs SET started_at=?, ended_at=?, seed_set_id=?, "
            "record_completeness_status=?, comparability_status=? WHERE run_id=?",
            (*run_projection(run_json, config_json), run_id),
        )
    calls = conn.execute("SELECT model_call_id, call_json FROM model_calls").fetchall()
    addressed = 0
    for call_id, call_json in calls:
        call = json.loads(call_json)
        turn_row = conn.execute(
            "SELECT turn_number FROM turn_cycles WHERE turn_cycle_id = ?",
            (call.get("turn_cycle_id"),),
        ).fetchone()
        step_row = conn.execute(
            "SELECT step_index FROM decision_steps WHERE decision_step_id = ?",
            (call.get("decision_step_id"),),
        ).fetchone()
        conn.execute(
            "UPDATE model_calls SET turn_number=?, step_index=? WHERE model_call_id=?",
            (
                turn_row[0] if turn_row else None,
                step_row[0] if step_row else None,
                call_id,
            ),
        )
        if turn_row:
            addressed += 1
    return len(runs), addressed


def migrate_1_0_to_1_1(
    db_path: Path,
    conn: sqlite3.Connection,
    *,
    host_platform: str,
    now: datetime | None = None,
) -> MigrationRecord:
    """Copy first, then migrate in one transaction; return the record written (V1, V3)."""
    stamp = now or datetime.now(UTC)
    backup = backup_path_for(db_path, now=stamp)
    copy_store_file(conn, backup)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Statement by statement: executescript() would issue its own COMMIT first.
        for statement in _statements(_META_SQL_1_1):
            conn.execute(statement)
        present = {table: column_names(conn, table) for table in ("runs", "model_calls")}
        for table, column, declared in ADDED_COLUMNS_1_1:
            if column not in present[table]:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declared}")
        for statement in _statements(_INDEX_SQL_1_1):
            conn.execute(statement)
        runs_backfilled, _ = _backfill_projection(conn)
        derived = _derive_model_calls(conn)
        _, calls_addressed = _backfill_projection(conn)
        _write_meta(conn, host_platform=host_platform, now=stamp)
        record = MigrationRecord(
            migration_id=f"{LEGACY_SCHEMA_VERSION}->{STORE_SCHEMA_VERSION}",
            from_version=str(LEGACY_SCHEMA_VERSION),
            to_version=str(STORE_SCHEMA_VERSION),
            applied_at=stamp,
            backup_path=str(backup),
            detail={
                "model_calls_derived": derived,
                "runs_backfilled": runs_backfilled,
                "model_calls_addressed": calls_addressed,
            },
        )
        conn.execute(
            "INSERT INTO schema_migrations (migration_id, from_version, to_version, applied_at, "
            "backup_path, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.migration_id,
                record.from_version,
                record.to_version,
                record.applied_at.isoformat(),
                record.backup_path,
                json.dumps(dict(record.detail), sort_keys=True),
            ),
        )
        conn.execute("COMMIT")
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise StoreSchemaError(
            "migration 1.0->1.1 failed and was rolled back; the pre-migration copy is intact",
            detail={"backup_path": str(backup), "error": str(exc)},
        ) from exc
    return record


def _statements(script: str) -> list[str]:
    return [part.strip() for part in script.split(";") if part.strip()]


def plan_migration(conn: sqlite3.Connection) -> dict[str, Any]:
    """What a migration *would* do, without doing it (``civsim store migrate --dry-run``)."""
    version = detect_version(conn)
    if version is None:
        return {"detected": None, "action": "initialise", "changes": {}}
    if version != LEGACY_SCHEMA_VERSION:
        return {"detected": str(version), "action": "none", "changes": {}}
    present = {row[0] for row in conn.execute("SELECT model_call_id FROM model_calls")}
    embedded = 0
    for (bundle_json,) in conn.execute("SELECT bundle_json FROM decision_steps"):
        call = json.loads(bundle_json).get("model_call") or {}
        if isinstance(call, dict) and call.get("model_call_id") not in present:
            embedded += 1
    runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    return {
        "detected": str(version),
        "action": f"migrate to {STORE_SCHEMA_VERSION}",
        "changes": {
            "model_calls_to_derive": embedded,
            "runs_to_backfill": runs,
            "columns_to_add": [f"{t}.{c}" for t, c, _ in ADDED_COLUMNS_1_1],
            "tables_to_add": ["store_meta", "schema_migrations"],
        },
    }


# --------------------------------------------------------------------------
# Opening
# --------------------------------------------------------------------------


def open_store_file(
    db_path: Path,
    *,
    read_only: bool,
    host_platform: str,
    now: datetime | None = None,
) -> tuple[sqlite3.Connection, StoreSchemaVersion, tuple[MigrationRecord, ...]]:
    """Open *db_path* and bring it to a version this module can serve.

    Write mode: an empty file is initialised at 1.1; a 1.0 file is migrated (copy first); a
    newer minor opens as is. Read-only mode (``PRAGMA query_only``): a 1.0 file is refused
    naming :data:`MIGRATE_COMMAND`; a missing file is refused. Either mode: a major this module
    does not know is refused naming both versions, and nothing is read (FR-026, V2).
    """
    if read_only and not db_path.exists():
        raise StoreReadError("store file does not exist", detail={"path": str(db_path)})
    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    try:
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")

        version = detect_version(conn)
        applied: tuple[MigrationRecord, ...] = ()

        if version is None:
            if read_only:
                raise StoreSchemaError(
                    "store file is empty and the reader may not initialise it",
                    detail={"path": str(db_path), "command": MIGRATE_COMMAND},
                )
            initialise_new_store(conn, host_platform=host_platform, now=now)
            version = STORE_SCHEMA_VERSION
        elif not version.readable_by(STORE_SCHEMA_VERSION):
            raise StoreSchemaError(
                "store file was written by a newer schema major version than this reader "
                "understands; refusing to read it partially (FR-026)",
                detail={
                    "path": str(db_path),
                    "file_version": str(version),
                    "reader_version": str(STORE_SCHEMA_VERSION),
                },
            )
        elif version == LEGACY_SCHEMA_VERSION:
            if read_only:
                raise StoreSchemaError(
                    "store file is at schema 1.0 and must be migrated before a read-only "
                    "opener can serve it",
                    detail={
                        "path": str(db_path),
                        "file_version": str(version),
                        "reader_version": str(STORE_SCHEMA_VERSION),
                        "command": MIGRATE_COMMAND,
                    },
                )
            applied = (migrate_1_0_to_1_1(db_path, conn, host_platform=host_platform, now=now),)
            version = STORE_SCHEMA_VERSION
        # else: same major, minor >= ours -- additive, readable as is.
        return conn, version, applied
    except Exception:
        conn.close()
        raise
