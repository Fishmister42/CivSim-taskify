"""Export / import round trips and every refusal (T035; 003 US4; contract V4-V6; SC-006)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from civsim_harness.errors import BundleError
from civsim_harness.store.bundle import (
    ARCHIVE_SUFFIX,
    MANIFEST_NAME,
    read_bundle,
    render_bundle,
    write_bundle,
)
from civsim_harness.store.contract import BUNDLE_FORMAT, RunRecordSet
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import (
    at,
    make_capture,
    make_config,
    make_event,
    make_run,
    make_save_point,
    make_turn_cycle_record,
)

RUN = "run-x"


def populate(store: SqliteMatchStore, *, run_id: str = RUN, parent: str | None = None) -> None:
    """A finished, archived run: 3 turns (turn 2 replayed), 6 captures (2 withheld), an
    archive event and eligible saves -- the shape spec US4/AC1 describes."""
    store.create_run(
        make_run(
            run_id,
            f"{run_id}-cfg",
            started_at=at(0),
            parent_run_id=parent,
            parent_turn=1 if parent else None,
        ),
        make_config(f"{run_id}-cfg", seed_set_id="seed-1"),
    )
    captures: dict[int, dict[int, list[str]]] = {
        1: {1: [f"{run_id}-c1", f"{run_id}-c2"]},
        2: {1: [f"{run_id}-c3", f"{run_id}-c4"]},
        3: {1: [f"{run_id}-c5", f"{run_id}-c6"]},
    }
    for turn in (1, 2, 3):
        store.write_save_point(make_save_point(f"{run_id}-sp{turn}-0", run_id, turn))
        if turn == 2:
            store.write_turn_cycle(
                make_turn_cycle_record(
                    run_id, 2, 0, num_steps=2, is_authoritative=True, cost_usd=0.01
                )
            )
            store.mark_turn_superseded(run_id, 2, 0)
            record = make_turn_cycle_record(
                run_id, 2, 1, num_steps=3, cost_usd=0.01, captures_by_step={1: captures[2][1]}
            )
        else:
            record = make_turn_cycle_record(
                run_id,
                turn,
                num_steps=3,
                cost_usd=0.01,
                yields={"science": turn * 2},
                cities=turn,
                captures_by_step={1: captures[turn][1]},
            )
        store.write_turn_cycle(record)
        first_step = record.steps[0].step.decision_step_id
        kept_id, withheld_id = captures[turn][1]
        kept, blob = make_capture(kept_id, run_id, turn, first_step, blob=f"frame-{turn}".encode())
        store.write_capture(kept, blob)
        if turn in (1, 3):
            withheld, _ = make_capture(withheld_id, run_id, turn, first_step, blob=None)
            store.write_capture(withheld, None)
        else:
            second, blob2 = make_capture(withheld_id, run_id, turn, first_step, blob=b"frame-2b")
            store.write_capture(second, blob2)
    store.write_run_event(make_event(f"{run_id}-e1", run_id, "save_taken", turn_number=1))
    store.update_run(
        run_id, lifecycle_state="finished", stop_resolution="turn_reached", ended_at=at(30)
    )
    store.archive_run(run_id, by="operator", at=datetime(2026, 9, 21, 13, tzinfo=UTC))


@pytest.fixture
def source(tmp_path: Path) -> SqliteMatchStore:
    store = SqliteMatchStore(tmp_path / "source" / "s.db")
    populate(store)
    yield store
    store.close()


@pytest.fixture
def target(tmp_path: Path) -> SqliteMatchStore:
    store = SqliteMatchStore(tmp_path / "target" / "t.db")
    yield store
    store.close()


def test_export_carries_every_attempt_event_call_save_and_kept_image(
    source: SqliteMatchStore,
) -> None:
    records = source.export_run(RUN)
    counts = records.counts()
    assert counts["turn_cycles"] == 4 and counts["decision_steps"] == 2 + 3 + 3 + 3
    assert counts["captures"] == 6 and counts["images"] == 4
    assert counts["model_calls"] == 11
    assert [e.event_type.value for e in records.run_events] == ["save_taken", "run_archived"]
    assert all(s.retention_status.value == "eligible" for s in records.save_points)
    assert records.run.archived_at is not None


def test_directory_and_archive_round_trip_identically(
    source: SqliteMatchStore, target: SqliteMatchStore, tmp_path: Path
) -> None:
    exported = source.export_run(RUN)
    info = source.store_info()
    archive = write_bundle(
        exported,
        tmp_path / "out",
        store_schema_version=str(info.schema_version),
        source_store_id=info.store_id,
        archive=True,
    )
    assert archive.name == f"{RUN}{ARCHIVE_SUFFIX}"
    directory = tmp_path / "out" / f"{RUN}.civsim-bundle"
    assert directory.is_dir()

    manifest_dir, from_dir = read_bundle(directory)
    manifest_tar, from_tar = read_bundle(archive)
    assert manifest_dir == manifest_tar
    assert from_dir == from_tar == exported
    assert manifest_dir.bundle_format == BUNDLE_FORMAT
    assert manifest_dir.counts == exported.counts()
    assert manifest_dir.source_store_id == info.store_id
    assert all("\\" not in p and not p.startswith("/") for p in manifest_dir.files)
    assert set(manifest_dir.images) == {f"{RUN}-c1", f"{RUN}-c3", f"{RUN}-c4", f"{RUN}-c5"}

    target.import_run(from_tar)
    assert target.export_run(RUN) == exported  # V4: counts, ordering, statuses, bytes
    assert target.store_info().counts == {k: v for k, v in source.store_info().counts.items()}
    for capture_id in (f"{RUN}-c2", f"{RUN}-c6"):
        assert target.get_capture_image(capture_id).status.value == "withheld"
    assert target.get_capture_image(f"{RUN}-c1").content == b"frame-1"
    assert target.get_run(RUN) == source.get_run(RUN)
    assert target.list_model_calls(RUN) == source.list_model_calls(RUN)
    assert target.model_call_totals(RUN) == source.model_call_totals(RUN)


def test_import_refuses_an_existing_run_id_naming_it(
    source: SqliteMatchStore, target: SqliteMatchStore
) -> None:
    exported = source.export_run(RUN)
    target.import_run(exported)
    with pytest.raises(BundleError) as info:
        target.import_run(exported)
    assert info.value.detail["run_id"] == RUN
    assert target.store_info().counts["runs"] == 1


def test_a_tampered_file_a_missing_image_and_a_newer_format_are_each_refused(
    source: SqliteMatchStore, tmp_path: Path
) -> None:
    exported = source.export_run(RUN)
    info = source.store_info()

    tampered = write_bundle(
        exported, tmp_path / "t1", store_schema_version="1.1", source_store_id=info.store_id
    )
    events = tampered / "run_events.jsonl"
    events.write_bytes(events.read_bytes() + b"\n")
    with pytest.raises(BundleError) as err:
        read_bundle(tampered)
    assert err.value.detail["path"] == "run_events.jsonl"

    missing = write_bundle(
        exported, tmp_path / "t2", store_schema_version="1.1", source_store_id=info.store_id
    )
    shutil.rmtree(missing / "images")
    with pytest.raises(BundleError) as err:
        read_bundle(missing)
    assert "path" in err.value.detail

    newer = write_bundle(
        exported, tmp_path / "t3", store_schema_version="1.1", source_store_id=info.store_id
    )
    manifest = json.loads((newer / MANIFEST_NAME).read_text())
    manifest["bundle_format"] = BUNDLE_FORMAT + 1
    (newer / MANIFEST_NAME).write_text(json.dumps(manifest))
    with pytest.raises(BundleError) as err:
        read_bundle(newer)
    assert err.value.detail["bundle_format"] == BUNDLE_FORMAT + 1

    schema_newer = write_bundle(
        exported, tmp_path / "t4", store_schema_version="7.0", source_store_id=info.store_id
    )
    with pytest.raises(BundleError) as err:
        read_bundle(schema_newer)
    assert err.value.detail["bundle_schema_version"] == "7.0"


def test_render_refuses_a_record_set_missing_a_kept_image(source: SqliteMatchStore) -> None:
    exported = source.export_run(RUN)
    stripped = RunRecordSet(**{**exported.__dict__, "images": {}})
    with pytest.raises(BundleError):
        render_bundle(stripped, store_schema_version="1.1", source_store_id="x")


def test_import_refuses_a_set_whose_records_belong_to_another_run(
    source: SqliteMatchStore, target: SqliteMatchStore
) -> None:
    exported = source.export_run(RUN)
    foreign = RunRecordSet(
        **{
            **exported.__dict__,
            "run": make_run(
                "other", f"{RUN}-cfg", lifecycle_state="finished", stop_resolution="turn_reached"
            ),
        }
    )
    with pytest.raises(BundleError):
        target.import_run(foreign)


def test_a_branch_with_an_absent_parent_imports_and_is_reported(
    tmp_path: Path, target: SqliteMatchStore
) -> None:
    src = SqliteMatchStore(tmp_path / "branch-src" / "s.db")
    try:
        populate(src, run_id="child", parent="ghost-parent")
        target.import_run(src.export_run("child"))
    finally:
        src.close()
    assert target.store_info().runs_with_absent_parent == ("child",)


def test_write_bundle_refuses_to_overwrite(source: SqliteMatchStore, tmp_path: Path) -> None:
    exported = source.export_run(RUN)
    write_bundle(exported, tmp_path / "once", store_schema_version="1.1", source_store_id="x")
    with pytest.raises(BundleError):
        write_bundle(exported, tmp_path / "once", store_schema_version="1.1", source_store_id="x")
