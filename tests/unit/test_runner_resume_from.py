"""T215 -- ``Runner.resume_from``: rewinding a run to a recorded turn-start save (FR-004, FR-036).

``civsim run resume-from`` reaches this method for the first time now that the composition root
exists; before T209 it failed earlier, at "no runner is configured". These tests pin the three
things the method is responsible for:

1. **What it refuses, before touching anything.** An unknown run, a terminal run, a run still
   actively playing (a turn is never cut short to honour an operator command -- FR-004, FR-008,
   SC-022), a turn number that is not a turn, and a save that is missing or already removed
   (FR-036: reported, never retargeted to a nearby turn).
2. **Lineage (constitution Principle IV).** A rewind abandons every recorded attempt at the
   resumed turn and after it, and the replay will overwrite those turns' quicksaves -- so which
   lineage it resumed from is recorded, both on the ``turn_abandoned`` event written before the
   load (so even a rewind that never completes leaves an auditable claim) and on the ``resumed``
   event written after it, naming every ``(turn_number, attempt_index)`` superseded.
3. **That a failed rewind is non-destructive.** A live load can genuinely fail -- the production
   ``LuaSaveLoader`` (T217, ``saves/load_game.py``) refuses a load the client rejects or that
   lands on the wrong position -- so a failed ``resume-from`` must fail *specifically* -- naming
   the loader's own reason -- and must leave the run's authoritative turn record exactly as it
   was.

Nothing here re-implements recovery: the rewind is delegated to ``resilience.recovery.
RecoveryEngine``, reached through the seam that already carries it
(``RunnerDependencies.build_turn_dependencies(...).recovery``), so these tests drive a scripted
``SaveLoader`` through that same seam rather than through a second load path of the runner's own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.errors import HarnessError
from civsim_harness.models.config import RunConfiguration, TurnReachedStopCondition
from civsim_harness.models.decision import Decision
from civsim_harness.models.records import ModelCall, RetentionStatus, RunEventType, SavePoint
from civsim_harness.models.run import LifecycleState, Run
from civsim_harness.models.turn import DecisionStep, Observation, TurnCycle
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import DecisionLoopContext
from civsim_harness.run.runner import (
    PreparedRun,
    ResumeFromFailed,
    ResumeFromRefused,
    Runner,
    RunnerDependencies,
    _RunState,
)
from civsim_harness.run.stop import StopEvaluation
from civsim_harness.run.turn_cycle import TurnCycleDependencies
from civsim_harness.saves.addressing import SaveAddressingError
from civsim_harness.store.port import DecisionStepBundle, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from fakes.fake_host import FakeHostPlatform

NOW = datetime(2026, 9, 20, tzinfo=UTC)
RUN_ID = "run-resume-from-t215"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteMatchStore]:
    adapter = SqliteMatchStore(tmp_path / "match.db")
    yield adapter
    adapter.close()


# --------------------------------------------------------------------------
# Scripted save loaders (the `resilience.recovery.SaveLoader` seam)
# --------------------------------------------------------------------------


class _RecordingSaveLoader:
    """A ``SaveLoader`` that succeeds and remembers what it was asked to load -- there is no real
    game client here, and the point of the successful-rewind tests is what the *record* says."""

    def __init__(self) -> None:
        self.loaded: list[SavePoint] = []

    async def load(self, save: SavePoint) -> None:
        self.loaded.append(save)


class _NoLoadPathLoader:
    """A ``SaveLoader`` whose every call raises, by name -- the scripted stand-in for any load
    the production ``LuaSaveLoader`` (T217) itself refuses (a client rejection, a wrong
    far-side position). The message is this test file's own fixture text, asserted verbatim
    below to prove the loader's *own* reason survives to the operator unflattened."""

    async def load(self, save: SavePoint) -> None:
        raise HarnessError(
            "no verified live save-load path exists in this codebase yet",
            detail={"save_point_id": save.save_point_id},
        )


# --------------------------------------------------------------------------
# Record builders (same minimal-valid-instance style as tests/integration/test_branching.py)
# --------------------------------------------------------------------------


def _catalog_ref() -> dict[str, str]:
    return {"version": "2026.09.1", "content_hash": "abc123"}


def _make_config(config_id: str) -> RunConfiguration:
    return RunConfiguration.model_validate(
        {
            "config_id": config_id,
            "map_seed": "1849275663",
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


def _make_run(run_id: str, *, lifecycle_state: str = "paused") -> Run:
    return Run.model_validate(
        {
            "run_id": run_id,
            "config_id": f"cfg-{run_id}",
            "lifecycle_state": lifecycle_state,
            "record_completeness_status": "complete",
            "comparability_status": "comparable",
            "observation_catalog_version": _catalog_ref(),
            "action_catalog_version": _catalog_ref(),
            "game_build": "win/1.0.12.9",
            "host_support_tier": "validated",
            "capture_path": "none",
        }
    )


def _make_step_bundle(run_id: str, turn_cycle_id: str, step_index: int) -> DecisionStepBundle:
    step_id = f"{turn_cycle_id}-step{step_index}"
    obs_id, dec_id, call_id = f"{step_id}-obs", f"{step_id}-dec", f"{step_id}-call"
    return DecisionStepBundle(
        step=DecisionStep.model_validate(
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
        ),
        observation=Observation.model_validate(
            {
                "observation_id": obs_id,
                "decision_step_id": step_id,
                "assembled_at": NOW,
                "catalog_version": _catalog_ref(),
                "entries": [],
                "captures": [],
                "screen_identity": "world",
            }
        ),
        decision=Decision.model_validate(
            {
                "decision_id": dec_id,
                "decision_step_id": step_id,
                "action_declaration_id": "units.move_to",
                "reasoning": "test reasoning",
                "trigger": "proactive",
                "model_call_id": call_id,
                "execution": {"outcome": "applied", "verified_at": NOW},
            }
        ),
        model_call=ModelCall.model_validate(
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
        ),
    )


def _make_save_point(run_id: str, turn: int, **overrides: Any) -> SavePoint:
    return SavePoint.model_validate(
        {
            "save_point_id": f"{run_id}-sp{turn}",
            "run_id": run_id,
            "turn_number": turn,
            "save_name": f"civsim__{run_id}__t{turn:04d}",
            "taken_at": NOW,
            "verified": True,
            "retention_status": "retained",
            **overrides,
        }
    )


def _make_turn_cycle_record(run_id: str, turn: int) -> TurnCycleRecord:
    turn_cycle_id = f"{run_id}-t{turn}-a0"
    return TurnCycleRecord(
        turn_cycle=TurnCycle.model_validate(
            {
                "turn_cycle_id": turn_cycle_id,
                "run_id": run_id,
                "turn_number": turn,
                "attempt_index": 0,
                "is_authoritative": True,
                "save_point_id": f"{run_id}-sp{turn}",
                "step_count": 1,
                "outcome": "ended_by_agent",
                "final_no_progress_streak": 0,
                "visually_degraded": False,
                "started_at": NOW,
                "ended_at": NOW,
                "persisted_at": NOW,
            }
        ),
        steps=[_make_step_bundle(run_id, turn_cycle_id, 1)],
    )


def _seed_run(
    store: SqliteMatchStore, *, through_turn: int, lifecycle_state: str = "paused"
) -> PreparedRun:
    """A run that already played turns 1..*through_turn*, each with its FR-007 quicksave and one
    authoritative ``TurnCycle`` -- the record a rewind has to act on."""
    run = _make_run(RUN_ID, lifecycle_state=lifecycle_state)
    store.create_run(run, _make_config(f"cfg-{RUN_ID}"))
    for turn in range(1, through_turn + 1):
        store.write_save_point(_make_save_point(RUN_ID, turn))
        store.write_turn_cycle(_make_turn_cycle_record(RUN_ID, turn))
    return PreparedRun(run=run, stop_condition=TurnReachedStopCondition(turn=50))


# --------------------------------------------------------------------------
# Runner assembly
# --------------------------------------------------------------------------


def _evaluate_stop_facts(_prepared: PreparedRun, turn_number: int) -> StopEvaluation:
    return StopEvaluation(current_turn=turn_number)


def _build_runner(
    store: SqliteMatchStore,
    prepared: PreparedRun,
    *,
    loader: Any,
    tmp_path: Path,
    current_turn: int | None,
    recovery_attempt_limit: int = 3,
    halt_play_loop: bool = True,
) -> Runner:
    """A ``Runner`` whose per-turn seam hands back the *real* ``RecoveryEngine``, wired to
    *loader* -- exactly how ``run/composition.py`` wires the production one.

    ``halt_play_loop`` makes every call *after* the one ``resume_from`` itself makes raise, so the
    play loop `resume_from` schedules stops immediately at a recorded ``paused`` instead of trying
    to drive a turn against a game client that does not exist here. The turn number it reports is
    how these tests observe *where* the loop resumed.
    """
    host = FakeHostPlatform()
    calls = {"n": 0}

    def build_turn_dependencies(prepared: PreparedRun, turn_number: int) -> TurnCycleDependencies:
        calls["n"] += 1
        if halt_play_loop and calls["n"] > 1:
            raise HarnessError("play loop halted by the test after the rewind")

        def build_loop_context(turn_cycle_id: Any) -> DecisionLoopContext:  # pragma: no cover
            raise AssertionError("no turn is ever played in these tests")

        return TurnCycleDependencies(
            run_id=prepared.run.run_id,
            turn_number=turn_number,
            store=store,
            save_capability=None,  # type: ignore[arg-type]  # never reached; no turn is played
            host=host,
            min_free_disk_gb=1.0,
            disk_check_path=tmp_path,
            build_loop_context=build_loop_context,
            recovery=RecoveryEngine(
                run_id=prepared.run.run_id,
                store=store,
                loader=loader,
                recovery_attempt_limit=recovery_attempt_limit,
            ),
            home=tmp_path,
        )

    runner = Runner(
        RunnerDependencies(
            store=store,
            prepare_run=lambda _config: prepared,  # never called; state is injected below
            build_turn_dependencies=build_turn_dependencies,
            evaluate_stop_facts=_evaluate_stop_facts,
        )
    )
    runner._runs[prepared.run.run_id] = _RunState(
        run=prepared.run, prepared=prepared, current_turn=current_turn
    )
    return runner


async def _wait_until(condition: Any, *, timeout_s: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition did not become true in time")
        await asyncio.sleep(0.01)


def _events(store: SqliteMatchStore, event_type: RunEventType) -> list[Any]:
    return store.list_run_events(RUN_ID, event_types=[event_type])


# --------------------------------------------------------------------------
# What resume_from refuses, before anything is touched
# --------------------------------------------------------------------------


def test_unknown_run_is_refused(store: SqliteMatchStore, tmp_path: Path) -> None:
    prepared = _seed_run(store, through_turn=3)
    runner = _build_runner(
        store, prepared, loader=_RecordingSaveLoader(), tmp_path=tmp_path, current_turn=3
    )
    with pytest.raises(HarnessError, match="unknown run_id"):
        runner.resume_from("run-that-never-existed", 2)  # type: ignore[arg-type]


def test_turn_zero_is_refused_as_not_a_turn(store: SqliteMatchStore, tmp_path: Path) -> None:
    prepared = _seed_run(store, through_turn=3)
    runner = _build_runner(
        store, prepared, loader=_RecordingSaveLoader(), tmp_path=tmp_path, current_turn=3
    )
    with pytest.raises(ResumeFromRefused, match="turns are numbered from 1"):
        runner.resume_from(RUN_ID, 0)


def test_an_actively_playing_run_is_refused_rather_than_having_its_turn_cut_short(
    store: SqliteMatchStore, tmp_path: Path
) -> None:
    """FR-004/FR-008/SC-022: a turn is never cut short to honour an operator command. Rewinding
    under a live play loop would do exactly that, so ``playing`` is refused with the remedy named
    (pause first), and nothing is abandoned, loaded, or written."""
    prepared = _seed_run(store, through_turn=3, lifecycle_state="playing")
    loader = _RecordingSaveLoader()
    runner = _build_runner(store, prepared, loader=loader, tmp_path=tmp_path, current_turn=3)

    with pytest.raises(ResumeFromRefused, match="pause the run"):
        runner.resume_from(RUN_ID, 2)

    assert loader.loaded == []
    assert _events(store, RunEventType.TURN_ABANDONED) == []
    assert store.get_turn_cycle(RUN_ID, 2) is not None  # still authoritative, untouched


def test_a_terminal_run_is_refused(store: SqliteMatchStore, tmp_path: Path) -> None:
    prepared = _seed_run(store, through_turn=3)
    finished = prepared.run.model_copy(
        update={
            "lifecycle_state": LifecycleState.FINISHED,
            "ended_at": NOW,
            "stop_resolution": "turn_reached",
        }
    )
    prepared = PreparedRun(run=finished, stop_condition=prepared.stop_condition)
    runner = _build_runner(
        store, prepared, loader=_RecordingSaveLoader(), tmp_path=tmp_path, current_turn=3
    )
    with pytest.raises(ResumeFromRefused, match="terminal lifecycle state"):
        runner.resume_from(RUN_ID, 2)


def test_a_save_never_recorded_is_reported_and_never_retargeted(
    store: SqliteMatchStore, tmp_path: Path
) -> None:
    """FR-036: a required save that is not on record is named, not quietly swapped for a nearby
    turn's save. Turn 9 was never played, so there is no turn-9 save point."""
    prepared = _seed_run(store, through_turn=3)
    loader = _RecordingSaveLoader()
    runner = _build_runner(store, prepared, loader=loader, tmp_path=tmp_path, current_turn=3)

    with pytest.raises(SaveAddressingError) as excinfo:
        runner.resume_from(RUN_ID, 9)

    assert excinfo.value.detail["turn"] == 9
    assert excinfo.value.detail["expected_save_name"] == f"civsim__{RUN_ID}__t0009"
    assert loader.loaded == []


def test_a_save_recorded_removed_is_refused_rather_than_resumed_from_a_different_turn(
    store: SqliteMatchStore, tmp_path: Path
) -> None:
    """FR-036's other half: the reaper deleted turn 2's file (``retention_status=removed``). The
    rewind fails naming that save -- it does not fall back to turn 1 or turn 3."""
    prepared = _seed_run(store, through_turn=3)
    store.write_save_point(_make_save_point(RUN_ID, 2, retention_status=RetentionStatus.REMOVED))
    loader = _RecordingSaveLoader()
    runner = _build_runner(store, prepared, loader=loader, tmp_path=tmp_path, current_turn=3)

    with pytest.raises(SaveAddressingError, match="no longer present on disk"):
        runner.resume_from(RUN_ID, 2)

    assert loader.loaded == []


# --------------------------------------------------------------------------
# A loader that cannot load: the rewind stays non-destructive
# --------------------------------------------------------------------------


def test_a_loader_that_cannot_load_fails_specifically_and_leaves_the_record_intact(
    store: SqliteMatchStore, tmp_path: Path
) -> None:
    """A ``SaveLoader`` whose load raises -- as the production ``LuaSaveLoader`` (T217) does for
    a load the client refuses or that lands on the wrong position -- must surface *that* reason
    rather than a generic failure, and -- because the supersede is ordered after the load --
    must leave every recorded attempt exactly as authoritative as it was."""
    prepared = _seed_run(store, through_turn=3)
    runner = _build_runner(
        store, prepared, loader=_NoLoadPathLoader(), tmp_path=tmp_path, current_turn=3
    )

    with pytest.raises(ResumeFromFailed) as excinfo:
        runner.resume_from(RUN_ID, 2)

    failure = excinfo.value
    assert "no verified live save-load path exists in this codebase yet" in failure.message
    assert failure.detail["error_type"] == "HarnessError"
    assert failure.detail["resumed_from_turn"] == 2
    assert failure.detail["save_point_id"] == f"{RUN_ID}-sp2"

    # Nothing was abandoned in the record: turns 2 and 3 are still the authoritative attempts.
    for turn in (1, 2, 3):
        record = store.get_turn_cycle(RUN_ID, turn, authoritative_only=True)
        assert record is not None
        assert record.turn_cycle.is_authoritative

    # The run is left where RecoveryEngine's own contract leaves it, below the recovery bound.
    persisted = store.get_run(RUN_ID)
    assert persisted is not None
    assert persisted.lifecycle_state is LifecycleState.RESUMING
    assert runner.get_status(RUN_ID).last_error is not None

    # Principle IV: even a rewind that never completed recorded which lineage it was resuming from.
    abandoned = _events(store, RunEventType.TURN_ABANDONED)
    assert len(abandoned) == 1
    assert abandoned[0].detail["command"] == "resume-from"
    assert abandoned[0].detail["resumed_from_run_id"] == RUN_ID
    assert abandoned[0].detail["resumed_from_turn"] == 2
    assert abandoned[0].detail["abandoned_from_turn"] == 3


# --------------------------------------------------------------------------
# A rewind that completes: lineage, supersede, and where play resumes
# --------------------------------------------------------------------------


async def test_a_completed_rewind_records_its_lineage_and_supersedes_what_it_abandoned(
    store: SqliteMatchStore, tmp_path: Path
) -> None:
    """Constitution Principle IV, verbatim: "any branch that mutates or abandons a save MUST
    record which lineage it branched from". A resume-from abandons turns 2 and 3 and will
    overwrite their quicksaves on replay, so the ``resumed`` event names the run and turn resumed
    from, the save point loaded, the turn abandoned from, and every superseded attempt.

    FR-047: those attempts are marked non-authoritative, never deleted -- each stays retrievable.
    """
    prepared = _seed_run(store, through_turn=3)
    loader = _RecordingSaveLoader()
    runner = _build_runner(store, prepared, loader=loader, tmp_path=tmp_path, current_turn=3)

    runner.resume_from(RUN_ID, 2)

    # The save that was actually loaded is turn 2's own turn-start quicksave.
    assert [save.save_point_id for save in loader.loaded] == [f"{RUN_ID}-sp2"]

    resumed = _events(store, RunEventType.RESUMED)
    lineage = next(e for e in resumed if e.detail.get("command") == "resume-from")
    assert lineage.turn_number == 2
    assert lineage.detail["resumed_from_run_id"] == RUN_ID
    assert lineage.detail["resumed_from_turn"] == 2
    assert lineage.detail["save_point_id"] == f"{RUN_ID}-sp2"
    assert lineage.detail["save_name"] == f"civsim__{RUN_ID}__t0002"
    assert lineage.detail["abandoned_from_turn"] == 3
    assert lineage.detail["superseded_turns"] == [
        {"turn_number": 2, "attempt_index": 0},
        {"turn_number": 3, "attempt_index": 0},
    ]

    # Turn 1 is before the rewind point and is untouched; turns 2 and 3 are abandoned but kept.
    assert store.get_turn_cycle(RUN_ID, 1, authoritative_only=True) is not None
    for turn in (2, 3):
        assert store.get_turn_cycle(RUN_ID, turn, authoritative_only=True) is None
        kept = store.get_turn_cycle(RUN_ID, turn, authoritative_only=False)
        assert kept is not None  # FR-047: never deleted
        assert not kept.turn_cycle.is_authoritative

    # The whole rewind is on the timeline through the legal SS4 edges, not a widened graph.
    seen = [
        (e.detail.get("from"), e.detail.get("to"))
        for e in _events(store, RunEventType.LIFECYCLE_TRANSITION)
    ]
    assert ("paused", "playing") in seen
    assert ("playing", "interrupted") in seen
    assert ("interrupted", "resuming") in seen
    assert ("resuming", "playing") in seen


async def test_play_resumes_at_the_turn_that_was_resumed_from(
    store: SqliteMatchStore, tmp_path: Path
) -> None:
    """``resume_from`` differs from ``start`` only in where the play loop begins counting: the
    loop it schedules plays *turn 2*, not turn 4. The test's play loop is halted on its first
    iteration, and the turn number it reports on the way out is the observable proof."""
    prepared = _seed_run(store, through_turn=3)
    runner = _build_runner(
        store, prepared, loader=_RecordingSaveLoader(), tmp_path=tmp_path, current_turn=3
    )

    runner.resume_from(RUN_ID, 2)
    await _wait_until(lambda: runner.get_status(RUN_ID).last_error is not None)

    status = runner.get_status(RUN_ID)
    assert status.current_turn == 2
    assert status.lifecycle_state is LifecycleState.PAUSED  # the halted loop's recorded stop

    paused = [
        e
        for e in _events(store, RunEventType.LIFECYCLE_TRANSITION)
        if e.detail.get("from") == "playing" and e.detail.get("to") == "paused"
    ]
    assert len(paused) == 1
    assert paused[0].turn_number == 2


async def test_resuming_from_turn_one_replays_the_whole_run(
    store: SqliteMatchStore, tmp_path: Path
) -> None:
    """The boundary case: turn 1 is a legal target, and every recorded attempt is superseded."""
    prepared = _seed_run(store, through_turn=3)
    runner = _build_runner(
        store, prepared, loader=_RecordingSaveLoader(), tmp_path=tmp_path, current_turn=3
    )

    runner.resume_from(RUN_ID, 1)
    await _wait_until(lambda: runner.get_status(RUN_ID).last_error is not None)

    assert runner.get_status(RUN_ID).current_turn == 1
    for turn in (1, 2, 3):
        assert store.get_turn_cycle(RUN_ID, turn, authoritative_only=True) is None

    lineage = next(
        e for e in _events(store, RunEventType.RESUMED) if e.detail.get("command") == "resume-from"
    )
    assert lineage.detail["superseded_turns"] == [
        {"turn_number": 1, "attempt_index": 0},
        {"turn_number": 2, "attempt_index": 0},
        {"turn_number": 3, "attempt_index": 0},
    ]


async def test_a_run_interrupted_mid_rewind_can_be_resumed_again(
    store: SqliteMatchStore, tmp_path: Path
) -> None:
    """A rewind that failed at the load leaves the run at ``resuming`` (above). That is not a dead
    end: ``resuming`` is one of the states ``resume_from`` accepts, so the operator can simply
    issue the command again once the load path works -- which is exactly the T217 unblock path."""
    prepared = _seed_run(store, through_turn=3, lifecycle_state="resuming")
    loader = _RecordingSaveLoader()
    runner = _build_runner(store, prepared, loader=loader, tmp_path=tmp_path, current_turn=3)

    runner.resume_from(RUN_ID, 2)
    await _wait_until(lambda: runner.get_status(RUN_ID).last_error is not None)

    assert [save.save_point_id for save in loader.loaded] == [f"{RUN_ID}-sp2"]
    # `resuming` needs no abandon step repeated -- RecoveryEngine carries it straight forward.
    assert _events(store, RunEventType.TURN_ABANDONED) == []
    assert runner.get_status(RUN_ID).current_turn == 2
