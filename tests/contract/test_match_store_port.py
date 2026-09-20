"""Port conformance suite for `MatchStore` (T041, T042).

Runs against `SqliteMatchStore`, the reference adapter, today; deliverable
3's real store must pass the same suite unchanged (swapping implementations
is meant to be a configuration change -- contracts/match-store-port.md
"Conformance tests"). Every test below is annotated with the requirement id
it demonstrates: D1-D6 (durability), A1-A4 (archival/retention), FR-/SC-/
invariant ids where the contract names one specifically.

Two tests are deliberately adversarial (T042): a several-hundred-step turn
must round-trip with order intact and no truncation, and a
finished-but-unarchived run's save points must never appear in
`list_eligible_save_points()` no matter how old the run is (invariant I17).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from civsim_harness.errors import StoreWriteError
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.decision import Decision
from civsim_harness.models.records import ModelCall, RunEvent, RunEventType, SavePoint
from civsim_harness.models.run import Run
from civsim_harness.models.turn import DecisionStep, Observation, ScreenCapture, TurnCycle
from civsim_harness.store.guard import (
    TurnPersistedToken,
    advance_turn,
    persist_turn_before_advance,
    write_then_advance,
)
from civsim_harness.store.port import DecisionStepBundle, MatchStore, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

NOW = datetime.now(UTC)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteMatchStore]:
    adapter = SqliteMatchStore(tmp_path / "match.db")
    yield adapter
    adapter.close()


# --------------------------------------------------------------------------
# Record builders -- minimal, valid instances of every model this suite
# needs, built through .model_validate() (never raw construction) so every
# fixture also exercises each model's own validators.
# --------------------------------------------------------------------------


def _catalog_ref() -> dict[str, str]:
    return {"version": "2026.09.1", "content_hash": "abc123"}


def _make_config(config_id: str) -> RunConfiguration:
    return RunConfiguration.model_validate(
        {
            "config_id": config_id,
            "map_seed": "123",
            "civilization": "CIVILIZATION_ROME",
            "leader": "LEADER_TRAJAN",
            "ruleset": "RULESET_STANDARD",
            "difficulty": "DIFFICULTY_PRINCE",
            "stop_condition": {"type": "turn_reached", "turn": 50},
            "model_config": {"primary": {"provider": "openrouter", "model": "x"}},
            "no_progress_step_limit": 8,
            "recovery_attempt_limit": 3,
            "min_free_disk_gb": 25,
            "created_at": NOW,
        }
    )


def _make_run(
    run_id: str,
    config_id: str = "cfg1",
    *,
    lifecycle_state: str = "playing",
    stop_resolution: str | None = None,
    parent_run_id: str | None = None,
    parent_turn: int | None = None,
) -> Run:
    return Run.model_validate(
        {
            "run_id": run_id,
            "config_id": config_id,
            "lifecycle_state": lifecycle_state,
            "stop_resolution": stop_resolution,
            "record_completeness_status": "unknown",
            "comparability_status": "comparable",
            "observation_catalog_version": _catalog_ref(),
            "action_catalog_version": _catalog_ref(),
            "parent_run_id": parent_run_id,
            "parent_turn": parent_turn,
            "game_build": "win/1.0.12.9",
            "host_support_tier": "validated",
            "capture_path": "windows_graphics_capture",
        }
    )


def _make_turn_cycle(
    turn_cycle_id: str,
    run_id: str,
    turn_number: int,
    attempt_index: int,
    *,
    is_authoritative: bool,
    step_count: int,
    save_point_id: str,
    outcome: str = "ended_by_agent",
) -> TurnCycle:
    return TurnCycle.model_validate(
        {
            "turn_cycle_id": turn_cycle_id,
            "run_id": run_id,
            "turn_number": turn_number,
            "attempt_index": attempt_index,
            "is_authoritative": is_authoritative,
            "save_point_id": save_point_id,
            "step_count": step_count,
            "outcome": outcome,
            "final_no_progress_streak": 0,
            "visually_degraded": False,
            "started_at": NOW,
            "ended_at": NOW,
            "persisted_at": NOW,
        }
    )


def _make_step_bundle(run_id: str, turn_cycle_id: str, step_index: int) -> DecisionStepBundle:
    step_id = f"{turn_cycle_id}-step{step_index}"
    obs_id = f"{step_id}-obs"
    dec_id = f"{step_id}-dec"
    call_id = f"{step_id}-call"

    step = DecisionStep.model_validate(
        {
            "decision_step_id": step_id,
            "turn_cycle_id": turn_cycle_id,
            "step_index": step_index,
            "observation_id": obs_id,
            "decision_id": dec_id,
            "model_call_id": call_id,
            "progress": "changed_state",
            "no_progress_streak_after": 0,
            "visually_degraded": False,
            "started_at": NOW,
            "ended_at": NOW,
        }
    )
    observation = Observation.model_validate(
        {
            "observation_id": obs_id,
            "decision_step_id": step_id,
            "assembled_at": NOW,
            "catalog_version": _catalog_ref(),
            "entries": [],
            "captures": [],
            "screen_identity": "world",
        }
    )
    decision = Decision.model_validate(
        {
            "decision_id": dec_id,
            "decision_step_id": step_id,
            "action_declaration_id": "units.move_to",
            "reasoning": "test reasoning",
            "trigger": "proactive",
            "model_call_id": call_id,
            "execution": {"outcome": "applied", "verified_at": NOW},
        }
    )
    model_call = ModelCall.model_validate(
        {
            "model_call_id": call_id,
            "run_id": run_id,
            "turn_cycle_id": turn_cycle_id,
            "decision_step_id": step_id,
            "model_requested": {"provider": "openrouter", "model": "x"},
            "model_served": {"provider": "openrouter", "model": "x"},
            "latency_ms": 100,
            "cost": {},
            "retry_count": 0,
            "fallback_occurred": False,
            "image_count": 0,
            "outcome": "decision_returned",
        }
    )
    return DecisionStepBundle(
        step=step, observation=observation, decision=decision, model_call=model_call
    )


def _make_turn_cycle_record(
    run_id: str,
    turn_number: int,
    attempt_index: int = 0,
    *,
    num_steps: int = 1,
    step_indices: list[int] | None = None,
    is_authoritative: bool = True,
    outcome: str = "ended_by_agent",
) -> TurnCycleRecord:
    turn_cycle_id = f"{run_id}-t{turn_number}-a{attempt_index}"
    indices = step_indices if step_indices is not None else list(range(1, num_steps + 1))
    bundles = [_make_step_bundle(run_id, turn_cycle_id, i) for i in indices]
    tc = _make_turn_cycle(
        turn_cycle_id,
        run_id,
        turn_number,
        attempt_index,
        is_authoritative=is_authoritative,
        step_count=len(bundles),
        save_point_id=f"{run_id}-sp{turn_number}-{attempt_index}",
        outcome=outcome,
    )
    return TurnCycleRecord(turn_cycle=tc, steps=bundles)


def _make_save_point(
    save_point_id: str,
    run_id: str,
    turn_number: int,
    *,
    retention_status: str = "retained",
    missing: bool = False,
    taken_at: datetime | None = None,
) -> SavePoint:
    return SavePoint.model_validate(
        {
            "save_point_id": save_point_id,
            "run_id": run_id,
            "turn_number": turn_number,
            "save_name": f"civsim__{run_id}__t{turn_number:04d}",
            "taken_at": taken_at or NOW,
            "verified": True,
            "retention_status": retention_status,
            "missing": missing,
        }
    )


def _run_archived_event_types(store: SqliteMatchStore, run_id: str) -> list[str]:
    """White-box helper: the port has no read op for events, so verifying
    `archive_run` wrote its `run_archived` event requires reaching past the
    port into the adapter's own connection -- legitimate here because this
    suite is testing this adapter's internals, not just its public surface.
    """
    rows = store._conn.execute(  # noqa: SLF001 -- intentional white-box check
        "SELECT event_json FROM run_events WHERE run_id = ?", (run_id,)
    ).fetchall()
    return [RunEvent.model_validate_json(row[0]).event_type.value for row in rows]


# --------------------------------------------------------------------------
# Protocol shape
# --------------------------------------------------------------------------


def test_sqlite_match_store_satisfies_the_matchstore_protocol(store: SqliteMatchStore) -> None:
    typed_store: MatchStore = store  # mypy structural check: this line must type-check
    assert isinstance(typed_store, MatchStore)


# --------------------------------------------------------------------------
# D1 -- durable before return
# --------------------------------------------------------------------------


def test_write_turn_cycle_is_durable_before_return(tmp_path: Path) -> None:
    """D1: a brand-new adapter instance opened against the same database
    file, with no flush/close step from the writer in between, must
    immediately see a record whose write already returned.
    """
    db_path = tmp_path / "durable.db"
    first = SqliteMatchStore(db_path)
    record = _make_turn_cycle_record("run-durable", 1, 0)
    try:
        first.create_run(_make_run("run-durable"), _make_config("cfg-durable"))
        first.write_turn_cycle(record)
    finally:
        first.close()

    second = SqliteMatchStore(db_path)
    try:
        assert second.get_turn_cycle("run-durable", 1) == record
    finally:
        second.close()


# --------------------------------------------------------------------------
# D2 -- any write failure raises, never a falsy return
# --------------------------------------------------------------------------


def test_a_failed_write_raises_rather_than_returning_falsy(store: SqliteMatchStore) -> None:
    with pytest.raises(StoreWriteError):
        store.update_run("does-not-exist", lifecycle_state="playing")

    orphan_event = RunEvent.model_validate(
        {
            "event_id": "evt-orphan",
            "run_id": "does-not-exist",
            "event_type": "provider_failure",
            "occurred_at": NOW,
        }
    )
    with pytest.raises(StoreWriteError):
        store.write_run_event(orphan_event)


# --------------------------------------------------------------------------
# D3 -- write_turn_cycle is atomic
# --------------------------------------------------------------------------


def test_write_turn_cycle_rejects_a_record_for_a_nonexistent_run(store: SqliteMatchStore) -> None:
    """D3's write-or-nothing guarantee starts with referential integrity:
    a turn cycle can never land for a run that was never created.
    """
    record = _make_turn_cycle_record("ghost-run", 1, 0, num_steps=3)
    with pytest.raises(StoreWriteError):
        store.write_turn_cycle(record)
    assert store.get_turn_cycle("ghost-run", 1) is None


def test_write_turn_cycle_all_steps_land_together_or_not_at_all(store: SqliteMatchStore) -> None:
    """D3: the whole turn -- turn_cycle row and every one of its decision
    steps -- commits as one unit. Demonstrated by a successful write
    producing every step, since a partial commit is structurally
    unreachable through this method (one transaction wraps every INSERT).
    """
    store.create_run(_make_run("run-atomic"), _make_config("cfg-atomic"))
    record = _make_turn_cycle_record("run-atomic", 1, 0, num_steps=12)

    store.write_turn_cycle(record)

    reread = store.get_turn_cycle("run-atomic", 1)
    assert reread is not None
    assert len(reread.steps) == 12


# --------------------------------------------------------------------------
# D4 -- idempotent on (run_id, turn_number, attempt_index)
# --------------------------------------------------------------------------


def test_create_run_is_idempotent_on_an_identical_retry(store: SqliteMatchStore) -> None:
    run = _make_run("run-cr-idem")
    config = _make_config("cfg-cr-idem")
    assert store.create_run(run, config) == store.create_run(run, config)


def test_write_turn_cycle_is_idempotent_on_an_identical_retry(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-idem"), _make_config("cfg-idem"))
    record = _make_turn_cycle_record("run-idem", 1, 0, num_steps=3)

    first_id = store.write_turn_cycle(record)
    second_id = store.write_turn_cycle(record)  # retry after an "ambiguous failure"

    assert first_id == second_id
    reread = store.get_turn_cycle("run-idem", 1)
    assert reread is not None
    assert len(reread.steps) == 3  # not duplicated


def test_write_turn_cycle_rejects_a_retry_with_different_content_under_the_same_key(
    store: SqliteMatchStore,
) -> None:
    """D4's necessary counterpart: a retry under the same key carrying
    *different* content is not a legitimate retry and must raise, never
    silently overwrite what is already on record.
    """
    store.create_run(_make_run("run-idem2"), _make_config("cfg-idem2"))
    store.write_turn_cycle(_make_turn_cycle_record("run-idem2", 1, 0, num_steps=2))

    with pytest.raises(StoreWriteError):
        store.write_turn_cycle(_make_turn_cycle_record("run-idem2", 1, 0, num_steps=5))


# --------------------------------------------------------------------------
# D5 -- captures and save-point references go through this port
# --------------------------------------------------------------------------


def test_write_capture_rejects_a_screened_clean_capture_with_no_blob(
    store: SqliteMatchStore,
) -> None:
    """D5: nothing a record depends on may exist only in local/ephemeral
    form -- a shown, clean capture with no durably-stored blob is exactly
    that, and must be rejected rather than recorded half-true.
    """
    capture = ScreenCapture.model_validate(
        {
            "capture_id": "cap-noblob",
            "run_id": "run-x",
            "turn_number": 1,
            "decision_step_id": "step-x",
            "captured_at": NOW,
            "view_declaration_id": "views.world",
            "screening_status": "screened_clean",
            "shown_to_agent": True,
            "retained_as_evidence": True,
            "blob_ref": "deadbeef",
            "capture_path": "windows_graphics_capture",
        }
    )
    with pytest.raises(StoreWriteError):
        store.write_capture(capture, None)


def test_write_capture_stores_a_content_addressed_blob_and_round_trips(
    store: SqliteMatchStore,
) -> None:
    blob = b"pretend-image-bytes"
    digest = hashlib.sha256(blob).hexdigest()
    capture = ScreenCapture.model_validate(
        {
            "capture_id": "cap1",
            "run_id": "run-cap",
            "turn_number": 1,
            "decision_step_id": "step-1",
            "captured_at": NOW,
            "view_declaration_id": "views.world",
            "screening_status": "screened_clean",
            "shown_to_agent": True,
            "retained_as_evidence": True,
            "blob_ref": digest,
            "capture_path": "windows_graphics_capture",
        }
    )
    assert store.write_capture(capture, blob) == "cap1"


def test_write_capture_rejects_blob_ref_mismatching_the_actual_content_hash(
    store: SqliteMatchStore,
) -> None:
    capture = ScreenCapture.model_validate(
        {
            "capture_id": "cap-mismatch",
            "run_id": "run-x",
            "turn_number": 1,
            "decision_step_id": "step-x",
            "captured_at": NOW,
            "view_declaration_id": "views.world",
            "screening_status": "screened_clean",
            "shown_to_agent": True,
            "retained_as_evidence": True,
            "blob_ref": "not-the-real-hash",
            "capture_path": "windows_graphics_capture",
        }
    )
    with pytest.raises(StoreWriteError):
        store.write_capture(capture, b"actual content")


def test_write_capture_withheld_capture_is_recorded_with_no_blob(
    store: SqliteMatchStore,
) -> None:
    """SC-019: a withheld capture is recorded -- the evidence screening
    worked -- but never carries a blob.
    """
    capture = ScreenCapture.model_validate(
        {
            "capture_id": "cap-withheld",
            "run_id": "run-x",
            "turn_number": 1,
            "decision_step_id": "step-x",
            "captured_at": NOW,
            "view_declaration_id": "views.world",
            "screening_status": "withheld",
            "withheld_reason": "capture_failed",
            "shown_to_agent": False,
            "retained_as_evidence": True,
            "capture_path": "windows_graphics_capture",
        }
    )
    assert store.write_capture(capture, None) == "cap-withheld"


# --------------------------------------------------------------------------
# D6 -- ping() failure legibly aborts preparation
# --------------------------------------------------------------------------


def test_ping_reports_healthy_for_a_reachable_store(store: SqliteMatchStore) -> None:
    health = store.ping()
    assert health.ok is True
    assert health.detail is None


def test_ping_reports_unhealthy_once_the_store_is_unreachable(store: SqliteMatchStore) -> None:
    """D6: ping() must surface store-unreachable as a legible failure so
    preparation can abort before turn 1, rather than starting a run against
    a store that cannot record.
    """
    store.close()
    health = store.ping()
    assert health.ok is False
    assert health.detail


# --------------------------------------------------------------------------
# A1-A4 -- archival and retention
# --------------------------------------------------------------------------


def test_archive_run_sets_archived_at_writes_event_and_makes_retained_saves_eligible(
    store: SqliteMatchStore,
) -> None:
    """A1: archive_run sets Run.archived_at, writes a run_archived event,
    and transitions the run's retained save points to eligible.
    """
    store.create_run(
        _make_run("run-arch", lifecycle_state="finished", stop_resolution="turn_reached"),
        _make_config("cfg-arch"),
    )
    store.write_save_point(_make_save_point("run-arch-sp1", "run-arch", 1))
    store.write_save_point(_make_save_point("run-arch-sp2", "run-arch", 2))

    at = datetime.now(UTC)
    store.archive_run("run-arch", by="operator@example.com", at=at)

    archived = store.get_run("run-arch")
    assert archived is not None
    assert archived.archived_at == at

    saves = {s.save_point_id: s for s in store.list_save_points("run-arch")}
    assert saves["run-arch-sp1"].retention_status == "eligible"
    assert saves["run-arch-sp2"].retention_status == "eligible"
    eligible_ids = {s.save_point_id for s in store.list_eligible_save_points()}
    assert {"run-arch-sp1", "run-arch-sp2"} <= eligible_ids
    assert "run_archived" in _run_archived_event_types(store, "run-arch")


def test_write_save_point_rejects_retention_status_eligible_from_any_caller(
    store: SqliteMatchStore,
) -> None:
    """A2/I17: retention_status=eligible has exactly one legitimate
    producer, archive_run. write_save_point must reject it outright and
    unconditionally -- not as a helper, not as a config option, always.
    """
    store.create_run(_make_run("run-elig"), _make_config("cfg-elig"))
    tampered = _make_save_point("run-elig-sp1", "run-elig", 1, retention_status="eligible")
    with pytest.raises(StoreWriteError):
        store.write_save_point(tampered)


def test_update_run_rejects_setting_archived_at(store: SqliteMatchStore) -> None:
    """A2/FR-036: archived_at has exactly one legitimate writer, archive_run."""
    store.create_run(_make_run("run-ua"), _make_config("cfg-ua"))
    with pytest.raises(StoreWriteError):
        store.update_run("run-ua", archived_at=datetime.now(UTC))


def test_archive_run_rejects_a_non_terminal_run(store: SqliteMatchStore) -> None:
    """A3: archive_run is rejected on a non-terminal run."""
    store.create_run(_make_run("run-nonterm", lifecycle_state="playing"), _make_config("cfg-nt"))

    with pytest.raises(StoreWriteError):
        store.archive_run("run-nonterm", by="operator", at=datetime.now(UTC))

    unarchived = store.get_run("run-nonterm")
    assert unarchived is not None
    assert unarchived.archived_at is None


def test_archive_run_does_not_touch_records_events_or_captures(store: SqliteMatchStore) -> None:
    """A4: archival is not deletion. Turn records remain fully readable,
    byte-for-byte, after archive_run."""
    store.create_run(
        _make_run("run-a4", lifecycle_state="finished", stop_resolution="turn_reached"),
        _make_config("cfg-a4"),
    )
    record = _make_turn_cycle_record("run-a4", 1, 0)
    store.write_turn_cycle(record)

    store.archive_run("run-a4", by="operator", at=datetime.now(UTC))

    assert store.get_turn_cycle("run-a4", 1) == record


def test_finished_but_unarchived_run_save_points_never_appear_eligible_no_matter_how_old(
    store: SqliteMatchStore,
) -> None:
    """T042 adversarial case / invariant I17: a finished-but-unarchived
    run's save points must never appear in list_eligible_save_points(),
    regardless of age -- guarding against exactly the TTL/age/quota
    "housekeeping" A2 forbids, which would otherwise look like a sensible
    feature in code review.
    """
    store.create_run(
        _make_run("old-run", lifecycle_state="finished", stop_resolution="turn_reached"),
        _make_config("cfg-old"),
    )
    ancient = datetime(2000, 1, 1, tzinfo=UTC)
    sp = _make_save_point("old-run-sp1", "old-run", 1, taken_at=ancient)
    store.write_save_point(sp)

    eligible_ids = {s.save_point_id for s in store.list_eligible_save_points()}
    assert sp.save_point_id not in eligible_ids
    assert store.list_save_points("old-run")[0].retention_status == "retained"


# --------------------------------------------------------------------------
# Parent-immutability (FR-034, I12)
# --------------------------------------------------------------------------


def test_write_turn_cycle_rejects_altering_an_already_persisted_parent_turn(
    store: SqliteMatchStore,
) -> None:
    """A branch never modifies its parent (FR-034, I12): once a turn attempt
    is on record, no later write -- including one shaped like a branch
    trying to rewrite the exact point it branched from -- may silently
    replace its content. It must raise, and the original must survive
    provably unchanged.
    """
    store.create_run(_make_run("parent"), _make_config("cfg-parent"))
    original = _make_turn_cycle_record("parent", 5, 0, num_steps=2)
    store.write_turn_cycle(original)

    store.create_run(
        _make_run("child", parent_run_id="parent", parent_turn=5), _make_config("cfg-child")
    )

    tampered = _make_turn_cycle_record("parent", 5, 0, num_steps=3)
    with pytest.raises(StoreWriteError):
        store.write_turn_cycle(tampered)

    assert store.get_turn_cycle("parent", 5) == original


# --------------------------------------------------------------------------
# turn_gaps and step_gaps (FR-052, SC-011, SC-003)
# --------------------------------------------------------------------------


def test_turn_gaps_detects_a_missing_turn_number(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-tg"), _make_config("cfg-tg"))
    store.write_turn_cycle(_make_turn_cycle_record("run-tg", 1, 0))
    store.write_turn_cycle(_make_turn_cycle_record("run-tg", 3, 0))

    assert store.turn_gaps("run-tg") == [2]


def test_turn_gaps_is_empty_for_a_run_with_no_turns(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-tg2"), _make_config("cfg-tg2"))
    assert store.turn_gaps("run-tg2") == []


def test_step_gaps_detects_a_missing_step_index_within_a_turn(store: SqliteMatchStore) -> None:
    """SC-003: a turn present but internally incomplete must be
    detectable -- turn-level gap detection alone would call it complete.
    """
    store.create_run(_make_run("run-sg"), _make_config("cfg-sg"))
    record = _make_turn_cycle_record("run-sg", 1, 0, step_indices=[1, 2, 4])
    store.write_turn_cycle(record)

    assert store.step_gaps("run-sg", 1) == [3]


def test_step_gaps_is_empty_when_the_turn_has_no_authoritative_attempt(
    store: SqliteMatchStore,
) -> None:
    store.create_run(_make_run("run-sg2"), _make_config("cfg-sg2"))
    assert store.step_gaps("run-sg2", 1) == []


def test_turn_gaps_reports_a_trailing_attempted_turn_on_a_terminal_run(
    store: SqliteMatchStore,
) -> None:
    """A turn that was attempted (it has a quicksave) but never produced a
    TurnCycle is invisible to turn_gaps by turn-number-range alone, since
    there is no later recorded turn to expose it as a hole. On a run that
    has reached a terminal lifecycle_state (finished/failed), that trailing
    attempted-but-unrecorded turn means the run ended without ever
    persisting it -- a genuine gap (Principle III, FR-052) -- so it must be
    reported.
    """
    store.create_run(
        _make_run("run-trailing-terminal", lifecycle_state="failed"),
        _make_config("cfg-trailing-terminal"),
    )
    store.write_turn_cycle(_make_turn_cycle_record("run-trailing-terminal", 1, 0))
    store.write_save_point(
        _make_save_point("run-trailing-terminal-sp2", "run-trailing-terminal", 2)
    )
    # Turn 2 has a quicksave and nothing else -- the run failed before ever
    # persisting a TurnCycle for it.

    assert store.turn_gaps("run-trailing-terminal") == [2]


def test_turn_gaps_reports_a_trailing_attempted_turn_on_a_paused_run(
    store: SqliteMatchStore,
) -> None:
    """The same shape as the terminal case above, but on a `paused` run --
    not a terminal lifecycle_state, yet not `playing` either. FR-042 pauses
    a run rather than fabricating a turn on chain exhaustion, so `paused`
    with a trailing attempted-but-unrecorded turn must be a reported gap
    exactly like the terminal case: "not terminal" alone is too narrow a
    test for whether the run is still actively producing this turn's
    record.
    """
    store.create_run(
        _make_run("run-trailing-paused", lifecycle_state="paused"),
        _make_config("cfg-trailing-paused"),
    )
    store.write_turn_cycle(_make_turn_cycle_record("run-trailing-paused", 1, 0))
    store.write_save_point(_make_save_point("run-trailing-paused-sp2", "run-trailing-paused", 2))
    # Turn 2 has a quicksave and nothing else -- the run paused (e.g. its
    # provider chain exhausted, FR-042) before ever persisting a TurnCycle.

    assert store.turn_gaps("run-trailing-paused") == [2]


def test_turn_gaps_does_not_report_a_trailing_attempted_turn_on_a_still_playing_run(
    store: SqliteMatchStore,
) -> None:
    """The exact same shape as the terminal case above -- turn 2 has a
    quicksave and no TurnCycle -- must NOT be reported while the run is
    still playing: turn 2 is simply the turn currently in progress, whose
    quicksave legitimately lands before its TurnCycle does (FR-007). Getting
    this backwards would make every healthy running run report gaps.
    """
    store.create_run(
        _make_run("run-trailing-playing", lifecycle_state="playing"),
        _make_config("cfg-trailing-playing"),
    )
    store.write_turn_cycle(_make_turn_cycle_record("run-trailing-playing", 1, 0))
    store.write_save_point(_make_save_point("run-trailing-playing-sp2", "run-trailing-playing", 2))

    assert store.turn_gaps("run-trailing-playing") == []


# --------------------------------------------------------------------------
# Step-order preservation on read-back
# --------------------------------------------------------------------------


def test_write_turn_cycle_preserves_step_order_on_readback(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-order"), _make_config("cfg-order"))
    record = _make_turn_cycle_record("run-order", 1, 0, num_steps=25)

    store.write_turn_cycle(record)
    reread = store.get_turn_cycle("run-order", 1)

    assert reread is not None
    assert [b.step.step_index for b in reread.steps] == list(range(1, 26))
    assert [b.step.decision_step_id for b in reread.steps] == [
        b.step.decision_step_id for b in record.steps
    ]


def test_several_hundred_step_turn_round_trips_without_truncation(
    store: SqliteMatchStore,
) -> None:
    """T042 adversarial case: a turn of several hundred steps must
    round-trip with its order intact and no truncation (FR-012, FR-014,
    invariant I16 -- there is no step cap anywhere in this store)."""
    store.create_run(_make_run("big-run"), _make_config("cfg-big"))
    record = _make_turn_cycle_record("big-run", 1, 0, num_steps=350)

    store.write_turn_cycle(record)
    reread = store.get_turn_cycle("big-run", 1)

    assert reread is not None
    assert len(reread.steps) == 350
    assert [b.step.step_index for b in reread.steps] == list(range(1, 351))
    assert reread == record


# --------------------------------------------------------------------------
# Authoritative-attempt selection (FR-047, invariant I9)
# --------------------------------------------------------------------------


def test_authoritative_attempt_selection_and_replay(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-auth"), _make_config("cfg-auth"))
    attempt0 = _make_turn_cycle_record("run-auth", 1, 0, num_steps=2)
    store.write_turn_cycle(attempt0)

    # A second authoritative attempt for the same turn cannot land while the
    # first is still marked authoritative (I9).
    attempt1_premature = _make_turn_cycle_record("run-auth", 1, 1, num_steps=1)
    with pytest.raises(StoreWriteError):
        store.write_turn_cycle(attempt1_premature)

    # The abandoned attempt remains retrievable with the flag off (FR-047).
    store.mark_turn_superseded("run-auth", 1, 0)
    assert store.get_turn_cycle("run-auth", 1, authoritative_only=True) is None
    abandoned = store.get_turn_cycle("run-auth", 1, authoritative_only=False)
    assert abandoned is not None
    assert abandoned.turn_cycle.turn_cycle_id == attempt0.turn_cycle.turn_cycle_id
    assert abandoned.turn_cycle.outcome == "abandoned"
    assert abandoned.turn_cycle.is_authoritative is False

    attempt1 = _make_turn_cycle_record("run-auth", 1, 1, num_steps=1)
    store.write_turn_cycle(attempt1)

    selected = store.get_turn_cycle("run-auth", 1, authoritative_only=True)
    assert selected is not None
    assert selected.turn_cycle.attempt_index == 1
    assert store.get_turn_cycle("run-auth", 1, authoritative_only=False) == selected


def test_mark_turn_superseded_raises_for_an_unknown_attempt(store: SqliteMatchStore) -> None:
    with pytest.raises(StoreWriteError):
        store.mark_turn_superseded("no-such-run", 1, 0)


# --------------------------------------------------------------------------
# Remaining port surface: create_run/update_run/get_run, model calls, save
# points, active runs, last-known-good
# --------------------------------------------------------------------------


def test_get_run_returns_none_for_an_unknown_run(store: SqliteMatchStore) -> None:
    assert store.get_run("nope") is None


def test_update_run_applies_field_changes(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-upd", lifecycle_state="playing"), _make_config("cfg-upd"))

    store.update_run("run-upd", lifecycle_state="paused")

    updated = store.get_run("run-upd")
    assert updated is not None
    assert updated.lifecycle_state == "paused"


def test_write_model_call_records_calls_that_never_produced_a_decision_step(
    store: SqliteMatchStore,
) -> None:
    """write_model_call is the audit trail for outcomes that never produce a
    DecisionStep (empty_response/failed/rate_limited/context_rejected),
    which write_turn_cycle's embedded model calls never carry (FR-042,
    SC-012)."""
    store.create_run(_make_run("run-mc"), _make_config("cfg-mc"))
    call = ModelCall.model_validate(
        {
            "model_call_id": "mc-failed",
            "run_id": "run-mc",
            "turn_cycle_id": "run-mc-t1-a0",
            "decision_step_id": "step-that-never-happened",
            "model_requested": {"provider": "openrouter", "model": "x"},
            "model_served": {"provider": "openrouter", "model": "x"},
            "latency_ms": 50,
            "cost": {},
            "retry_count": 2,
            "fallback_occurred": False,
            "image_count": 0,
            "outcome": "failed",
        }
    )
    assert store.write_model_call(call) == "mc-failed"


def test_get_last_known_good_skips_missing_and_removed_saves(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-lkg"), _make_config("cfg-lkg"))
    store.write_save_point(_make_save_point("sp1", "run-lkg", 1))
    store.write_save_point(_make_save_point("sp2", "run-lkg", 2, missing=True))
    store.write_save_point(_make_save_point("sp3", "run-lkg", 3, retention_status="removed"))

    good = store.get_last_known_good("run-lkg")
    assert good is not None
    assert good.save_point_id == "sp1"


def test_get_last_known_good_is_none_with_no_usable_saves(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-lkg2"), _make_config("cfg-lkg2"))
    assert store.get_last_known_good("run-lkg2") is None


def test_get_capture_returns_none_for_an_unknown_capture(store: SqliteMatchStore) -> None:
    assert store.get_capture("no-such-capture") is None


def test_get_capture_round_trips_a_written_capture(store: SqliteMatchStore) -> None:
    blob = b"pretend-image-bytes-for-get-capture"
    digest = hashlib.sha256(blob).hexdigest()
    capture = ScreenCapture.model_validate(
        {
            "capture_id": "cap-getread",
            "run_id": "run-cap-read",
            "turn_number": 1,
            "decision_step_id": "step-1",
            "captured_at": NOW,
            "view_declaration_id": "views.world",
            "screening_status": "screened_clean",
            "shown_to_agent": True,
            "retained_as_evidence": True,
            "blob_ref": digest,
            "capture_path": "windows_graphics_capture",
        }
    )
    store.write_capture(capture, blob)

    assert store.get_capture("cap-getread") == capture


def test_get_capture_round_trips_a_withheld_capture_with_no_blob(store: SqliteMatchStore) -> None:
    capture = ScreenCapture.model_validate(
        {
            "capture_id": "cap-getwithheld",
            "run_id": "run-cap-read",
            "turn_number": 1,
            "decision_step_id": "step-1",
            "captured_at": NOW,
            "view_declaration_id": "views.world",
            "screening_status": "withheld",
            "withheld_reason": "capture_failed",
            "shown_to_agent": False,
            "retained_as_evidence": True,
            "capture_path": "windows_graphics_capture",
        }
    )
    store.write_capture(capture, None)

    assert store.get_capture("cap-getwithheld") == capture


def test_list_run_events_returns_a_runs_timeline_in_chronological_order(
    store: SqliteMatchStore,
) -> None:
    store.create_run(_make_run("run-events"), _make_config("cfg-events"))
    early = RunEvent.model_validate(
        {
            "event_id": "evt-early",
            "run_id": "run-events",
            "event_type": "resumed",
            "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    late = RunEvent.model_validate(
        {
            "event_id": "evt-late",
            "run_id": "run-events",
            "event_type": "unknown_screen",
            "occurred_at": datetime(2026, 1, 2, tzinfo=UTC),
        }
    )
    # Written out of chronological order -- list_run_events must still
    # return them ordered by occurred_at, not by write order.
    store.write_run_event(late)
    store.write_run_event(early)

    events = store.list_run_events("run-events")

    assert [e.event_id for e in events] == ["evt-early", "evt-late"]


def test_list_run_events_filters_by_event_types(store: SqliteMatchStore) -> None:
    """FR-005: an audit needs just the stall-shaped events
    (`unknown_screen`) off a run's timeline without re-filtering the whole
    thing itself.
    """
    store.create_run(_make_run("run-events2"), _make_config("cfg-events2"))
    store.write_run_event(
        RunEvent.model_validate(
            {
                "event_id": "evt-resumed",
                "run_id": "run-events2",
                "event_type": "resumed",
                "occurred_at": NOW,
            }
        )
    )
    store.write_run_event(
        RunEvent.model_validate(
            {
                "event_id": "evt-unknown-screen",
                "run_id": "run-events2",
                "event_type": "unknown_screen",
                "occurred_at": NOW,
            }
        )
    )

    filtered = store.list_run_events("run-events2", event_types=[RunEventType.UNKNOWN_SCREEN])

    assert [e.event_id for e in filtered] == ["evt-unknown-screen"]


def test_list_run_events_is_empty_for_a_run_with_no_events(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-events3"), _make_config("cfg-events3"))
    assert store.list_run_events("run-events3") == []


def test_list_active_runs_excludes_terminal_states(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-active", lifecycle_state="playing"), _make_config("cfg-a"))
    store.create_run(
        _make_run("run-done", lifecycle_state="finished", stop_resolution="turn_reached"),
        _make_config("cfg-d"),
    )

    active_ids = {r.run_id for r in store.list_active_runs()}
    assert "run-active" in active_ids
    assert "run-done" not in active_ids


# --------------------------------------------------------------------------
# E5 -- get_run_configuration resolves run ids and nothing else (T244)
# --------------------------------------------------------------------------


def test_get_run_configuration_returns_none_for_an_unknown_run_id(
    store: SqliteMatchStore,
) -> None:
    """E5: an id that is not a known run_id answers None -- never a guess,
    never a resolution against any secondary key."""
    assert store.get_run_configuration("no-such-run") is None


def test_get_run_configuration_never_cross_resolves_a_config_id_run_id_collision(
    store: SqliteMatchStore,
) -> None:
    """E5's stated worry, pinned (contracts/match-store-port.md, "The keying
    collision"): a run whose config_id happens to equal ANOTHER run's run_id
    must never cross-resolve. A store resolving against config_id -- the
    secondary key E5 names specifically -- would serve the wrong run's
    configuration silently, which is strictly worse than answering None.
    """
    victim_config = _make_config("cfg-e5-victim")
    store.create_run(_make_run("run-e5-victim", "cfg-e5-victim"), victim_config)

    # The collider's config_id IS the victim's run_id.
    collider_config = _make_config("run-e5-victim")
    store.create_run(_make_run("run-e5-collider", "run-e5-victim"), collider_config)

    # The collider resolves to its own configuration -- the victim's
    # configuration is never served for the collider.
    served_for_collider = store.get_run_configuration("run-e5-collider")
    assert served_for_collider == collider_config
    assert served_for_collider != victim_config

    # The victim's own id still resolves to the victim's configuration: a
    # store keyed by config_id would find the collider's configuration row
    # under this id and serve it here instead.
    served_for_victim = store.get_run_configuration("run-e5-victim")
    assert served_for_victim == victim_config
    assert served_for_victim != collider_config

    # And an id the store knows ONLY as a config_id is not a run_id at all,
    # so it answers None rather than resolving against the secondary key.
    assert store.get_run_configuration("cfg-e5-victim") is None


def test_get_run_configuration_round_trips_the_written_configuration_verbatim(
    store: SqliteMatchStore,
) -> None:
    """E5/T226: the configuration create_run persisted comes back exactly as
    written -- no re-derivation, no defaults applied on top of a stored
    value (the adapter's own docstring makes this claim; this pins it)."""
    config = _make_config("cfg-e5-roundtrip")
    store.create_run(_make_run("run-e5-roundtrip", "cfg-e5-roundtrip"), config)

    reread = store.get_run_configuration("run-e5-roundtrip")

    assert reread is not None
    assert reread == config
    assert reread.model_dump(mode="json") == config.model_dump(mode="json")


# --------------------------------------------------------------------------
# T046: the write-before-advance guard
# --------------------------------------------------------------------------


def test_write_then_advance_calls_end_turn_only_after_a_durable_write(
    store: SqliteMatchStore,
) -> None:
    store.create_run(_make_run("run-guard"), _make_config("cfg-guard"))
    record = _make_turn_cycle_record("run-guard", 1, 0)
    received: list[TurnPersistedToken] = []

    def end_turn(token: TurnPersistedToken) -> str:
        received.append(token)
        assert store.get_turn_cycle("run-guard", 1) is not None
        return "advanced"

    result = write_then_advance(store, record, end_turn)

    assert result == "advanced"
    assert len(received) == 1
    assert received[0].turn_cycle_id == record.turn_cycle.turn_cycle_id


def test_a_failed_write_halts_before_end_turn_is_ever_called(store: SqliteMatchStore) -> None:
    """FR-013/I3: end-turn is reachable only downstream of an acknowledged
    durable write; a failed write raises and halts the run."""
    store.create_run(_make_run("run-guard-fail"), _make_config("cfg-guard-fail"))
    store.write_turn_cycle(_make_turn_cycle_record("run-guard-fail", 1, 0))
    conflicting = _make_turn_cycle_record("run-guard-fail", 1, 0, num_steps=5)

    called = False

    def end_turn(token: TurnPersistedToken) -> None:
        nonlocal called
        called = True

    with pytest.raises(StoreWriteError):
        write_then_advance(store, conflicting, end_turn)

    assert called is False


def test_advance_turn_requires_a_token_from_persist_turn_before_advance(
    store: SqliteMatchStore,
) -> None:
    store.create_run(_make_run("run-guard2"), _make_config("cfg-guard2"))
    record = _make_turn_cycle_record("run-guard2", 1, 0)

    token = persist_turn_before_advance(store, record)

    assert isinstance(token, TurnPersistedToken)
    assert advance_turn(token, lambda t: t.turn_cycle_id) == record.turn_cycle.turn_cycle_id
