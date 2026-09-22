"""Conformance suite for `MatchTrackingStore` (003; contracts/match-tracking-store.md).

Runs against `SqliteMatchStore`. Every test names the rule it demonstrates -- W (writing), R
(reading), T (trends), V (evolution/portability) -- and the spec scenario (`US1/AC3`) or success
criterion it discharges. The inherited 002 floor is asserted by
`tests/contract/test_match_store_port.py`, which must keep passing unchanged.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.errors import StoreWriteError
from civsim_harness.models.run import (
    ComparabilityStatus,
    LifecycleState,
    RecordCompletenessStatus,
)
from civsim_harness.store.contract import (
    CaptureImageStatus,
    ExclusionReason,
    RunQuery,
    RunSort,
    TrendQuery,
)
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from store_support.builders import (
    at,
    make_capture,
    make_config,
    make_event,
    make_failed_call,
    make_run,
    make_save_point,
    make_turn_cycle_record,
    record_run,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteMatchStore]:
    adapter = SqliteMatchStore(tmp_path / "match.db")
    yield adapter
    adapter.close()


def _new_run(store: SqliteMatchStore, run_id: str, **kwargs: Any) -> None:
    store.create_run(make_run(run_id, f"{run_id}-cfg", **kwargs), make_config(f"{run_id}-cfg"))


# ==========================================================================
# US1 -- a turn is on the record before the game moves on (W1-W3, FR-001..010)
# ==========================================================================


def test_us1_ac1_a_250_step_turn_reads_back_in_order_with_its_calls_as_rows(
    store: SqliteMatchStore,
) -> None:
    _new_run(store, "r")
    store.write_save_point(make_save_point("r-sp1-0", "r", 1))
    record = make_turn_cycle_record("r", 1, num_steps=250, cost_usd=0.001)
    store.write_turn_cycle(record)

    back = store.get_turn_cycle("r", 1)
    assert back == record
    assert [b.step.step_index for b in back.steps] == list(range(1, 251))

    rows = store.list_model_calls("r")  # W1: rows, from the same transaction
    assert len(rows) == 250
    assert [(row.turn_number, row.step_index) for row in rows] == [(1, i) for i in range(1, 251)]
    assert rows[7].call == record.steps[7].model_call
    totals = store.model_call_totals("r")
    assert totals.call_count == 250 and totals.priced_call_count == 250
    assert totals.cost_usd == pytest.approx(0.25)


def test_us1_ac3_an_identical_rewrite_of_attempt_0_is_accepted_exactly_once(
    store: SqliteMatchStore,
) -> None:
    _new_run(store, "r")
    store.write_save_point(make_save_point("r-sp1-0", "r", 1))
    record = make_turn_cycle_record("r", 1, num_steps=4)
    first = store.write_turn_cycle(record)
    second = store.write_turn_cycle(record)
    assert first == second
    assert len(store.list_turn_attempts("r", 1)) == 1
    assert len(store.list_model_calls("r")) == 4  # W2: no duplicated rows


def test_w2_a_call_row_with_different_content_under_the_same_id_is_refused(
    store: SqliteMatchStore,
) -> None:
    _new_run(store, "r")
    store.write_save_point(make_save_point("r-sp1-0", "r", 1))
    record = make_turn_cycle_record("r", 1, num_steps=1)
    changed = record.steps[0].model_call.model_copy(update={"latency_ms": 999})
    store.write_model_call(changed)  # the row lands first, with different content
    with pytest.raises(StoreWriteError):
        store.write_turn_cycle(record)
    assert store.get_turn_cycle("r", 1) is None  # the whole turn rolled back with it


def test_us1_ac4_a_superseded_attempt_stays_retrievable_and_is_not_a_gap(
    store: SqliteMatchStore,
) -> None:
    _new_run(store, "r")
    store.write_save_point(make_save_point("r-sp7-0", "r", 7))
    store.write_turn_cycle(make_turn_cycle_record("r", 7, 0, num_steps=2))
    store.mark_turn_superseded("r", 7, 0)
    store.write_turn_cycle(make_turn_cycle_record("r", 7, 1, num_steps=3))

    authoritative = store.get_turn_cycle("r", 7)
    assert authoritative is not None and authoritative.turn_cycle.attempt_index == 1
    abandoned = store.get_turn_cycle_attempt("r", 7, 0)  # R4: exactly what was asked for
    assert abandoned is not None
    assert abandoned.turn_cycle.attempt_index == 0
    assert abandoned.turn_cycle.is_authoritative is False
    assert abandoned.turn_cycle.outcome.value == "abandoned"
    assert store.get_turn_cycle_attempt("r", 7, 5) is None
    summaries = store.list_turn_attempts("r", 7)
    assert [(s.attempt_index, s.is_authoritative, s.step_count) for s in summaries] == [
        (0, False, 2),
        (1, True, 3),
    ]
    assert store.turn_gaps("r") == [1, 2, 3, 4, 5, 6]  # 7 itself is not a gap
    assert len(store.list_model_calls("r", turn=7)) == 5  # both attempts' calls are rows


def test_us1_ac5_a_quicksave_with_no_turn_becomes_a_gap_once_the_run_stops(
    store: SqliteMatchStore,
) -> None:
    record_run(store, "r", turns=2)
    store.write_save_point(make_save_point("r-sp3-0", "r", 3))  # the in-flight turn

    assert store.turn_gaps("r") == []  # still playing: normal shape (FR-007)
    # Not a gap -- and not `complete` either (T298). The carve-out above holds only while the
    # run really is cycling, and a run that halted on a failed write keeps the `playing` it last
    # wrote, so these two shapes are indistinguishable from the record. `in_flight` says that
    # rather than calling a possibly-lost turn a whole record.
    assert store.record_completeness("r") is RecordCompletenessStatus.IN_FLIGHT
    assert store.highest_recorded_turn("r") == 3  # R7 counts the trailing save point

    store.update_run("r", lifecycle_state="paused")  # the writer stopped
    assert store.turn_gaps("r") == [3]
    assert store.record_completeness("r") is RecordCompletenessStatus.HAS_GAPS
    # W3 / FR-010: persisted by the store itself -- nobody called refresh_run_completeness.
    assert store.get_run("r").record_completeness_status is RecordCompletenessStatus.HAS_GAPS


def test_us1_ac6_a_withheld_capture_is_recorded_with_its_reason_and_no_image(
    store: SqliteMatchStore,
) -> None:
    _new_run(store, "r")
    withheld, _ = make_capture(
        "c-w", "r", 1, "r-t1-a0-step1", blob=None, withheld_reason="geometry_mismatch"
    )
    store.write_capture(withheld, None)
    image = store.get_capture_image("c-w")
    assert image.status is CaptureImageStatus.WITHHELD
    assert image.withheld_reason is not None and image.withheld_reason.value == "geometry_mismatch"
    assert image.content is None
    assert store.get_capture_blob("c-w") is None  # E4
    assert store.get_capture("c-w") is not None  # the record is the evidence


def test_w3_the_store_persists_completeness_on_every_write_that_can_change_it(
    store: SqliteMatchStore,
) -> None:
    record_run(store, "r", turns=3)
    assert store.get_run("r").record_completeness_status is RecordCompletenessStatus.COMPLETE
    store.mark_turn_superseded("r", 2, 0)  # no replacement -> turn 2 has no authoritative attempt
    assert store.turn_gaps("r") == [2]
    assert store.get_run("r").record_completeness_status is RecordCompletenessStatus.HAS_GAPS
    store.write_turn_cycle(make_turn_cycle_record("r", 2, 1, num_steps=1))
    assert store.get_run("r").record_completeness_status is RecordCompletenessStatus.COMPLETE
    # A sparse attempt (steps 1, 2, 4) is a step gap the store also notices (SC-003).
    store.write_save_point(make_save_point("r-sp4-0", "r", 4))
    store.write_turn_cycle(make_turn_cycle_record("r", 4, step_indices=[1, 2, 4]))
    assert store.step_gaps("r", 4) == [3]
    assert store.get_run("r").record_completeness_status is RecordCompletenessStatus.HAS_GAPS


def test_w5_a_read_only_reader_sees_the_last_complete_turn_and_cannot_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "shared.db"
    writer = SqliteMatchStore(path)
    record_run(writer, "r", turns=1)
    writer.write_save_point(make_save_point("r-sp2-0", "r", 2))
    reader = SqliteMatchStore(path, read_only=True)

    entered = threading.Event()
    release = threading.Event()
    original = SqliteMatchStore._persist_completeness

    def blocking(self: SqliteMatchStore, conn: Any, run_id: str) -> None:
        entered.set()
        release.wait(10)
        original(self, conn, run_id)

    monkeypatch.setattr(SqliteMatchStore, "_persist_completeness", blocking)
    turn_two = make_turn_cycle_record("r", 2, num_steps=3)
    thread = threading.Thread(target=writer.write_turn_cycle, args=(turn_two,))
    thread.start()
    try:
        assert entered.wait(10), "the writer never reached the middle of its transaction"
        # Mid-transaction: the reader sees turn 1 and not the partial turn 2.
        assert reader.get_turn_cycle("r", 1) is not None
        assert reader.get_turn_cycle("r", 2) is None
        assert reader.list_model_calls("r", turn=2) == []
        with pytest.raises(StoreWriteError):
            reader.write_run_event(make_event("e", "r", "save_taken"))
        with pytest.raises(StoreWriteError):
            reader.create_run(make_run("x"), make_config("x-cfg"))
    finally:
        release.set()
        thread.join(10)
    assert reader.get_turn_cycle("r", 2) == turn_two
    assert reader.read_only is True
    reader.close()
    writer.close()


# ==========================================================================
# US2 -- one published contract (R1-R8, FR-011..017)
# ==========================================================================


def _populate_catalog(store: SqliteMatchStore, count: int = 200) -> None:
    states = ["playing", "paused", "finished", "failed", "interrupted"]
    for i in range(count):
        state = states[i % len(states)]
        run = make_run(
            f"run-{i:04d}",
            f"cfg-{i:04d}",
            lifecycle_state=state,
            stop_resolution="turn_reached"
            if state == "finished"
            else ("unrecoverable_failure" if state == "failed" else None),
            started_at=None if i % 50 == 7 else at(i),
            ended_at=at(i + 5) if state in {"finished", "failed"} else None,
            record_completeness_status="has_gaps" if i % 3 == 0 else "complete",
            comparability_status="visually_degraded" if i % 4 == 0 else "comparable",
        )
        store.create_run(run, make_config(f"cfg-{i:04d}", seed_set_id=f"ss-{i % 4}"))
        if state == "finished" and i % 10 == 2:
            store.archive_run(run.run_id, by="t", at=datetime(2026, 9, 21, tzinfo=UTC))


def test_us2_ac1_query_runs_pages_sorts_filters_and_counts_with_archived_like_any_other(
    store: SqliteMatchStore,
) -> None:
    _populate_catalog(store)
    page = store.query_runs(RunQuery(page=2, page_size=25, seed_set_id="ss-1"))
    assert page.total == 50 and len(page.runs) == 25 and page.page == 2
    ids = [r.run_id for r in page.runs]
    # ss-1 is every i % 4 == 1; run-0057 and run-0157 have no started_at and so sort last of
    # the 50, by run_id, landing at the end of page 2 (R2: NULLs last); the other 48 descend
    # by started_at, so page 1 holds 197..97 and page 2 opens at 93.
    assert ids[0] == "run-0093" and ids[-2:] == ["run-0057", "run-0157"]
    assert ids[:-2] == sorted(ids[:-2], reverse=True)
    assert all(r.started_at is None for r in page.runs[-2:])
    assert all(r.started_at is not None for r in page.runs[:-2])

    # NULL started_at sorts last in every timestamp order.
    everything = store.query_runs(RunQuery(page_size=500))
    assert everything.total == 200
    assert [r.run_id for r in everything.runs][-4:] == [
        "run-0007",
        "run-0057",
        "run-0107",
        "run-0157",
    ]
    ascending = store.query_runs(RunQuery(page_size=500, sort=RunSort.STARTED_AT_ASC))
    assert [r.run_id for r in ascending.runs][0] == "run-0000"
    assert [r.run_id for r in ascending.runs][-1] == "run-0157"

    archived = store.query_runs(RunQuery(page_size=500, archived=True))
    assert archived.total == 20 and all(r.archived_at is not None for r in archived.runs)
    finished = store.query_runs(
        RunQuery(page_size=500, lifecycle_states=frozenset({LifecycleState.FINISHED}))
    )
    assert finished.total == 40  # the twenty archived ones appear like any other (FR-011)
    assert {r.run_id for r in archived.runs} <= {r.run_id for r in finished.runs}
    gapped = store.query_runs(
        RunQuery(page_size=500, completeness=frozenset({RecordCompletenessStatus.HAS_GAPS}))
    )
    assert gapped.total == 67
    degraded = store.query_runs(
        RunQuery(page_size=500, comparability=frozenset({ComparabilityStatus.VISUALLY_DEGRADED}))
    )
    assert degraded.total == 50
    combined = store.query_runs(
        RunQuery(
            page_size=500,
            seed_set_id="ss-0",
            archived=False,
            lifecycle_states=frozenset({LifecycleState.FINISHED, LifecycleState.FAILED}),
        )
    )
    # ss-0 finished runs are i = 12 mod 20 and every one of them is archived; the failed
    # ones are i = 8 mod 20 -- ten runs -- and none is.
    assert combined.total == 10
    assert all(r.lifecycle_state is LifecycleState.FAILED for r in combined.runs)
    by_id = store.query_runs(RunQuery(page_size=3, sort=RunSort.RUN_ID_ASC))
    assert [r.run_id for r in by_id.runs] == ["run-0000", "run-0001", "run-0002"]
    assert store.query_runs(RunQuery(page=99)).runs == ()

    listed = store.list_runs()  # R1 / E1
    assert len(listed) == 200
    assert {r.lifecycle_state.value for r in listed} == {
        "playing",
        "paused",
        "finished",
        "failed",
        "interrupted",
    }


def test_us2_ac3_model_call_totals_equal_the_sums_embedded_in_the_steps(
    store: SqliteMatchStore,
) -> None:
    record_run(store, "r", turns=3, num_steps=8, cost_usd=0.0125)
    failed = make_failed_call("r", "r-t4-a0", "r-t4-a0-step1", "r-failed")
    store.write_model_call(failed)  # a call that produced no step (FR-042) is a row too

    embedded = sum(
        b.model_call.cost.amount_usd or 0.0
        for turn in (1, 2, 3)
        for b in store.get_turn_cycle("r", turn).steps
    )
    totals = store.model_call_totals("r")
    assert totals.call_count == 25 and totals.priced_call_count == 25
    assert totals.cost_usd == pytest.approx(embedded + 0.001)
    assert totals.retry_count == 1 and totals.fallback_count == 0
    assert totals.calls_by_outcome == {"decision_returned": 24, "failed": 1}
    rows = store.list_model_calls("r")
    assert rows[-1].call.model_call_id == "r-failed" and rows[-1].turn_number is None
    assert [r.step_index for r in store.list_model_calls("r", turn=2)] == list(range(1, 9))
    assert len(store.list_model_calls("r", turn=2, step=5)) == 1


def test_us2_ac4_r5_capture_image_answers_all_four_ways(store: SqliteMatchStore) -> None:
    _new_run(store, "r")
    kept, blob = make_capture("c-kept", "r", 1, "s1", blob=b"\x89PNG-frame")
    store.write_capture(kept, blob)
    withheld, _ = make_capture("c-wh", "r", 1, "s1", blob=None)
    store.write_capture(withheld, None)
    lost, lost_blob = make_capture("c-lost", "r", 1, "s2", blob=b"gone-later")
    store.write_capture(lost, lost_blob)
    assert lost.blob_ref is not None
    store._blob_path(lost.blob_ref).unlink()  # noqa: SLF001 -- a store copied without its blobs

    assert store.get_capture_image("c-kept").content == b"\x89PNG-frame"
    assert store.get_capture_blob("c-kept") == b"\x89PNG-frame"
    assert store.get_capture_image("c-wh").status is CaptureImageStatus.WITHHELD
    missing = store.get_capture_image("c-lost")
    assert missing.status is CaptureImageStatus.MISSING and missing.blob_ref == lost.blob_ref
    assert store.get_capture("c-lost") == lost  # the record is intact
    assert store.get_capture_blob("c-lost") is None
    assert store.get_capture_image("nope").status is CaptureImageStatus.NO_SUCH_CAPTURE


def test_us2_r5_list_captures_enumerates_one_run_s_records_without_reading_a_blob(
    store: SqliteMatchStore,
) -> None:
    """`list_captures` answers "what did this run capture, and what happened to each frame"
    from the records alone -- including for a run whose blobs are gone from disk, which
    `export_run` (the only other enumeration) refuses outright.
    """
    _new_run(store, "r")
    _new_run(store, "other")
    kept, blob = make_capture("c-kept", "r", 1, "s1", blob=b"\x89PNG-frame")
    store.write_capture(kept, blob)
    withheld, _ = make_capture(
        "c-wh", "r", 2, "s2", blob=None, withheld_reason="provenance_failure"
    )
    store.write_capture(withheld, None)
    elsewhere, other_blob = make_capture("c-other", "other", 1, "s1", blob=b"not-ours")
    store.write_capture(elsewhere, other_blob)
    assert kept.blob_ref is not None
    store._blob_path(kept.blob_ref).unlink()  # noqa: SLF001 -- a store copied without its blobs

    captures = store.list_captures("r")
    assert [capture.capture_id for capture in captures] == ["c-kept", "c-wh"]
    assert captures[0] == kept and captures[1] == withheld
    assert captures[0].shown_to_agent is True
    assert captures[1].withheld_reason is not None and captures[1].blob_ref is None
    assert store.list_captures("no-such-run") == []


def test_us2_ac5_e5_a_configuration_is_resolved_by_run_id_and_nothing_else(
    store: SqliteMatchStore,
) -> None:
    _new_run(store, "r")
    run = store.get_run("r")
    assert run.config_id != run.run_id
    assert store.get_run_configuration("r") is not None
    assert store.get_run_configuration(run.config_id) is None


# ==========================================================================
# US3 -- trends only from records that can bear them (T1-T4, FR-018..021)
# ==========================================================================


def _science(turn: int) -> dict[str, Any]:
    return {"science": 10 + turn, "culture": 5}


def test_us3_ac1_gapped_runs_are_absent_from_every_series_and_named_with_their_gap(
    store: SqliteMatchStore,
) -> None:
    for i in range(5):
        record_run(
            store,
            f"run-{i}",
            turns=6,
            seed_set_id="ss",
            lifecycle_state="finished",
            yields_for=_science,
            cities=1,
            skip_turns=[3] if i in (1, 3) else (),
        )
    response = store.metric_series(TrendQuery(seed_set_id="ss", metrics=("science",)))
    assert sorted(s.run_id for s in response.series) == ["run-0", "run-2", "run-4"]
    assert all(len(s.points) == 6 and not s.in_progress for s in response.series)
    assert [(e.run_id, e.reason, e.gaps) for e in response.excluded] == [
        ("run-1", ExclusionReason.HAS_GAPS, (3,)),
        ("run-3", ExclusionReason.HAS_GAPS, (3,)),
    ]
    assert response.metric_names == ("city_count", "culture", "science")
    everything = store.metric_series(TrendQuery(run_ids=("run-0", "run-1", "ghost")))
    assert [e.run_id for e in everything.excluded] == ["ghost", "run-1"]
    assert {s.metric for s in everything.series} == {"city_count", "culture", "science"}


def test_us3_ac2_divergence_is_the_first_turn_the_runs_did_something_different(
    store: SqliteMatchStore,
) -> None:
    def actions(diverge_at: int):
        def for_turn(turn: int):
            if turn >= diverge_at:
                return [("units.found_city", {"target": 65536})]
            return [("units.move_to", {"target": 100 + turn})]

        return for_turn

    record_run(
        store,
        "a",
        turns=15,
        seed_set_id="ss",
        lifecycle_state="finished",
        actions_for=actions(12),
        yields_for=_science,
        cities=1,
    )
    record_run(
        store,
        "b",
        turns=15,
        seed_set_id="ss",
        lifecycle_state="finished",
        actions_for=actions(99),
        yields_for=_science,
        cities=1,
    )
    report = store.divergence("a", "b")
    assert report.same_seed_set is True
    assert report.compared_turns == tuple(range(1, 16))
    assert report.first_divergent_turn == 12
    assert [d.dimension for d in report.differed] == ["actions"]
    assert "units.found_city" in report.differed[0].value_a
    assert report.excluded == ()

    record_run(
        store,
        "c",
        turns=4,
        seed_set_id="ss",
        lifecycle_state="paused",
        actions_for=actions(99),
        skip_turns=[2],
    )
    gapped = store.divergence("a", "c")
    assert gapped.first_divergent_turn is None
    assert [e.reason for e in gapped.excluded] == [ExclusionReason.HAS_GAPS]


def test_us3_ac3_a_run_still_playing_is_in_progress_with_its_turns_so_far(
    store: SqliteMatchStore,
) -> None:
    """US3/AC3, narrowed by T298: a live run is trended *while its record is whole*.

    Turns 1-3 recorded, save points 1-3, nothing in flight: the record is currently complete,
    the run is non-terminal, so the series carries `in_progress=True` (FR-021, T3) and nothing
    is excluded. That is the window this criterion lives in, and it is a real one -- it is every
    moment between a turn's `TurnCycle` landing and the next turn's quicksave.
    """
    record_run(store, "live", turns=3, yields_for=_science)
    response = store.metric_series(TrendQuery(run_ids=("live",), metrics=("science",)))
    (series,) = response.series
    assert series.in_progress is True
    assert [p.turn for p in series.points] == [1, 2, 3]
    assert response.excluded == ()


def test_us3_ac3_a_run_mid_turn_is_excluded_until_that_turn_lands(
    store: SqliteMatchStore,
) -> None:
    """The other half of the same criterion, changed by T298 (contract amended, see
    contracts/match-tracking-store.md T1/T3).

    Add turn 4's quicksave and the run is mid-turn. Before T298 it derived `complete` and fed
    trending; now it derives `in_flight` and is refused under `record_in_flight`. The reason is
    not that turns 1-3 are wrong -- they are fine -- but that this shape is exactly the shape a
    run that *died* on turn 4's write leaves behind, and the record cannot tell the two apart.
    Principle III does not let the gate guess, so it refuses both and the run returns to the
    series the moment turn 4 lands (or reports `has_gaps` the moment it stops advancing).
    """
    record_run(store, "live", turns=3, yields_for=_science)
    store.write_save_point(make_save_point("live-sp4-0", "live", 4))  # mid-turn 4

    assert store.record_completeness("live") is RecordCompletenessStatus.IN_FLIGHT
    response = store.metric_series(TrendQuery(run_ids=("live",), metrics=("science",)))
    assert response.series == ()
    assert [e.reason for e in response.excluded] == [ExclusionReason.RECORD_IN_FLIGHT]

    # It is not disqualified for having gaps: it has none. The reason names the real fact.
    assert store.turn_gaps("live") == []
    assert response.excluded[0].gaps == ()


def test_t1_the_exclusion_rule_is_the_stores_and_the_override_is_recorded(
    store: SqliteMatchStore,
) -> None:
    record_run(
        store, "ok", turns=2, seed_set_id="ss", lifecycle_state="finished", yields_for=_science
    )
    record_run(
        store,
        "deg",
        turns=2,
        seed_set_id="ss",
        lifecycle_state="finished",
        yields_for=_science,
        comparability_status="visually_degraded",
    )
    record_run(
        store,
        "no",
        turns=2,
        seed_set_id="ss",
        lifecycle_state="finished",
        yields_for=_science,
        comparability_status="not_comparable",
    )

    strict = store.metric_series(TrendQuery(seed_set_id="ss", metrics=("science",)))
    assert [s.run_id for s in strict.series] == ["ok"]
    assert {e.run_id: e.reason for e in strict.excluded} == {
        "deg": ExclusionReason.VISUALLY_DEGRADED,
        "no": ExclusionReason.NOT_COMPARABLE,
    }
    assert strict.included_visually_degraded is False

    admitted = store.metric_series(
        TrendQuery(seed_set_id="ss", metrics=("science",), include_visually_degraded=True)
    )
    assert admitted.included_visually_degraded is True
    by_run = {s.run_id: s for s in admitted.series}
    assert set(by_run) == {"ok", "deg"}
    assert by_run["deg"].comparability_status is ComparabilityStatus.VISUALLY_DEGRADED
    assert [e.run_id for e in admitted.excluded] == ["no"]


def test_t1_a_run_whose_game_turn_never_advanced_is_not_eligible_for_trending(
    store: SqliteMatchStore,
) -> None:
    """R14, revised 2026-09-21 (gameplay block 7): a gap-free record is not automatically a
    trendable one.

    ``run-480aa573`` recorded five turn cycles all at game turn 35 -- every turn present, every
    step present, ``record_completeness`` ``complete`` -- because each end turn was dispatched
    and never confirmed. Trending that run would average a stall in with real play, which is
    what Constitution Principle III excludes a gapped record for. So the store excludes it too,
    under its own reason rather than dressed up as ``has_gaps``.

    Asserted on **the flag** here: the cycle that recorded ``game_turn_advanced=False`` is
    named, and nothing else about the run is wrong.
    """
    record_run(store, "ok", turns=3, seed_set_id="ss", lifecycle_state="finished")
    record_run(store, "stalled", turns=3, seed_set_id="ss", lifecycle_state="finished")
    store.mark_turn_superseded("stalled", 2, 0)
    store.write_turn_cycle(
        make_turn_cycle_record(
            "stalled", 2, 1, outcome="end_turn_unconfirmed", game_turn_advanced=False
        )
    )

    # The record itself has no gaps -- the two questions are genuinely independent.
    assert store.record_completeness("stalled") is RecordCompletenessStatus.COMPLETE

    assert store.trend_exclusion("ok") is None
    exclusion = store.trend_exclusion("stalled")
    assert exclusion is not None
    assert exclusion.reason is ExclusionReason.GAME_TURN_DID_NOT_ADVANCE
    assert exclusion.gaps == (2,)

    response = store.metric_series(TrendQuery(seed_set_id="ss"))
    assert [s.run_id for s in response.series] == ["ok"] or not response.series
    assert [e.reason for e in response.excluded] == [ExclusionReason.GAME_TURN_DID_NOT_ADVANCE]


def test_t1_the_same_game_turn_twice_is_caught_without_the_flag_or_a_migration(
    store: SqliteMatchStore,
) -> None:
    """The derived half of the rule, which is what covers the records already on disk.

    Block 7's five cycles predate ``TurnCycle.game_turn_advanced`` entirely: their JSON has no
    such key and the store answers ``None`` for it. What they *do* carry is the game's own turn
    number in each step's ``game.turn_state`` observation -- 35, five times over -- so the rule
    reads consecutive authoritative cycles' recorded game turns and finds the stall there. No
    column, no backfill, no rewrite of a historical record.

    The control matters as much as the finding: a run whose game turn advances every turn,
    written exactly the same way and equally without the flag, stays eligible.
    """
    record_run(store, "moving", turns=4, game_turn_for=lambda turn: 30 + turn)
    record_run(store, "block7", turns=5, game_turn_for=lambda _turn: 35)

    # Neither run recorded the flag at all -- this is the 002-era shape, unmigrated.
    for run_id in ("moving", "block7"):
        record = store.get_turn_cycle(run_id, 1)
        assert record is not None and record.turn_cycle.game_turn_advanced is None

    assert store.trend_exclusion("moving") is None
    exclusion = store.trend_exclusion("block7")
    assert exclusion is not None
    assert exclusion.reason is ExclusionReason.GAME_TURN_DID_NOT_ADVANCE
    # Turn 1 set the baseline; turns 2-5 each replayed game turn 35.
    assert exclusion.gaps == (2, 3, 4, 5)


def test_t1_trend_exclusion_names_a_run_the_store_does_not_hold(
    store: SqliteMatchStore,
) -> None:
    exclusion = store.trend_exclusion("never-recorded")
    assert exclusion is not None and exclusion.reason is ExclusionReason.NO_SUCH_RUN


def test_t2_a_metric_the_record_never_carries_is_unavailable_not_zero(
    store: SqliteMatchStore,
) -> None:
    record_run(store, "r", turns=2, lifecycle_state="finished", yields_for=_science)
    response = store.metric_series(TrendQuery(run_ids=("r",), metrics=("faith",)))
    (series,) = response.series
    assert series.points == () and series.unavailable_reason is not None
    assert "faith" in series.unavailable_reason


# ==========================================================================
# US5 -- archival and retention never touch the record (SC-009, FR-022)
# ==========================================================================


def _snapshot(store: SqliteMatchStore, run_id: str) -> dict[str, Any]:
    records = store.export_run(run_id)
    return {
        "turn_cycles": records.turn_cycles,
        "model_calls": records.model_calls,
        "captures": records.captures,
        "images": dict(records.images),
        "configuration": records.configuration,
        "events": records.run_events,
        "run": records.run,
        "save_points": records.save_points,
        "counts": dict(store.store_info().counts),
    }


def test_us5_ac1_sc009_archiving_changes_save_eligibility_and_nothing_else(
    store: SqliteMatchStore,
) -> None:
    record_run(store, "r", turns=3, lifecycle_state="finished", cost_usd=0.01, yields_for=_science)
    kept, blob = make_capture("c1", "r", 1, "r-t1-a0-step1", blob=b"img")
    store.write_capture(kept, blob)
    store.write_run_event(make_event("e1", "r", "save_taken", turn_number=1))
    before = _snapshot(store, "r")

    store.archive_run("r", by="op", at=datetime(2026, 9, 21, 14, tzinfo=UTC))
    after = _snapshot(store, "r")

    for kind in ("turn_cycles", "model_calls", "captures", "images", "configuration"):
        assert after[kind] == before[kind], kind
    assert after["run"] == before["run"].model_copy(
        update={"archived_at": after["run"].archived_at}
    )
    assert after["run"].archived_at is not None
    assert [e.event_type.value for e in after["events"]] == [
        *[e.event_type.value for e in before["events"]],
        "run_archived",
    ]
    assert all(s.retention_status.value == "retained" for s in before["save_points"])
    assert all(s.retention_status.value == "eligible" for s in after["save_points"])
    assert after["counts"] == {**before["counts"], "run_events": before["counts"]["run_events"] + 1}
    assert "r" in {r.run_id for r in store.list_runs()}
    assert [
        s.run_id
        for s in store.metric_series(TrendQuery(run_ids=("r",), metrics=("science",))).series
    ] == ["r"]


def test_us5_ac2_archival_of_a_non_terminal_run_is_refused_until_an_operator_ends_it(
    store: SqliteMatchStore,
) -> None:
    record_run(store, "p", turns=2, lifecycle_state="paused")
    with pytest.raises(StoreWriteError):
        store.archive_run("p", by="op", at=datetime(2026, 9, 21, tzinfo=UTC))
    assert store.get_run("p").archived_at is None
    store.update_run("p", lifecycle_state="failed", stop_resolution="operator_stop", ended_at=at(9))
    store.archive_run("p", by="op", at=datetime(2026, 9, 21, tzinfo=UTC))
    assert store.get_run("p").archived_at is not None
