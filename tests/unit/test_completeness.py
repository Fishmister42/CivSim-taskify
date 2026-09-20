"""Unit tests for record-completeness derivation (T145; FR-052, SC-003, SC-011, invariant I11).

Exercised against ``SqliteMatchStore`` (the reference ``MatchStore`` adapter) rather than a hand
-rolled fake, so ``turn_gaps``/``step_gaps``/``list_save_points`` are the real, already-conformance
-tested implementations -- ``store.completeness.record_completeness_status`` is a thin derivation
on top of them, not a second implementation of gap detection.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.decision import Decision
from civsim_harness.models.records import ModelCall, SavePoint
from civsim_harness.models.run import RecordCompletenessStatus, Run
from civsim_harness.models.turn import DecisionStep, Observation, TurnCycle
from civsim_harness.store.completeness import record_completeness_status
from civsim_harness.store.port import DecisionStepBundle, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

NOW = datetime(2026, 9, 20, tzinfo=UTC)

# --------------------------------------------------------------------------
# Fixtures / builders -- mirrors tests/contract/test_match_store_port.py's own
# minimal-valid-record builders (kept local/private to this file on purpose).
# --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteMatchStore]:
    adapter = SqliteMatchStore(tmp_path / "match.db")
    yield adapter
    adapter.close()


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


def _make_run(run_id: str, config_id: str = "cfg1", *, lifecycle_state: str = "playing") -> Run:
    return Run.model_validate(
        {
            "run_id": run_id,
            "config_id": config_id,
            "lifecycle_state": lifecycle_state,
            "record_completeness_status": "unknown",
            "comparability_status": "comparable",
            "observation_catalog_version": _catalog_ref(),
            "action_catalog_version": _catalog_ref(),
            "game_build": "win/1.0.12.9",
            "host_support_tier": "validated",
            "capture_path": "windows_graphics_capture",
        }
    )


def _make_save_point(run_id: str, turn_number: int) -> SavePoint:
    return SavePoint.model_validate(
        {
            "save_point_id": f"{run_id}-sp{turn_number}",
            "run_id": run_id,
            "turn_number": turn_number,
            "save_name": f"civsim__{run_id}__t{turn_number:04d}",
            "taken_at": NOW,
            "verified": True,
            "retention_status": "retained",
            "missing": False,
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


def _write_turn(
    store: SqliteMatchStore,
    run_id: str,
    turn_number: int,
    *,
    step_indices: list[int] | None = None,
) -> None:
    """Write both the FR-007 quicksave and the authoritative turn attempt for one turn -- the two
    records ``record_completeness_status`` reads (``list_save_points`` for the attempted-turn
    range, ``turn_gaps``/``step_gaps`` for the gap markers themselves)."""
    store.write_save_point(_make_save_point(run_id, turn_number))

    turn_cycle_id = f"{run_id}-t{turn_number}-a0"
    indices = step_indices if step_indices is not None else [1]
    bundles = [_make_step_bundle(run_id, turn_cycle_id, i) for i in indices]
    turn_cycle = TurnCycle.model_validate(
        {
            "turn_cycle_id": turn_cycle_id,
            "run_id": run_id,
            "turn_number": turn_number,
            "attempt_index": 0,
            "is_authoritative": True,
            "save_point_id": f"{run_id}-sp{turn_number}",
            "step_count": len(bundles),
            "outcome": "ended_by_agent",
            "final_no_progress_streak": 0,
            "visually_degraded": False,
            "started_at": NOW,
            "ended_at": NOW,
            "persisted_at": NOW,
        }
    )
    store.write_turn_cycle(TurnCycleRecord(turn_cycle=turn_cycle, steps=bundles))


# --------------------------------------------------------------------------
# T145 -- the three specified cases
# --------------------------------------------------------------------------


def test_missing_turn_number_forces_has_gaps(store: SqliteMatchStore) -> None:
    """A turn number with no authoritative attempt (turn 2, between two attempted turns) forces
    record_completeness_status = has_gaps."""
    store.create_run(_make_run("run-turn-gap"), _make_config("cfg-turn-gap"))
    _write_turn(store, "run-turn-gap", 1)
    # Turn 2 is skipped entirely -- no save point, no turn cycle.
    _write_turn(store, "run-turn-gap", 3)

    assert (
        record_completeness_status(store, "run-turn-gap")  # type: ignore[arg-type]
        is RecordCompletenessStatus.HAS_GAPS
    )


def test_missing_step_index_within_a_turn_forces_has_gaps(store: SqliteMatchStore) -> None:
    """A turn present with an authoritative attempt, but missing a step_index inside it (steps
    1, 2, 4 -- step 3 silently missing), forces has_gaps even though turn_gaps alone would call
    this turn present."""
    store.create_run(_make_run("run-step-gap"), _make_config("cfg-step-gap"))
    _write_turn(store, "run-step-gap", 1, step_indices=[1, 2, 4])

    assert (
        record_completeness_status(store, "run-step-gap")  # type: ignore[arg-type]
        is RecordCompletenessStatus.HAS_GAPS
    )


def test_contiguous_turns_and_steps_yield_complete(store: SqliteMatchStore) -> None:
    """A contiguous authoritative turn sequence with contiguous steps within each yields
    complete."""
    store.create_run(_make_run("run-complete"), _make_config("cfg-complete"))
    _write_turn(store, "run-complete", 1, step_indices=[1, 2, 3])
    _write_turn(store, "run-complete", 2, step_indices=[1])
    _write_turn(store, "run-complete", 3, step_indices=[1, 2])

    assert (
        record_completeness_status(store, "run-complete")  # type: ignore[arg-type]
        is RecordCompletenessStatus.COMPLETE
    )


# --------------------------------------------------------------------------
# Additional coverage: the unknown case, and gaps at both grains together
# --------------------------------------------------------------------------


def test_a_run_with_no_turn_attempts_yet_is_unknown(store: SqliteMatchStore) -> None:
    """No save points at all: nothing yet to judge complete or incomplete -- distinct from
    has_gaps, which requires the run to have actually attempted something."""
    store.create_run(_make_run("run-fresh"), _make_config("cfg-fresh"))

    assert (
        record_completeness_status(store, "run-fresh")  # type: ignore[arg-type]
        is RecordCompletenessStatus.UNKNOWN
    )


def test_a_single_complete_turn_yields_complete(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-one-turn"), _make_config("cfg-one-turn"))
    _write_turn(store, "run-one-turn", 1, step_indices=[1, 2, 3])

    assert (
        record_completeness_status(store, "run-one-turn")  # type: ignore[arg-type]
        is RecordCompletenessStatus.COMPLETE
    )


def test_step_gap_in_an_earlier_turn_is_not_masked_by_a_clean_later_turn(
    store: SqliteMatchStore,
) -> None:
    """A step gap in turn 1 must be caught even when every later turn (here, turn 2) is
    internally contiguous -- record_completeness_status must not stop checking after the first
    turn it happens to look at."""
    store.create_run(_make_run("run-early-gap"), _make_config("cfg-early-gap"))
    _write_turn(store, "run-early-gap", 1, step_indices=[1, 3])  # step 2 missing
    _write_turn(store, "run-early-gap", 2, step_indices=[1, 2])

    assert (
        record_completeness_status(store, "run-early-gap")  # type: ignore[arg-type]
        is RecordCompletenessStatus.HAS_GAPS
    )


def test_turn_level_gap_is_reported_even_when_present_turns_are_step_complete(
    store: SqliteMatchStore,
) -> None:
    """Turn-level and step-level gaps are independent signals (T158): a run whose present turns
    are all internally contiguous is still has_gaps if a whole turn number is missing."""
    store.create_run(_make_run("run-mixed"), _make_config("cfg-mixed"))
    _write_turn(store, "run-mixed", 1, step_indices=[1])
    # turn 2 missing entirely
    _write_turn(store, "run-mixed", 3, step_indices=[1, 2])

    assert (
        record_completeness_status(store, "run-mixed")  # type: ignore[arg-type]
        is RecordCompletenessStatus.HAS_GAPS
    )


# --------------------------------------------------------------------------
# Trailing attempted-but-unrecorded turn (T158 defect fix): the highest
# attempted turn (per list_save_points) has a quicksave and zero TurnCycle
# rows at all -- invisible to a "present turn_cycle numbers" range check
# alone, since there is no later recorded turn to expose it as a hole.
# --------------------------------------------------------------------------


def test_trailing_unrecorded_turn_on_a_terminal_run_forces_has_gaps(
    store: SqliteMatchStore,
) -> None:
    """A run that has reached a terminal lifecycle_state (here, failed) with a quicksave for
    its highest attempted turn but no TurnCycle for it at all -- the run crashed or exhausted
    its provider chain right after quicksaving and before ever persisting that turn -- must
    report has_gaps (Principle III, FR-052). Before this fix, turn_gaps derived its checked
    range solely from MAX(turn_number) in turn_cycles, so this trailing gap was invisible to
    it and record_completeness_status silently reported complete."""
    store.create_run(
        _make_run("run-trailing-failed", lifecycle_state="failed"),
        _make_config("cfg-trailing-failed"),
    )
    _write_turn(store, "run-trailing-failed", 1, step_indices=[1, 2])
    # Turn 2 only ever got its FR-007 quicksave -- no TurnCycle was ever written.
    store.write_save_point(_make_save_point("run-trailing-failed", 2))

    assert (
        record_completeness_status(store, "run-trailing-failed")  # type: ignore[arg-type]
        is RecordCompletenessStatus.HAS_GAPS
    )


def test_trailing_unrecorded_turn_on_a_paused_run_forces_has_gaps(
    store: SqliteMatchStore,
) -> None:
    """The same shape as the terminal case above, but on a `paused` run: not a terminal
    lifecycle_state, yet not `playing` either. FR-042 pauses a run rather than fabricating a
    turn on chain exhaustion, so treating "terminal" as the whole test would leave exactly
    this -- the realistic provider-chain-exhaustion shape -- silently complete. A `paused`
    run with a trailing attempted-but-unrecorded turn must report has_gaps just like a
    terminal one."""
    store.create_run(
        _make_run("run-trailing-paused", lifecycle_state="paused"),
        _make_config("cfg-trailing-paused"),
    )
    _write_turn(store, "run-trailing-paused", 1, step_indices=[1, 2])
    # Turn 2 only ever got its FR-007 quicksave -- the run paused (e.g. chain exhaustion)
    # before ever persisting a TurnCycle for it.
    store.write_save_point(_make_save_point("run-trailing-paused", 2))

    assert (
        record_completeness_status(store, "run-trailing-paused")  # type: ignore[arg-type]
        is RecordCompletenessStatus.HAS_GAPS
    )


def test_trailing_unrecorded_turn_on_a_still_playing_run_is_not_a_spurious_gap(
    store: SqliteMatchStore,
) -> None:
    """The exact same shape as the terminal case above -- a quicksave for the highest attempted
    turn and no TurnCycle for it -- must NOT be reported while the run is still playing: that
    turn is simply the one currently in progress, whose quicksave legitimately precedes its
    TurnCycle (FR-007). Getting this backwards would make every healthy running run report
    has_gaps."""
    store.create_run(
        _make_run("run-trailing-playing", lifecycle_state="playing"),
        _make_config("cfg-trailing-playing"),
    )
    _write_turn(store, "run-trailing-playing", 1, step_indices=[1, 2])
    # Turn 2 is in progress: its quicksave is taken, its TurnCycle is not written yet.
    store.write_save_point(_make_save_point("run-trailing-playing", 2))

    assert (
        record_completeness_status(store, "run-trailing-playing")  # type: ignore[arg-type]
        is RecordCompletenessStatus.COMPLETE
    )
