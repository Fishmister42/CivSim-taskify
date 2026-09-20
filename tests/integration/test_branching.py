"""Integration test for branch creation, build enforcement, and abandonment
(T161; FR-033, FR-034, FR-035, invariant I12, SC-014).

Exercises `saves/branching.py` end to end against a real `SqliteMatchStore`:
two branches from the same save point each record `parent_run_id` and
`parent_turn`, and the parent's record -- its `Run`, its turn cycle, and its
save points -- is byte-identical before and after. Also covers T175's
same-build enforcement and T169's branch abandonment.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from civsim_harness.errors import StoreWriteError
from civsim_harness.models.common import BuildAcceptance, CatalogVersionRef
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.decision import Decision
from civsim_harness.models.records import ModelCall, RetentionStatus, SavePoint
from civsim_harness.models.run import Run
from civsim_harness.models.turn import DecisionStep, Observation, TurnCycle
from civsim_harness.saves.addressing import SaveAddressingError
from civsim_harness.saves.branching import (
    BranchBuildMismatchError,
    BranchFrom,
    BranchPlatformSpikeRequiredError,
    BranchSource,
    BranchSourceMissingError,
    abandon_branch,
    create_branch,
)
from civsim_harness.store.port import DecisionStepBundle, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

NOW = datetime(2026, 9, 19, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteMatchStore]:
    adapter = SqliteMatchStore(tmp_path / "match.db")
    yield adapter
    adapter.close()


class _RecordingLoader:
    """A `saves.branching.SaveLoader` that records every save it was asked
    to load, and does nothing else -- there is no real game client here.
    """

    def __init__(self) -> None:
        self.loaded: list[SavePoint] = []

    async def load(self, save: SavePoint) -> None:
        self.loaded.append(save)


# --------------------------------------------------------------------------
# Record builders (mirrors tests/contract/test_match_store_port.py's own
# minimal-valid-instance style)
# --------------------------------------------------------------------------


def _catalog_ref() -> dict[str, str]:
    return {"version": "2026.09.1", "content_hash": "abc123"}


def _make_config(config_id: str, *, model: str = "anthropic/claude-opus-5") -> RunConfiguration:
    return RunConfiguration.model_validate(
        {
            "config_id": config_id,
            "map_seed": "1849275663",
            "civilization": "CIVILIZATION_ROME",
            "leader": "LEADER_TRAJAN",
            "ruleset": "RULESET_STANDARD",
            "difficulty": "DIFFICULTY_PRINCE",
            "stop_condition": {"type": "turn_reached", "turn": 50},
            "model_config": {"primary": {"provider": "openrouter", "model": model}},
            "no_progress_step_limit": 8,
            "recovery_attempt_limit": 3,
            "min_free_disk_gb": 25,
            "created_at": NOW,
        }
    )


def _make_run(
    run_id: str,
    *,
    game_build: str = "win/1.0.12.9",
    lifecycle_state: str = "playing",
) -> Run:
    return Run.model_validate(
        {
            "run_id": run_id,
            "config_id": f"cfg-{run_id}",
            "lifecycle_state": lifecycle_state,
            "record_completeness_status": "unknown",
            "comparability_status": "comparable",
            "observation_catalog_version": _catalog_ref(),
            "action_catalog_version": _catalog_ref(),
            "game_build": game_build,
            "host_support_tier": "validated",
            "capture_path": "windows_graphics_capture",
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
    run_id: str, turn_number: int, *, save_point_id: str, attempt_index: int = 0
) -> TurnCycleRecord:
    turn_cycle_id = f"{run_id}-t{turn_number}-a{attempt_index}"
    tc = TurnCycle.model_validate(
        {
            "turn_cycle_id": turn_cycle_id,
            "run_id": run_id,
            "turn_number": turn_number,
            "attempt_index": attempt_index,
            "is_authoritative": True,
            "save_point_id": save_point_id,
            "step_count": 1,
            "outcome": "ended_by_agent",
            "final_no_progress_streak": 0,
            "visually_degraded": False,
            "started_at": NOW,
            "ended_at": NOW,
            "persisted_at": NOW,
        }
    )
    return TurnCycleRecord(turn_cycle=tc, steps=[_make_step_bundle(run_id, turn_cycle_id, 1)])


def _make_save_point(save_point_id: str, run_id: str, turn: int) -> SavePoint:
    return SavePoint.model_validate(
        {
            "save_point_id": save_point_id,
            "run_id": run_id,
            "turn_number": turn,
            "save_name": f"civsim__{run_id}__t{turn:04d}",
            "taken_at": NOW,
            "verified": True,
            "retention_status": "retained",
        }
    )


def _seed_parent_through_turn(store: SqliteMatchStore, run_id: str, *, up_to_turn: int) -> None:
    store.create_run(_make_run(run_id), _make_config(f"cfg-{run_id}"))
    for turn in range(1, up_to_turn + 1):
        save_point_id = f"{run_id}-sp{turn}"
        store.write_save_point(_make_save_point(save_point_id, run_id, turn))
        store.write_turn_cycle(_make_turn_cycle_record(run_id, turn, save_point_id=save_point_id))


def _branch_source(
    run_id: str, *, model: str = "anthropic/claude-sonnet-5", game_build: str = "win/1.0.12.9"
) -> BranchSource:
    return BranchSource(
        run_id=run_id,
        config=_make_config(f"cfg-{run_id}", model=model),
        game_build=game_build,
        observation_catalog_version=CatalogVersionRef.model_validate(_catalog_ref()),
        action_catalog_version=CatalogVersionRef.model_validate(_catalog_ref()),
        host_support_tier="validated",  # type: ignore[arg-type]
        capture_path="windows_graphics_capture",  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# T161: two branches from the same save point; the parent is untouched
# --------------------------------------------------------------------------


async def test_two_branches_from_the_same_save_point_each_record_lineage(
    store: SqliteMatchStore,
) -> None:
    _seed_parent_through_turn(store, "parent", up_to_turn=23)

    parent_before = store.get_run("parent")
    parent_turn23_before = store.get_turn_cycle("parent", 23)
    parent_saves_before = store.list_save_points("parent")
    assert parent_before is not None

    loader_a = _RecordingLoader()
    child_a, save_a, event_a = await create_branch(
        store,
        loader_a,
        branch_from=BranchFrom(run_id="parent", turn=23),
        child=_branch_source("branch-a", model="anthropic/claude-sonnet-5"),
        occurred_at=NOW,
    )

    loader_b = _RecordingLoader()
    child_b, save_b, event_b = await create_branch(
        store,
        loader_b,
        branch_from=BranchFrom(run_id="parent", turn=23),
        child=_branch_source("branch-b", model="anthropic/claude-opus-5"),
        occurred_at=NOW,
    )

    # -- both branches record parent_run_id and parent_turn (FR-033) --
    assert child_a.parent_run_id == "parent"
    assert child_a.parent_turn == 23
    assert child_b.parent_run_id == "parent"
    assert child_b.parent_turn == 23
    assert child_a.run_id != child_b.run_id

    # -- both loaded the identical parent save point (SC-014's record-side
    # precondition: both branches start from the same recorded position) --
    assert save_a.save_point_id == save_b.save_point_id == "parent-sp23"
    assert loader_a.loaded == [save_a]
    assert loader_b.loaded == [save_b]

    # -- branch_created was recorded for each child, naming its lineage --
    assert event_a.event_type.value == "branch_created"
    assert event_a.detail["parent_run_id"] == "parent"
    assert event_a.detail["parent_turn"] == 23
    assert event_b.detail["parent_run_id"] == "parent"

    # -- the parent's record is byte-identical before and after (FR-034, I12) --
    parent_after = store.get_run("parent")
    assert parent_after is not None
    assert parent_after.model_dump_json() == parent_before.model_dump_json()
    assert store.get_turn_cycle("parent", 23) == parent_turn23_before
    assert store.list_save_points("parent") == parent_saves_before

    # -- each child is independently retrievable and distinct from the parent --
    assert store.get_run("branch-a") == child_a
    assert store.get_run("branch-b") == child_b


async def test_branch_from_a_missing_save_is_rejected_never_retargeted(
    store: SqliteMatchStore,
) -> None:
    _seed_parent_through_turn(store, "parent-gapped", up_to_turn=24)
    # Simulate the reaper having removed turn 23's file after archival --
    # the record survives, marked removed, per T171.
    sp23 = next(sp for sp in store.list_save_points("parent-gapped") if sp.turn_number == 23)
    store.write_save_point(sp23.model_copy(update={"retention_status": RetentionStatus.REMOVED}))

    loader = _RecordingLoader()
    with pytest.raises(SaveAddressingError) as excinfo:
        await create_branch(
            store,
            loader,
            branch_from=BranchFrom(run_id="parent-gapped", turn=23),
            child=_branch_source("branch-gap"),
            occurred_at=NOW,
        )

    assert excinfo.value.detail["turn"] == 23
    assert loader.loaded == []  # never attempted to load anything else instead
    assert store.get_run("branch-gap") is None  # never constructed a run either


async def test_branch_from_a_nonexistent_parent_run_is_rejected(store: SqliteMatchStore) -> None:
    loader = _RecordingLoader()
    with pytest.raises(BranchSourceMissingError):
        await create_branch(
            store,
            loader,
            branch_from=BranchFrom(run_id="does-not-exist", turn=1),
            child=_branch_source("branch-orphan"),
            occurred_at=NOW,
        )


# --------------------------------------------------------------------------
# T168: mark_turn_superseded refuses to touch a turn that is now a lineage
# point (exercised here, at the integration level, once a branch exists)
# --------------------------------------------------------------------------


async def test_a_branched_from_turn_cannot_be_superseded_on_the_parent(
    store: SqliteMatchStore,
) -> None:
    _seed_parent_through_turn(store, "parent-locked", up_to_turn=23)
    loader = _RecordingLoader()
    await create_branch(
        store,
        loader,
        branch_from=BranchFrom(run_id="parent-locked", turn=23),
        child=_branch_source("branch-locked"),
        occurred_at=NOW,
    )

    with pytest.raises(StoreWriteError):
        store.mark_turn_superseded("parent-locked", 23, 0)

    # And the parent's turn 23 record is still there, still authoritative.
    assert store.get_turn_cycle("parent-locked", 23) is not None


# --------------------------------------------------------------------------
# T175: same-build branching
# --------------------------------------------------------------------------


async def test_branch_with_a_different_build_and_no_acceptance_is_refused(
    store: SqliteMatchStore,
) -> None:
    _seed_parent_through_turn(store, "parent-build", up_to_turn=1)
    loader = _RecordingLoader()

    with pytest.raises(BranchBuildMismatchError):
        await create_branch(
            store,
            loader,
            branch_from=BranchFrom(run_id="parent-build", turn=1),
            child=_branch_source("branch-build-mismatch", game_build="win/1.0.12.11"),
            occurred_at=NOW,
        )
    assert loader.loaded == []


async def test_branch_with_a_version_only_accepted_build_change_succeeds(
    store: SqliteMatchStore,
) -> None:
    _seed_parent_through_turn(store, "parent-vb", up_to_turn=1)
    acceptance = BuildAcceptance(
        acceptance_id="acc-1",
        from_build="win/1.0.12.9",
        to_build="win/1.0.12.11",
        accepted_by="researcher",
        accepted_at=NOW,
        reason="patch notes affect only matchmaking",
    )

    child, _, event = await create_branch(
        store,
        _RecordingLoader(),
        branch_from=BranchFrom(run_id="parent-vb", turn=1),
        child=_branch_source("branch-vb", game_build="win/1.0.12.11"),
        accepted_build_changes=[acceptance],
        occurred_at=NOW,
    )

    assert child.game_build_acceptance_ref == "acc-1"
    assert event.detail["game_build_acceptance_ref"] == "acc-1"
    assert event.detail["is_platform_transition"] is False


async def test_branch_with_a_platform_crossing_acceptance_but_no_spike_ref_is_refused(
    store: SqliteMatchStore,
) -> None:
    _seed_parent_through_turn(store, "parent-plat", up_to_turn=1)
    acceptance = BuildAcceptance(
        acceptance_id="acc-2",
        from_build="win/1.0.12.9",
        to_build="mac/1.0.12.9",
        accepted_by="researcher",
        accepted_at=NOW,
        reason="testing cross-platform",
    )

    with pytest.raises(BranchPlatformSpikeRequiredError):
        await create_branch(
            store,
            _RecordingLoader(),
            branch_from=BranchFrom(run_id="parent-plat", turn=1),
            child=_branch_source("branch-plat", game_build="mac/1.0.12.9"),
            accepted_build_changes=[acceptance],
            occurred_at=NOW,
        )


# --------------------------------------------------------------------------
# T169: branch abandonment marks turns superseded, never deletes them
# --------------------------------------------------------------------------


async def test_abandon_branch_marks_turns_superseded_and_records_the_event(
    store: SqliteMatchStore,
) -> None:
    _seed_parent_through_turn(store, "parent-ab", up_to_turn=5)
    child, _, _ = await create_branch(
        store,
        _RecordingLoader(),
        branch_from=BranchFrom(run_id="parent-ab", turn=5),
        child=_branch_source("branch-ab"),
        occurred_at=NOW,
    )
    # The branch plays two of its own turns before being abandoned.
    for turn in (6, 7):
        save_point_id = f"branch-ab-sp{turn}"
        store.write_save_point(_make_save_point(save_point_id, "branch-ab", turn))
        store.write_turn_cycle(
            _make_turn_cycle_record("branch-ab", turn, save_point_id=save_point_id)
        )

    original_turn6 = store.get_turn_cycle("branch-ab", 6)
    assert original_turn6 is not None

    event = abandon_branch(
        store, "branch-ab", turns=[6, 7], reason="strategy diverged badly", occurred_at=NOW
    )

    assert event.event_type.value == "branch_abandoned"
    assert event.detail["reason"] == "strategy diverged badly"
    assert {t["turn_number"] for t in event.detail["superseded_turns"]} == {6, 7}

    # Superseded, not deleted: no longer authoritative, but still retrievable.
    assert store.get_turn_cycle("branch-ab", 6, authoritative_only=True) is None
    still_there = store.get_turn_cycle("branch-ab", 6, authoritative_only=False)
    assert still_there is not None
    assert still_there.turn_cycle.turn_cycle_id == original_turn6.turn_cycle.turn_cycle_id
    assert still_there.turn_cycle.outcome.value == "abandoned"


async def test_abandon_branch_rejects_a_run_with_no_parent(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("standalone-run"), _make_config("cfg-standalone"))
    from civsim_harness.saves.branching import BranchTargetError

    with pytest.raises(BranchTargetError):
        abandon_branch(store, "standalone-run", turns=[1], occurred_at=NOW)
