"""Store file versioning and the copy-first 1.0 -> 1.1 migration (T010, T011; 003 FR-024,
FR-025, FR-026; contract V1-V3; SC-005).

Part 1 builds a **genuine** 1.0 file with 002's own DDL and rows written the way the 002
adapter wrote them, so the migration is exercised against the real legacy shape rather than a
fixture that happens to look like it. Part 2 runs the same assertions against a copy of the
real 2026-09-21 file when this host has it (it is gitignored and lives only on the Linux node).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from civsim_harness.errors import StoreReadError, StoreSchemaError
from civsim_harness.models.run import Run
from civsim_harness.store import schema as store_schema
from civsim_harness.store.contract import CaptureImageStatus, StoreSchemaVersion
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.legacy import build_legacy_file

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_FILE = REPO_ROOT / "civsim-match-store.db"


@pytest.fixture
def legacy(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    db_path = tmp_path / "legacy.db"
    return db_path, build_legacy_file(db_path)


# --------------------------------------------------------------------------
# Part 1 -- synthetic file
# --------------------------------------------------------------------------


def test_a_fresh_store_is_1_1_with_identity_and_no_migrations(tmp_path: Path) -> None:
    store = SqliteMatchStore(tmp_path / "fresh.db")
    try:
        info = store.store_info()
        assert info.schema_version == StoreSchemaVersion(1, 1)
        assert len(info.store_id) == 32
        assert info.migrations == ()
        assert info.read_only is False
        assert store.migrations_applied_on_open == ()
    finally:
        store.close()


def test_a_legacy_file_is_detected_as_1_0(legacy: tuple[Path, dict[str, object]]) -> None:
    db_path, _ = legacy
    conn = sqlite3.connect(str(db_path))
    try:
        assert store_schema.detect_version(conn) == StoreSchemaVersion(1, 0)
        plan = store_schema.plan_migration(conn)
        assert plan["detected"] == "1.0"
        assert plan["changes"]["model_calls_to_derive"] == 9
        assert plan["changes"]["runs_to_backfill"] == 2
    finally:
        conn.close()


def test_write_open_migrates_after_copying_and_reads_every_run_unchanged(
    legacy: tuple[Path, dict[str, object]],
) -> None:
    db_path, facts = legacy
    before = {
        run_id: Run.model_validate_json(row)
        for run_id, row in sqlite3.connect(str(db_path))
        .execute("SELECT run_id, run_json FROM runs")
        .fetchall()
    }
    store = SqliteMatchStore(db_path)
    try:
        # V1: the copy exists, beside the file, and is itself a readable 1.0 database.
        (applied,) = store.migrations_applied_on_open
        backup = Path(applied.backup_path)
        assert backup.parent == db_path.parent and backup.name.startswith("legacy.db.v1.0.bak-")
        assert store_schema.detect_version(sqlite3.connect(str(backup))) == StoreSchemaVersion(1, 0)

        info = store.store_info()
        assert info.schema_version == StoreSchemaVersion(1, 1)
        assert [m.migration_id for m in info.migrations] == ["1.0->1.1"]
        assert info.migrations[0].detail["model_calls_derived"] == facts["steps"]

        # FR-025 / SC-005: every run reads back equal to what the 1.0 file held.
        for run_id, run in before.items():
            assert store.get_run(run_id) == run
        assert {r.run_id for r in store.list_runs()} == set(before)

        # V3: one row per step bundle, plus the failed call that already had a row.
        rows = store.list_model_calls("run-a")
        assert len(rows) == 8 + 1  # 3 + 3 + 2 steps, + the failed call
        assert store.list_model_calls("run-b")[0].turn_number == 1
        failed = [row for row in rows if row.call.model_call_id == "run-a-failed-call"]
        assert failed and failed[0].turn_number is None  # its turn was never recorded
        assert rows[0].turn_number == 1 and rows[0].step_index == 1
        totals = store.model_call_totals("run-a")
        assert totals.call_count == 9
        assert totals.cost_usd == pytest.approx(0.01 * 3 + 0.02 * 2 + 0.001)

        # The projection columns were backfilled from the JSON.
        page = store.query_runs(store_query(seed_set_id="seed-1"))
        assert page.total == 2

        # Captures survived, the kept image resolves, the withheld one is withheld.
        assert store.get_capture_image("cap-kept").content == facts["kept_blob"]
        assert store.get_capture_image("cap-withheld").status is CaptureImageStatus.WITHHELD

        # Re-opening the migrated file does not migrate again.
        store.close()
        again = SqliteMatchStore(db_path)
        try:
            assert again.migrations_applied_on_open == ()
            assert again.store_info().schema_version == StoreSchemaVersion(1, 1)
        finally:
            again.close()
    finally:
        try:
            store.close()
        except Exception:  # pragma: no cover - already closed above on the happy path
            pass


def store_query(**kwargs: object) -> object:
    from civsim_harness.store.contract import RunQuery

    return RunQuery(**kwargs)  # type: ignore[arg-type]


def test_read_only_open_refuses_a_legacy_file_naming_the_command(
    legacy: tuple[Path, dict[str, object]],
) -> None:
    db_path, _ = legacy
    with pytest.raises(StoreSchemaError) as info:
        SqliteMatchStore(db_path, read_only=True)
    assert info.value.detail["command"] == "civsim store migrate"
    assert info.value.detail["file_version"] == "1.0"
    # Nothing changed: the file is still 1.0 and no backup was taken.
    assert store_schema.detect_version(sqlite3.connect(str(db_path))) == StoreSchemaVersion(1, 0)
    assert not list(db_path.parent.glob("legacy.db.v1.0.bak-*"))


def test_read_only_open_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(StoreReadError):
        SqliteMatchStore(tmp_path / "absent.db", read_only=True)


def test_a_newer_major_is_refused_naming_both_versions_and_reading_nothing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "future.db"
    store = SqliteMatchStore(db_path)
    store.close()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE store_meta SET value = '2' WHERE key = 'schema_major'")
    conn.execute("UPDATE store_meta SET value = '0' WHERE key = 'schema_minor'")
    conn.commit()
    conn.close()
    for read_only in (False, True):
        with pytest.raises(StoreSchemaError) as info:
            SqliteMatchStore(db_path, read_only=read_only)
        assert info.value.detail["file_version"] == "2.0"
        assert info.value.detail["reader_version"] == "1.1"


def test_a_newer_minor_opens_as_is(tmp_path: Path) -> None:
    db_path = tmp_path / "minor.db"
    store = SqliteMatchStore(db_path)
    store.close()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE store_meta SET value = '2' WHERE key = 'schema_minor'")
    conn.commit()
    conn.close()
    reopened = SqliteMatchStore(db_path, read_only=True)
    try:
        assert reopened.schema_version == StoreSchemaVersion(1, 2)
        assert reopened.migrations_applied_on_open == ()
    finally:
        reopened.close()


def test_migration_is_idempotent_on_a_partially_upgraded_file(
    legacy: tuple[Path, dict[str, object]],
) -> None:
    """A 1.0 file that already gained a column (say, from an interrupted hand edit) still
    migrates: the ADD COLUMNs are guarded by the live column list."""
    db_path, _ = legacy
    conn = sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE runs ADD COLUMN started_at TEXT")
    conn.commit()
    conn.close()
    store = SqliteMatchStore(db_path)
    try:
        assert store.store_info().schema_version == StoreSchemaVersion(1, 1)
        assert store.query_runs(store_query()).total == 2
    finally:
        store.close()


# --------------------------------------------------------------------------
# Part 2 -- the real 2026-09-21 file, when present (T011, SC-005)
# --------------------------------------------------------------------------


@pytest.mark.skipif(not REAL_FILE.exists(), reason="the 2026-09-21 store file is not on this host")
def test_the_real_pre_feature_file_migrates_and_reads_back(tmp_path: Path) -> None:
    db_copy = tmp_path / "real.db"
    shutil.copy2(REAL_FILE, db_copy)
    for sidecar in ("-wal", "-shm"):
        side = REAL_FILE.with_name(REAL_FILE.name + sidecar)
        if side.exists():
            shutil.copy2(side, db_copy.with_name(db_copy.name + sidecar))
    real_blobs = REAL_FILE.parent / "blobs"
    if real_blobs.is_dir():
        shutil.copytree(real_blobs, tmp_path / "blobs")

    legacy_conn = sqlite3.connect(str(db_copy))
    version_before = store_schema.detect_version(legacy_conn)
    before = {
        run_id: json.loads(row)
        for run_id, row in legacy_conn.execute("SELECT run_id, run_json FROM runs").fetchall()
    }
    steps_before = legacy_conn.execute("SELECT COUNT(*) FROM decision_steps").fetchone()[0]
    calls_before = legacy_conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
    legacy_conn.close()
    if version_before != StoreSchemaVersion(1, 0):
        pytest.skip(f"the real file is already at {version_before}; nothing to migrate")

    store = SqliteMatchStore(db_copy)
    try:
        assert len(before) == 10, "the 2026-09-21 file holds ten runs (research R2)"
        for run_id, raw in before.items():
            run = store.get_run(run_id)
            assert run is not None
            assert run.model_dump(mode="json") == raw
        assert len(store.list_runs()) == 10
        info = store.store_info()
        assert info.schema_version == StoreSchemaVersion(1, 1)
        assert info.counts["model_calls"] == steps_before + calls_before
        for run_id in before:
            rows = store.list_model_calls(run_id)
            steps = sum(
                len(record.steps)
                for turn in range(1, store.highest_recorded_turn(run_id) + 1)
                for record in [store.get_turn_cycle(run_id, turn, authoritative_only=False)]
                if record is not None
            )
            assert len(rows) >= steps
            # Every kept capture answers available or missing -- never raises.
            for capture_id in _capture_ids(store, run_id):
                assert store.get_capture_image(capture_id).status in {
                    CaptureImageStatus.AVAILABLE,
                    CaptureImageStatus.MISSING,
                    CaptureImageStatus.WITHHELD,
                }
    finally:
        store.close()


def _capture_ids(store: SqliteMatchStore, run_id: str) -> list[str]:
    rows = store._conn.execute(  # noqa: SLF001 -- white-box enumeration for the audit
        "SELECT capture_id FROM captures WHERE run_id = ?", (run_id,)
    ).fetchall()
    return [str(row[0]) for row in rows]
