"""A **genuine** schema-1.0 store file, built with 002's own DDL and rows written the way the
002 adapter wrote them (003 T010) -- shared by the schema and CLI suites so the migration is
exercised against the real legacy shape rather than a fixture that happens to look like it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from civsim_harness.store import schema as store_schema
from store_support.builders import (
    make_capture,
    make_config,
    make_failed_call,
    make_run,
    make_save_point,
    make_turn_cycle_record,
)


def build_legacy_file(db_path: Path) -> dict[str, object]:
    """Two runs; run-a has a two-attempt turn of three steps and turn 2 + a failed call; one
    kept and one withheld capture (the kept image written to ``blobs/`` the 002 way)."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(store_schema.LEGACY_SCHEMA_SQL_1_0)
    runs = {
        "run-a": make_run(
            "run-a", "cfg-a", lifecycle_state="finished", stop_resolution="turn_reached"
        ),
        "run-b": make_run("run-b", "cfg-b", lifecycle_state="paused"),
    }
    for run_id, run in runs.items():
        conn.execute(
            "INSERT INTO runs (run_id, config_id, lifecycle_state, parent_run_id, archived_at, "
            "run_json, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                run.config_id,
                run.lifecycle_state.value,
                None,
                None,
                run.model_dump_json(),
                make_config(run.config_id, seed_set_id="seed-1").model_dump_json(by_alias=True),
            ),
        )
    records = [
        make_turn_cycle_record(
            "run-a", 1, 0, num_steps=3, is_authoritative=False, outcome="abandoned"
        ),
        make_turn_cycle_record("run-a", 1, 1, num_steps=3, cost_usd=0.01),
        make_turn_cycle_record("run-a", 2, 0, num_steps=2, cost_usd=0.02),
        make_turn_cycle_record("run-b", 1, 0, num_steps=1),
    ]
    for record in records:
        tc = record.turn_cycle
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
    failed = make_failed_call("run-a", "run-a-t3-a0", "run-a-t3-a0-step1", "run-a-failed-call")
    conn.execute(
        "INSERT INTO model_calls (model_call_id, run_id, decision_step_id, call_json) "
        "VALUES (?, ?, ?, ?)",
        (failed.model_call_id, failed.run_id, failed.decision_step_id, failed.model_dump_json()),
    )
    for run_id, turns in (("run-a", (1, 2)), ("run-b", (1, 2))):
        for turn in turns:
            sp = make_save_point(f"{run_id}-sp{turn}-0", run_id, turn)
            conn.execute(
                "INSERT INTO save_points (save_point_id, run_id, turn_number, retention_status, "
                "save_json) VALUES (?, ?, ?, ?, ?)",
                (
                    sp.save_point_id,
                    sp.run_id,
                    sp.turn_number,
                    sp.retention_status.value,
                    sp.model_dump_json(),
                ),
            )
    blob = b"PNG-bytes-of-a-kept-frame"
    kept, _ = make_capture("cap-kept", "run-a", 1, "run-a-t1-a1-step1", blob=blob)
    withheld, _ = make_capture("cap-withheld", "run-a", 1, "run-a-t1-a1-step2", blob=None)
    assert kept.blob_ref is not None
    blob_path = db_path.parent / "blobs" / kept.blob_ref[:2] / kept.blob_ref
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(blob)
    for capture in (kept, withheld):
        conn.execute(
            "INSERT INTO captures (capture_id, run_id, decision_step_id, blob_ref, capture_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                capture.capture_id,
                capture.run_id,
                capture.decision_step_id,
                capture.blob_ref,
                capture.model_dump_json(),
            ),
        )
    conn.commit()
    conn.close()
    return {"runs": runs, "steps": 9, "pre_existing_calls": 1, "kept_blob": blob}
