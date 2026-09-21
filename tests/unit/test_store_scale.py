"""SC-002 and SC-008, measured on the suite's own machine (T018, T025).

The timings are asserted with 2x headroom over the criteria and printed, so the validation
results can quote a measurement rather than an expectation.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from civsim_harness.models.run import LifecycleState, RecordCompletenessStatus
from civsim_harness.store.contract import RunQuery, RunSort
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import (
    at,
    make_config,
    make_run,
    make_save_point,
    make_turn_cycle_record,
)


def test_sc002_a_300_turn_run_of_100_steps_each_round_trips_in_order(tmp_path: Path) -> None:
    store = SqliteMatchStore(tmp_path / "big.db")
    try:
        store.create_run(make_run("big", "big-cfg"), make_config("big-cfg"))
        started = time.perf_counter()
        for turn in range(1, 301):
            store.write_save_point(make_save_point(f"big-sp{turn}-0", "big", turn))
            store.write_turn_cycle(make_turn_cycle_record("big", turn, num_steps=100))
        write_s = time.perf_counter() - started

        started = time.perf_counter()
        steps_seen = 0
        for turn in range(1, 301):
            record = store.get_turn_cycle("big", turn)
            assert record is not None
            assert [b.step.step_index for b in record.steps] == list(range(1, 101))
            steps_seen += len(record.steps)
        read_s = time.perf_counter() - started
        assert steps_seen == 30_000
        assert store.highest_recorded_turn("big") == 300
        assert store.turn_gaps("big") == []
        assert store.get_run("big").record_completeness_status is RecordCompletenessStatus.COMPLETE
        assert store.model_call_totals("big").call_count == 30_000
        print(f"SC-002: 30,000 steps written in {write_s:.2f}s, read back in {read_s:.2f}s")
    finally:
        store.close()


def test_sc008_a_thousand_runs_list_in_under_two_seconds_and_totals_in_under_one(
    tmp_path: Path,
) -> None:
    store = SqliteMatchStore(tmp_path / "many.db")
    try:
        states = ["playing", "paused", "finished", "failed"]
        for i in range(1000):
            state = states[i % 4]
            run = make_run(
                f"run-{i:04d}",
                f"cfg-{i:04d}",
                lifecycle_state=state,
                stop_resolution="turn_reached"
                if state == "finished"
                else ("unrecoverable_failure" if state == "failed" else None),
                started_at=at(i),
                record_completeness_status="complete" if i % 5 else "has_gaps",
                comparability_status="comparable" if i % 3 else "visually_degraded",
            )
            store.create_run(run, make_config(f"cfg-{i:04d}", seed_set_id=f"ss-{i % 40}"))
            if state == "finished" and i % 8 == 2:
                store.archive_run(run.run_id, by="t", at=datetime(2026, 9, 21, tzinfo=UTC))
        assert store.query_runs(RunQuery()).total == 1000

        queries = [
            RunQuery(
                sort=sort,
                page=page,
                page_size=25,
                seed_set_id=seed,
                archived=archived,
                lifecycle_states=states_filter,
                completeness=completeness,
            )
            for sort in RunSort
            for page in (1, 3, 40)
            for seed in (None, "ss-7")
            for archived in (None, True)
            for states_filter in (None, frozenset({LifecycleState.FINISHED}))
            for completeness in (None, frozenset({RecordCompletenessStatus.HAS_GAPS}))
        ]
        slowest = 0.0
        for query in queries:
            started = time.perf_counter()
            page = store.query_runs(query)
            elapsed = time.perf_counter() - started
            slowest = max(slowest, elapsed)
            assert page.total >= len(page.runs)
        assert slowest < 1.0, f"slowest listing {slowest:.3f}s (SC-008 allows 2s)"

        # One run with 300 turns x 10 calls: totals in well under a second.
        store.create_run(make_run("calls", "calls-cfg"), make_config("calls-cfg"))
        for turn in range(1, 301):
            store.write_save_point(make_save_point(f"calls-sp{turn}-0", "calls", turn))
            store.write_turn_cycle(
                make_turn_cycle_record("calls", turn, num_steps=10, cost_usd=0.002)
            )
        started = time.perf_counter()
        totals = store.model_call_totals("calls")
        totals_s = time.perf_counter() - started
        assert totals.call_count == 3000 and totals.cost_usd == pytest.approx(6.0)
        assert totals_s < 0.5, f"totals took {totals_s:.3f}s (SC-008 allows 1s)"
        print(
            f"SC-008: {len(queries)} listings over 1,001 runs, slowest {slowest * 1000:.1f} ms; "
            f"3,000-call totals in {totals_s * 1000:.1f} ms"
        )
    finally:
        store.close()
