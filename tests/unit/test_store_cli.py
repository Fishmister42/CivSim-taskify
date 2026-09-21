"""`civsim store …` (T038, T039; 003 US4) through Typer's `CliRunner`."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from civsim_harness.operator.cli import app
from civsim_harness.store import open_read_only
from civsim_harness.store import schema as store_schema
from civsim_harness.store.contract import StoreSchemaVersion
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import make_capture, record_run
from store_support.legacy import build_legacy_file

runner = CliRunner()


def _invoke(*args: str) -> tuple[int, str, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.stdout, result.stderr if hasattr(result, "stderr") else ""


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    path = tmp_path / "s.db"
    store = SqliteMatchStore(path)
    record_run(
        store, "run-a", turns=3, seed_set_id="ss-1", lifecycle_state="finished", cost_usd=0.02
    )
    kept, blob = make_capture("cap", "run-a", 1, "run-a-t1-a0-step1", blob=b"img")
    store.write_capture(kept, blob)
    record_run(store, "run-b", turns=1, seed_set_id="ss-2")
    store.close()
    return path


def test_info_on_a_fresh_store_prints_the_version_and_counts(populated: Path) -> None:
    code, out, _ = _invoke("store", "info", "--store", str(populated))
    assert code == 0, out
    assert "schema_version   : 1.1" in out
    assert "runs             : 2" in out
    assert "migrations       : none" in out


def test_info_on_a_missing_or_legacy_file_says_what_to_do(tmp_path: Path) -> None:
    code, out, _ = _invoke("store", "info", "--store", str(tmp_path / "none.db"))
    assert code == 0 and "no store file" in out

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript(store_schema.LEGACY_SCHEMA_SQL_1_0)
    conn.commit()
    conn.close()
    code, out, _ = _invoke("store", "info", "--store", str(legacy))
    assert code == 0
    assert "schema_version   : 1.0" in out and "civsim store migrate" in out
    assert store_schema.detect_version(sqlite3.connect(str(legacy))) == StoreSchemaVersion(1, 0)


def test_migrate_dry_run_changes_nothing_and_migrate_performs_it(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.db"
    build_legacy_file(legacy)
    code, out, _ = _invoke("store", "migrate", "--store", str(legacy), "--dry-run")
    assert code == 0, out
    assert "detected         : 1.0" in out
    assert "legacy.db.v1.0.bak-" in out
    assert "model_calls_to_derive: 9" in out
    assert store_schema.detect_version(sqlite3.connect(str(legacy))) == StoreSchemaVersion(1, 0)
    assert not list(tmp_path.glob("legacy.db.v1.0.bak-*"))

    code, out, _ = _invoke("store", "migrate", "--store", str(legacy))
    assert code == 0, out
    assert "migrated         : 1.0 -> 1.1" in out
    assert list(tmp_path.glob("legacy.db.v1.0.bak-*"))
    code, out, _ = _invoke("store", "migrate", "--store", str(legacy))
    assert code == 0 and "already at 1.1" in out


def test_runs_lists_and_filters(populated: Path) -> None:
    code, out, _ = _invoke("store", "runs", "--store", str(populated))
    assert code == 0, out
    assert "page 1 of 1 (total 2" in out and "run-a" in out and "run-b" in out
    code, out, _ = _invoke("store", "runs", "--store", str(populated), "--seed-set", "ss-2")
    assert code == 0 and "run-b" in out and "run-a" not in out
    code, out, _ = _invoke("store", "runs", "--store", str(populated), "--state", "finished")
    assert code == 0 and "run-a" in out and "run-b" not in out
    code, out, _ = _invoke("store", "runs", "--store", str(populated), "--sort", "bogus")
    assert code == 1


def test_model_calls_prints_rows_and_the_totals_line(populated: Path) -> None:
    code, out, _ = _invoke("store", "model-calls", "run-a", "--store", str(populated))
    assert code == 0, out
    assert "totals           : calls=3 priced=3 cost_usd=0.060000" in out
    code, out, _ = _invoke("store", "model-calls", "ghost", "--store", str(populated))
    assert code == 2


def test_export_then_import_round_trips_and_a_second_import_is_refused(
    populated: Path, tmp_path: Path
) -> None:
    code, out, _ = _invoke(
        "store",
        "export",
        "run-a",
        "--store",
        str(populated),
        "--out",
        str(tmp_path / "b"),
        "--archive",
    )
    assert code == 0, out
    archive = tmp_path / "b" / "run-a.civsim-bundle.tar.gz"
    assert archive.is_file() and "captures         : 1" in out

    other = tmp_path / "other.db"
    code, out, _ = _invoke("store", "import", str(archive), "--store", str(other))
    assert code == 0, out
    assert "imported         : run-a" in out
    code, out, err = _invoke("store", "import", str(archive), "--store", str(other))
    assert code == 2
    assert "run-a" in (out + err)

    imported = SqliteMatchStore(other, read_only=True)
    try:
        assert imported.get_capture_image("cap").content == b"img"
        assert imported.model_call_totals("run-a").call_count == 3
    finally:
        imported.close()

    code, out, _ = _invoke(
        "store", "export", "ghost", "--store", str(populated), "--out", str(tmp_path / "c")
    )
    assert code == 2


def test_open_read_only_resolves_the_environment_path(
    populated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIVSIM_STORE_PATH", str(populated))
    store = open_read_only()
    try:
        assert store.read_only is True
        assert {r.run_id for r in store.list_runs()} == {"run-a", "run-b"}
        assert store.get_capture_blob("cap") == b"img"
    finally:
        store.close()
    explicit = open_read_only(populated)
    try:
        assert explicit.store_info().read_only is True
    finally:
        explicit.close()
    assert datetime.now(UTC).year >= 2026
