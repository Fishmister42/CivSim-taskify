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
from civsim_harness.store.completeness import (
    CycleGameTurn,
    game_turn_of,
    record_completeness_status,
    refresh_run_completeness,
    turns_whose_game_turn_did_not_advance,
)
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


def _make_run(
    run_id: str,
    config_id: str = "cfg1",
    *,
    lifecycle_state: str = "playing",
    parent_run_id: str | None = None,
    parent_turn: int | None = None,
) -> Run:
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
            "parent_run_id": parent_run_id,
            "parent_turn": parent_turn,
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
            "execution": {
                "outcome": "applied",
                # FR-011: an applied execution must carry the predicate verdict that
                # confirmed it; an empty verification means nothing re-read the board.
                "verification": {"declaration_id": "units.move_to", "result": True},
                "verified_at": NOW,
            },
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
    turn and no TurnCycle for it -- must NOT be reported as a *gap* while the run is still
    playing: that turn is simply the one currently in progress, whose quicksave legitimately
    precedes its TurnCycle (FR-007). Getting this backwards would make every healthy running run
    report has_gaps.

    It must not be reported `complete` either (T298). The carve-out is right, but it holds only
    while the run really is still cycling, and no halt path is obliged to say so -- a run that
    died on a write failure keeps the `playing` it last wrote, so `complete` here would be the
    record of a lost turn describing itself as whole. `in_flight` is the honest third answer:
    not a gap, not whole, and not judgeable until this run stops advancing."""
    store.create_run(
        _make_run("run-trailing-playing", lifecycle_state="playing"),
        _make_config("cfg-trailing-playing"),
    )
    _write_turn(store, "run-trailing-playing", 1, step_indices=[1, 2])
    # Turn 2 is in progress: its quicksave is taken, its TurnCycle is not written yet.
    store.write_save_point(_make_save_point("run-trailing-playing", 2))

    assert (
        record_completeness_status(store, "run-trailing-playing")  # type: ignore[arg-type]
        is RecordCompletenessStatus.IN_FLIGHT
    )
    assert store.turn_gaps("run-trailing-playing") == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# T239: a branch owes its record from its branch point, not from turn 1 --
# and refresh_run_completeness persists the derivation onto the stored Run.
# --------------------------------------------------------------------------


def test_a_branch_with_a_contiguous_record_from_its_branch_point_is_complete(
    store: SqliteMatchStore,
) -> None:
    """A branch from parent turn 5 replays turn 5 and plays on; turns 1-4 live in the *parent's*
    record, reachable through the recorded lineage (FR-033, Principle IV). An unfloored
    derivation would call every such branch has_gaps forever on a perfect record -- the false
    disqualification T239 exists to prevent in the field Deliverable 1's trend gate consumes."""
    store.create_run(
        _make_run("branch-clean", parent_run_id="some-parent", parent_turn=5),
        _make_config("cfg-branch-clean"),
    )
    _write_turn(store, "branch-clean", 5, step_indices=[1, 2])
    _write_turn(store, "branch-clean", 6, step_indices=[1])

    assert (
        record_completeness_status(store, "branch-clean")  # type: ignore[arg-type]
        is RecordCompletenessStatus.COMPLETE
    )


def test_a_branch_with_a_gap_at_or_above_its_branch_point_is_still_has_gaps(
    store: SqliteMatchStore,
) -> None:
    """The floor is a floor, not a pass: a genuine gap in the branch's *own* record (turn 6,
    between its replayed turn 5 and its turn 7) still forces has_gaps."""
    store.create_run(
        _make_run("branch-gap", parent_run_id="some-parent", parent_turn=5),
        _make_config("cfg-branch-gap"),
    )
    _write_turn(store, "branch-gap", 5)
    # Turn 6 skipped entirely; turn 7 recorded.
    _write_turn(store, "branch-gap", 7)

    assert (
        record_completeness_status(store, "branch-gap")  # type: ignore[arg-type]
        is RecordCompletenessStatus.HAS_GAPS
    )


def test_a_branch_missing_its_own_branch_point_turn_is_has_gaps(
    store: SqliteMatchStore,
) -> None:
    """The branch replays its parent_turn itself (run/runner.py's _begin) -- a branch whose own
    record starts *after* its branch point is missing the very turn it exists to replay."""
    store.create_run(
        _make_run("branch-floor-gap", parent_run_id="some-parent", parent_turn=5),
        _make_config("cfg-branch-floor-gap"),
    )
    # Only turn 6 on record -- turn 5, the branch point itself, was never recorded.
    _write_turn(store, "branch-floor-gap", 6)

    assert (
        record_completeness_status(store, "branch-floor-gap")  # type: ignore[arg-type]
        is RecordCompletenessStatus.HAS_GAPS
    )


def test_refresh_run_completeness_persists_the_derivation_onto_the_stored_run(
    store: SqliteMatchStore,
) -> None:
    """T239's derive-and-persist half, asserted on the far side: what a fresh get_run returns
    after the refresh, not what the helper handed back."""
    store.create_run(_make_run("run-refresh"), _make_config("cfg-refresh"))
    persisted = store.get_run("run-refresh")  # type: ignore[arg-type]
    assert persisted is not None
    assert persisted.record_completeness_status is RecordCompletenessStatus.UNKNOWN

    _write_turn(store, "run-refresh", 1, step_indices=[1, 2])
    refresh_run_completeness(store, "run-refresh")  # type: ignore[arg-type]

    persisted = store.get_run("run-refresh")  # type: ignore[arg-type]
    assert persisted is not None
    assert persisted.record_completeness_status is RecordCompletenessStatus.COMPLETE


def test_refresh_run_completeness_is_a_no_op_for_a_run_the_store_does_not_know(
    store: SqliteMatchStore,
) -> None:
    """A hand-built PreparedRun that was never persisted has no row to update -- the helper
    reports the derivation (UNKNOWN) and writes nothing rather than raising."""
    assert (
        refresh_run_completeness(store, "never-persisted")  # type: ignore[arg-type]
        is RecordCompletenessStatus.UNKNOWN
    )
    assert store.get_run("never-persisted") is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The game-turn rule (R14, revised 2026-09-21 after gameplay block 7): pure,
# and asserted on its own so the two signals it reads cannot silently merge.
# --------------------------------------------------------------------------


def _cycle(
    turn: int, *, advanced: bool | None = None, game_turn: int | None = None
) -> CycleGameTurn:
    return CycleGameTurn(turn_number=turn, game_turn_advanced=advanced, game_turn=game_turn)


def _observation(*, entries: list[dict[str, object]]) -> Observation:
    return Observation.model_validate(
        {
            "observation_id": "obs-gt",
            "decision_step_id": "step-gt",
            "assembled_at": NOW,
            "catalog_version": _catalog_ref(),
            "entries": entries,
            "captures": [],
            "screen_identity": "world",
        }
    )


def test_the_flag_alone_names_the_turn_whose_game_turn_did_not_advance() -> None:
    cycles = [
        _cycle(1, advanced=True, game_turn=35),
        _cycle(2, advanced=False, game_turn=35),
        _cycle(3, advanced=True, game_turn=36),
    ]
    assert turns_whose_game_turn_did_not_advance(cycles) == (2,)


def test_consecutive_cycles_at_the_same_game_turn_are_caught_without_any_flag() -> None:
    """Block 7's own shape, and the reason the rule reads the recorded numbers as well: those
    five cycles predate the flag entirely, so ``game_turn_advanced`` is ``None`` on every one of
    them and only the repeated game turn 35 gives them away. No migration is involved."""
    block7 = [_cycle(turn, game_turn=35) for turn in range(1, 6)]
    assert turns_whose_game_turn_did_not_advance(block7) == (2, 3, 4, 5)


def test_a_run_whose_game_turn_advances_every_turn_is_never_flagged() -> None:
    """The false-positive floor: a rule that fires on ordinary play would disqualify every run."""
    healthy = [_cycle(turn, game_turn=30 + turn) for turn in range(1, 8)]
    assert turns_whose_game_turn_did_not_advance(healthy) == ()
    with_flags = [_cycle(turn, advanced=True, game_turn=30 + turn) for turn in range(1, 8)]
    assert turns_whose_game_turn_did_not_advance(with_flags) == ()


def test_an_unrecorded_game_turn_is_never_read_as_agreement() -> None:
    """``None`` means *not recorded*, in both signals. A cycle carrying neither contributes
    nothing -- and the last *known* game turn stays the baseline across it, because a run's
    authoritative game turns are monotonic: seeing 35 again after an unrecorded attempt still
    means the game did not move."""
    assert turns_whose_game_turn_did_not_advance([_cycle(1), _cycle(2), _cycle(3)]) == ()
    across_a_hole = [_cycle(1, game_turn=35), _cycle(2), _cycle(3, game_turn=35)]
    assert turns_whose_game_turn_did_not_advance(across_a_hole) == (3,)


def test_game_turn_of_reads_the_declared_entry_and_nothing_else() -> None:
    assert game_turn_of(None) is None
    assert game_turn_of(_observation(entries=[])) is None
    # A different declaration carrying a turn_number is not the game's turn counter.
    assert (
        game_turn_of(
            _observation(
                entries=[
                    {
                        "declaration_id": "units.state",
                        "key": "units",
                        "value": {"turn_number": 35},
                        "context": "InGame",
                    }
                ]
            )
        )
        is None
    )
    assert (
        game_turn_of(
            _observation(
                entries=[
                    {
                        "declaration_id": "game.turn_state",
                        "key": "turn_state",
                        "value": {
                            "turn_number": 35,
                            "is_local_player_turn": True,
                            "is_waiting_for_other_players": False,
                        },
                        "context": "InGame",
                    }
                ]
            )
        )
        == 35
    )
