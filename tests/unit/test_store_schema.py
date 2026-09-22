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

#: The frozen pre-feature snapshot both real-file tests below actually need. **Not**
#: ``REAL_FILE`` (``civsim-match-store.db``): that path is the *live* store this host keeps
#: driving Civ VI matches into, and by 2026-09-22 it has grown to 44 runs and had its schema
#: migrated to 1.1 by ordinary use -- both confirmed by direct measurement on 2026-09-22
#: (T051/T053). This ``.bak-*`` copy, taken 2026-09-21T15:21:08Z by the migration itself, is
#: the untouched ten-run/1.0 snapshot that SC-005's "the ten runs recorded on 2026-09-21" and
#: T045's recorded $1.424694 both describe. The fixed timestamp in its name is what makes it
#: safe to key an assertion to, where a live file that keeps moving is not.
#:
#: Neither file is ever opened here except to copy it; every test below works on `tmp_path`.
REAL_PRE_FEATURE_BAK_FILE = REPO_ROOT / "civsim-match-store.db.v1.0.bak-20260921T152108Z"


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


@pytest.mark.skipif(
    not REAL_PRE_FEATURE_BAK_FILE.exists(),
    reason="the 2026-09-21 pre-feature snapshot is not on this host",
)
def test_the_real_pre_feature_file_migrates_and_reads_back(tmp_path: Path) -> None:
    """SC-005 / T011, repointed at the file it was always about (T053, 2026-09-22).

    **This test had stopped testing.** It read ``REAL_FILE`` --
    ``civsim-match-store.db`` -- and bailed at ``nothing to migrate`` whenever that
    file was no longer at schema 1.0. Ordinary use of this host migrated it to 1.1 on
    2026-09-21, so from that moment the test skipped at runtime on the only machine
    that has the file, and every assertion below it (including ``len(before) == 10``,
    SC-005's whole claim) became dead code. A `skip` reads as "not applicable here",
    which is exactly how a guard stops guarding without anyone noticing -- the fourth
    instance of this project's own recurring failure, and the first where the check
    was not merely narrow but entirely inert.

    The file it was always about is the timestamped 1.0 backup the migration itself
    took. That one is frozen: ten runs, 97 decision steps, no ``model_calls`` rows.
    Keyed to it, the migration path is exercised on every run on this host, and the
    ``nothing to migrate`` branch below becomes a genuine safety net rather than the
    normal outcome.
    """
    source = REAL_PRE_FEATURE_BAK_FILE
    db_copy = tmp_path / "real.db"
    shutil.copy2(source, db_copy)
    for sidecar in ("-wal", "-shm"):
        side = source.with_name(source.name + sidecar)
        if side.exists():
            shutil.copy2(side, db_copy.with_name(db_copy.name + sidecar))
    real_blobs = source.parent / "blobs"
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
    assert version_before == StoreSchemaVersion(1, 0), (
        f"{source.name} is meant to be the frozen 1.0 snapshot and reports "
        f"{version_before}. Do not soften this into a skip: a skip here is what let "
        f"SC-005 go unasserted for a day (T053)."
    )

    # `orphan_sweep=False` is load-bearing, and the first run of this repointed test is
    # what found out why (T053, 2026-09-22). A *default* write-mode open runs the
    # open-time orphan sweep (contract W6, landed by the 002 lane in `9f200d5`), and this
    # snapshot contains `run-54a3cefb...` -- a run left `preparing` on 2026-09-21 whose
    # identity lock is long gone. The sweep correctly pauses it and writes a
    # `lifecycle_transition` event with `reason: orphaned`. That is a *later, deliberate*
    # write, not the migration mis-reading anything, so asserting V3's and SC-005's "reads
    # back unchanged" against a swept store would be asserting the wrong thing: it would
    # fail on correct behaviour, and papering over it by relaxing the equality would give
    # up the one assertion that proves the migration rewrites nothing.
    #
    # So the two claims are separated. The migration's own fidelity is checked with the
    # sweep off; W6 is then checked on its own below, against real data rather than a
    # fixture -- the first thing in the tree that does.
    store = SqliteMatchStore(db_copy, orphan_sweep=False)
    try:
        assert len(before) == 10, "the 2026-09-21 file holds ten runs (research R2)"
        for run_id, raw in before.items():
            run = store.get_run(run_id)
            assert run is not None
            dumped = run.model_dump(mode="json")
            # A key the *model* gained after this snapshot was frozen is not the migration
            # rewriting anything -- the stored `run_json` on disk is untouched, and the field
            # reads back as its `None` default, meaning "this run never recorded that fact"
            # (T280 added `debug_menu_state` this way). Such keys are allowed through here, but
            # only while they are `None`: a new field that read back as a *value* would mean
            # something invented an answer for a run that never gave one, which is the same
            # defect this assertion exists to catch. Everything the snapshot actually stored is
            # still compared exactly, so a dropped key or a changed value fails as it always did.
            invented = {k: v for k, v in dumped.items() if k not in raw and v is not None}
            assert not invented, (
                f"{run_id}: the migration produced values for fields the 1.0 snapshot never "
                f"stored, which is inventing history, not preserving it: {invented}"
            )
            assert {k: v for k, v in dumped.items() if k in raw} == raw, (
                f"{run_id} did not survive the 1.0 -> 1.1 migration unchanged (V3, SC-005)"
            )
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

    # W6, on real data. Re-open the same migrated copy with the sweep at its default and
    # assert it pauses exactly the runs that are genuinely nobody's -- no lock, no living
    # holder -- and leaves every other run's lifecycle state alone. This is the half of
    # the story the block above deliberately switched off, and it is worth pinning here
    # rather than only in `tests/unit/test_orphans.py`: those fixtures are built to be
    # orphans, this file merely is one.
    swept = SqliteMatchStore(db_copy)
    try:
        paused = {finding.run_id for finding in swept.orphans_paused_on_open}
        assert paused, (
            "the 2026-09-21 snapshot holds at least one run left `preparing` with no "
            "identity lock; a sweep that pauses none of them is not running (W6)"
        )
        for run_id, raw in before.items():
            run = swept.get_run(run_id)
            assert run is not None
            if run_id in paused:
                assert run.lifecycle_state == "paused"
                assert raw["lifecycle_state"] in {"preparing", "playing"}, (
                    "the sweep may only pause a run that was still claiming to be live"
                )
            else:
                assert run.lifecycle_state == raw["lifecycle_state"], (
                    f"{run_id} was not an orphan and the sweep changed it anyway (W6)"
                )
    finally:
        swept.close()


@pytest.mark.skipif(
    not REAL_PRE_FEATURE_BAK_FILE.exists(),
    reason="the 2026-09-21 pre-feature backup file is not on this host",
)
def test_the_real_pre_feature_file_s_priced_runs_reconcile_to_sc_004(tmp_path: Path) -> None:
    """SC-004: a run's total model spend must be available in one read and reconcile with the
    provider's billing. T045 hand-computed this once over the ten runs of the real 2026-09-21
    file and recorded it in validation-results.md as five priced runs summing to **$1.424694**
    (0.294044 + 0.407990 + 0.397258 + 0.241004 + 0.084398, over 93 priced calls); two
    fake-provider runs were unpriced (``cost_usd`` is ``None`` -- ``ModelCallTotals``'
    ``__post_init__`` invariant is that ``None`` means "no call of the run reported a dollar
    amount", never 0 for "unknown") and three runs had no calls at all, so both kinds are
    naturally excluded by skipping ``cost_usd is None`` rather than coerced to zero. That figure
    was previously recorded nowhere else in the suite -- a grep for "1.42", "1.424694" or
    "SC-004" under tests/ found nothing before this test, so a migration change that
    mis-derived a run's cost could pass silently. This derives the total mechanically from the
    store instead of hard-coding a run-by-run breakdown that a future recorded run would break.

    **T051 discovery, stated here because it changes which file this test reads**: this test
    was written to read ``REAL_FILE`` (``civsim-match-store.db``), matching the sibling
    migration test above. On this host, right now, that file no longer holds the ten runs the
    $1.424694 figure was computed from -- ongoing play has grown it to 44 runs, 30 of them
    priced, summing to roughly $13.40 (checked directly while writing this test; not asserted
    here since it is not this feature's recorded figure and will keep moving). It is also
    already at schema 1.1, which is why the sibling migration test's "nothing to migrate"
    runtime skip currently fires on this host and its own ``assert len(before) == 10`` does not
    currently execute either. Rather than assert a stale total against a file that has moved on,
    this test reads the timestamped ``.bak-20260921T152108Z`` copy instead, which this
    conversation confirmed independently holds exactly ten runs, 97 ``decision_steps`` and zero
    (pre-migration) ``model_calls`` rows -- the same ten-run 1.0 shape ``validation-results.md``
    and the sibling test describe. This test still only ever copies that file; it never opens it
    for writing.

    Tolerance: SC-004 commits to reconciling "to the cent", and the recorded night-report figure
    is the cent-rounded $1.42, so the cent-rounded total is the acceptance assertion. The
    unrounded sum is additionally checked against the recorded exact $1.424694 with an
    ``abs=1e-6`` float tolerance to absorb summation-order float noise, not because either total
    is uncertain -- both are deterministic sums of the stored floats.
    """
    db_copy = tmp_path / "real_sc004.db"
    shutil.copy2(REAL_PRE_FEATURE_BAK_FILE, db_copy)
    for sidecar in ("-wal", "-shm"):
        side = REAL_PRE_FEATURE_BAK_FILE.with_name(REAL_PRE_FEATURE_BAK_FILE.name + sidecar)
        if side.exists():
            shutil.copy2(side, db_copy.with_name(db_copy.name + sidecar))

    store = SqliteMatchStore(db_copy)
    try:
        run_ids = [run.run_id for run in store.list_runs()]
        assert len(run_ids) == 10, (
            "the 2026-09-21 pre-feature snapshot holds ten runs (research R2)"
        )

        priced_total = 0.0
        priced_run_count = 0
        for run_id in run_ids:
            totals = store.model_call_totals(run_id)
            if totals.cost_usd is None:
                continue  # unpriced fake-provider run, or a run with no calls: excluded (T045)
            priced_total += totals.cost_usd
            priced_run_count += 1

        assert priced_run_count == 5, "T045 recorded five priced runs among the ten"
        assert round(priced_total, 2) == 1.42
        assert priced_total == pytest.approx(1.424694, abs=1e-6)
    finally:
        store.close()


def _capture_ids(store: SqliteMatchStore, run_id: str) -> list[str]:
    rows = store._conn.execute(  # noqa: SLF001 -- white-box enumeration for the audit
        "SELECT capture_id FROM captures WHERE run_id = ?", (run_id,)
    ).fetchall()
    return [str(row[0]) for row in rows]
