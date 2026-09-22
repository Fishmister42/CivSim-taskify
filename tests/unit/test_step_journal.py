"""The per-step durability journal (``run/step_journal.py``; Constitution Principle III).

MEASURED, and the reason this file exists: ``run-0fc1d14640d24f85b4a5b93695f7dfa2`` ran one turn
for 67 minutes on 2026-09-22 and was killed. The store holds 285 captures with 285 distinct
``decision_step_id``s and **zero** ``decision_steps``, ``turn_cycles`` and ``model_calls`` rows.
Decision steps became durable only at turn commit, so 285 successful steps' whole reasoning died
with the process.

Two things must now both hold, and they pull in opposite directions -- which is why they are
tested together:

1. a turn killed mid-flight leaves its completed steps **reconstructible**; and
2. that record is **not readable as a complete turn** and is **excluded from trending**.

Either alone is easy and wrong. Persisting nothing satisfies (2). Persisting the steps and
letting the run count as complete satisfies (1) and violates Principle III's other half, which is
what ``store/trends.py``'s allowlist gate exists to prevent.

**Axis.** The twin runs below differ in exactly one thing: **whether the turn committed**. Same
run shape, same turn number, same step count, same step content, same lifecycle state, same
comparability. Varying the goal, the turn number or the step text instead would prove only that
these fixtures differ from each other.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.host.detect import HostInfo, LinuxSessionType, OperatingSystem
from civsim_harness.models.common import (
    CatalogVersionRef,
    DeclarationId,
    ModelRef,
    RunId,
    TurnCycleId,
)
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.run import RecordCompletenessStatus
from civsim_harness.parity.screening import load_screening_profiles
from civsim_harness.run.decision_loop import DecisionLoopContext, run_decision_loop
from civsim_harness.run.step_journal import (
    BUNDLE_KEY,
    STEP_JOURNAL_EVENT_TYPE,
    TURN_CYCLE_KEY,
    JournalledTurnStatus,
    ReconstructedTurn,
    build_step_journal_event,
    reconstruct_journalled_turns,
)
from civsim_harness.store.completeness import record_completeness_status
from civsim_harness.store.contract import ExclusionReason
from civsim_harness.store.port import TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from civsim_harness.store.trends import exclusion_for
from fakes.fake_host import FakeHostPlatform
from fakes.fake_provider import FakeModelProvider
from store_support.builders import (
    make_config,
    make_run,
    make_save_point,
    make_turn_cycle_record,
)

# The 500-step productive-turn rig from `test_no_truncation.py`, reused rather than copied: it is
# already the canonical "a turn that keeps going" fake in this suite, and its `_decision_factory`
# only flags `is_end_turn` at step 500 -- so a loop driven with it and interrupted earlier is
# precisely the shape of the run above, a turn that never ends.
from unit.test_no_truncation import _build_registry, _decision_factory, _FakeGame


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "match.db"


@pytest.fixture
def store(store_path: Path) -> Iterator[SqliteMatchStore]:
    opened = SqliteMatchStore(store_path)
    try:
        yield opened
    finally:
        opened.close()


def _seed_run(store: SqliteMatchStore, run_id: str, *, lifecycle_state: str = "playing") -> None:
    store.create_run(
        make_run(run_id, f"{run_id}-cfg", lifecycle_state=lifecycle_state),
        make_config(f"{run_id}-cfg"),
    )


# --------------------------------------------------------------------------
# (1) A turn killed mid-flight leaves its completed steps reconstructible
# --------------------------------------------------------------------------


class _GameThatVanishesMidTurn(_FakeGame):
    """The client goes away on the *n*-th read, with no warning and no chance to flush.

    The exception is a bare ``RuntimeError``, not an ``ObservationAssemblyError``: the latter is
    wrapped into ``MidTurnObservationFailure``, which the *caller* uses to persist an abandoned
    attempt. This one propagates straight out of ``run_decision_loop`` with nobody catching it,
    so no exit path of any kind runs -- the closest an in-process test can get to the SIGKILL
    that actually happened, and the case an on-exit flush could never have covered.
    """

    def __init__(self, vanish_on_read: int) -> None:
        super().__init__()
        self.reads = 0
        self.vanish_on_read = vanish_on_read

    async def read(self) -> tuple[Sequence[Any], str]:
        self.reads += 1
        if self.reads >= self.vanish_on_read:
            raise RuntimeError("the game client is gone")
        return await super().read()

    async def execute(
        self, declaration_id: DeclarationId, parameters: Mapping[str, Any], target: Any
    ) -> None:
        await super().execute(declaration_id, parameters, target)


def _loop_context(store: SqliteMatchStore, game: _FakeGame, run_id: str) -> DecisionLoopContext:
    provider = FakeModelProvider()
    provider.set_default_decision_factory(_decision_factory)
    return DecisionLoopContext(
        run_id=RunId(run_id),
        turn_number=1,
        turn_cycle_id=TurnCycleId("tc-killed"),
        registry=_build_registry(),
        catalog_version=CatalogVersionRef(version="test", content_hash="test"),
        model=ModelRef(provider="test", model="test-model"),
        guidance=None,
        provider=provider,
        no_progress_step_limit=10,
        read_observation_inputs=game.read,
        execute_action=game.execute,
        host=FakeHostPlatform(),
        host_info=HostInfo(
            os=OperatingSystem.linux, os_version="test", session_type=LinuxSessionType.x11
        ),
        view_declaration_id=DeclarationId("views.test"),
        screening_profiles=load_screening_profiles(),
        store=store,
    )


async def test_a_turn_killed_mid_flight_leaves_its_completed_steps_reconstructible(
    store_path: Path,
) -> None:
    """The real loop, the real store, and then a **separate connection** to the same file.

    Reading the journal back through a second opener is the point: it proves the steps are bytes
    on disk rather than objects the writer still happens to be holding. Under the old boundary
    this read returned nothing at all until the turn committed.
    """
    writer = SqliteMatchStore(store_path)
    _seed_run(writer, "run-killed")
    game = _GameThatVanishesMidTurn(vanish_on_read=5)
    try:
        with pytest.raises(RuntimeError, match="the game client is gone"):
            await run_decision_loop(_loop_context(writer, game, "run-killed"))
        # Nothing flushed anything: the turn never committed, exactly as a kill leaves it.
        assert writer.get_turn_cycle(RunId("run-killed"), 1, authoritative_only=False) is None
    finally:
        writer.close()

    reader = SqliteMatchStore(store_path, read_only=True)
    try:
        turns = reconstruct_journalled_turns(reader, RunId("run-killed"))
        assert len(turns) == 1
        recovered = turns[0]
        # Four reads: steps 1-3 completed and were journalled; the fourth read killed the loop.
        assert len(recovered.steps) == 3
        assert [bundle.step.step_index for bundle in recovered.steps] == [1, 2, 3]
        # The reasoning -- the thing that died with the process on run-0fc1d146 -- is here.
        assert recovered.steps[0].decision.reasoning == "step 1: keep advancing"
        assert all(bundle.decision.reasoning for bundle in recovered.steps)
        # ... and so is what the agent was looking at, and what its order actually did.
        assert recovered.steps[0].observation.entries
        assert recovered.steps[0].decision.execution is not None
    finally:
        reader.close()


# --------------------------------------------------------------------------
# (2) ... and that record is not readable as a complete turn
# --------------------------------------------------------------------------


def _journal_a_turn(
    store: SqliteMatchStore, run_id: str, record: TurnCycleRecord, *, minute: int = 0
) -> None:
    """Write the journal entries the loop would have written for *record*'s steps."""
    from store_support.builders import at

    for offset, bundle in enumerate(record.steps):
        store.write_run_event(
            build_step_journal_event(
                run_id=RunId(run_id),
                turn_number=record.turn_cycle.turn_number,
                turn_cycle_id=record.turn_cycle.turn_cycle_id,
                bundle=bundle,
                occurred_at=at(minute + offset),
            )
        )


def _twin(
    store: SqliteMatchStore, run_id: str, *, committed: bool, lifecycle_state: str = "playing"
) -> TurnCycleRecord:
    """One run, one turn, three steps, journalled. The **only** thing the twins vary is whether
    that turn then reached ``write_turn_cycle``."""
    _seed_run(store, run_id, lifecycle_state=lifecycle_state)
    record = make_turn_cycle_record(run_id, turn_number=1, num_steps=3)
    store.write_save_point(make_save_point(f"{run_id}-sp1-0", run_id, 1))
    _journal_a_turn(store, run_id, record)
    if committed:
        store.write_turn_cycle(record)
    return record


def test_the_twins_differ_only_in_whether_the_turn_committed(store: SqliteMatchStore) -> None:
    whole = _twin(store, "run-whole", committed=True)
    killed = _twin(store, "run-killed", committed=False)
    # The axis check: everything but commitment is identical between the twins.
    assert [b.step.step_index for b in whole.steps] == [b.step.step_index for b in killed.steps]
    assert [b.decision.reasoning for b in whole.steps] == [
        b.decision.reasoning for b in killed.steps
    ]
    assert whole.turn_cycle.turn_number == killed.turn_cycle.turn_number

    recovered_whole = reconstruct_journalled_turns(store, RunId("run-whole"))[0]
    recovered_killed = reconstruct_journalled_turns(store, RunId("run-killed"))[0]

    # Both recover the same three steps -- the reasoning is not lost either way.
    assert len(recovered_whole.steps) == len(recovered_killed.steps) == 3

    # But only one of them is a turn.
    assert recovered_whole.status is JournalledTurnStatus.COMMITTED
    assert recovered_whole.total_step_count == 3
    assert recovered_whole.unresolved_reason is None
    assert recovered_whole.describes_a_complete_turn() is True

    assert recovered_killed.status is JournalledTurnStatus.UNCOMMITTED
    assert recovered_killed.total_step_count is None
    assert recovered_killed.describes_a_complete_turn() is False
    # 882758e: the gap is named, not emitted as a silent short answer.
    assert recovered_killed.unresolved_reason is not None
    assert "never reached write_turn_cycle" in recovered_killed.unresolved_reason
    assert "floor, not the total" in recovered_killed.unresolved_reason


def test_a_reconstructed_turn_cannot_be_substituted_for_a_turn_record(
    store: SqliteMatchStore,
) -> None:
    """Structural, not conventional: there is no way to hand this to trending by mistake."""
    _twin(store, "run-killed", committed=False)
    recovered = reconstruct_journalled_turns(store, RunId("run-killed"))[0]
    assert isinstance(recovered, ReconstructedTurn)
    assert not isinstance(recovered, TurnCycleRecord)
    # No TurnCycle anywhere on it: no outcome, no yields, no authoritative step count to borrow.
    assert not hasattr(recovered, "turn_cycle")
    assert not hasattr(recovered, "yields")
    assert not hasattr(recovered, "outcome")


# --------------------------------------------------------------------------
# (3) ... and is excluded from trending
# --------------------------------------------------------------------------


def _exclusion(store: SqliteMatchStore, run_id: str) -> Any:
    run = store.get_run(RunId(run_id))
    assert run is not None
    return exclusion_for(
        run,
        record_completeness_status(store, RunId(run_id)),
        store.turn_gaps(RunId(run_id)),
        include_visually_degraded=False,
    )


def test_a_journalled_but_uncommitted_turn_is_excluded_from_trending(
    store: SqliteMatchStore,
) -> None:
    _twin(store, "run-whole", committed=True)
    _twin(store, "run-killed", committed=False, lifecycle_state="playing")
    _twin(store, "run-stopped", committed=False, lifecycle_state="paused")

    # Positive control: the committed twin really is admitted, so the exclusions below are about
    # commitment and not about these fixtures being unusable in general.
    assert _exclusion(store, "run-whole") is None

    killed = _exclusion(store, "run-killed")
    assert killed is not None
    assert killed.reason is ExclusionReason.RECORD_IN_FLIGHT

    stopped = _exclusion(store, "run-stopped")
    assert stopped is not None
    assert stopped.reason is ExclusionReason.HAS_GAPS


def test_the_journal_cannot_make_an_uncommitted_turn_look_complete(
    tmp_path: Path,
) -> None:
    """The completeness verdict is byte-for-byte the same with and without the journal.

    ``run_events`` is read by ``list_run_events`` and ``export_run`` and by nothing else:
    ``turn_gaps``, ``step_gaps``, ``store/completeness.py`` and ``store/trends.py`` all read
    ``turn_cycles`` and ``decision_steps``. This is that structural claim, asserted.
    """
    verdicts = []
    for name, journalled in (("with", True), ("without", False)):
        opened = SqliteMatchStore(tmp_path / f"{name}.db")
        try:
            _seed_run(opened, "run-killed")
            record = make_turn_cycle_record("run-killed", turn_number=1, num_steps=3)
            opened.write_save_point(make_save_point("run-killed-sp1-0", "run-killed", 1))
            if journalled:
                _journal_a_turn(opened, "run-killed", record)
            verdicts.append(
                (
                    record_completeness_status(opened, RunId("run-killed")),
                    tuple(opened.turn_gaps(RunId("run-killed"))),
                    tuple(opened.step_gaps(RunId("run-killed"), 1)),
                )
            )
        finally:
            opened.close()

    assert verdicts[0] == verdicts[1]
    assert verdicts[0][0] is RecordCompletenessStatus.IN_FLIGHT


# --------------------------------------------------------------------------
# (4) Absence polarity: every unknown resolves away from "it was fine"
# --------------------------------------------------------------------------


def test_an_unreadable_journal_entry_is_named_rather_than_dropped(
    store: SqliteMatchStore,
) -> None:
    record = _twin(store, "run-whole", committed=True)
    # A fourth entry that claims a step but carries a bundle nothing can read back.
    store.write_run_event(
        RunEvent(
            event_id="ev-corrupt",
            run_id=RunId("run-whole"),
            turn_number=1,
            step_index=4,
            event_type=STEP_JOURNAL_EVENT_TYPE,
            occurred_at=record.turn_cycle.ended_at,
            detail={
                TURN_CYCLE_KEY: str(record.turn_cycle.turn_cycle_id),
                BUNDLE_KEY: {"this": "is not a decision step bundle"},
            },
        )
    )
    recovered = reconstruct_journalled_turns(store, RunId("run-whole"))[0]
    assert recovered.unreadable_steps == (4,)
    # It committed, and the three readable steps match the recorded count -- and it still does
    # not read as whole, because one entry could not be read. Absence resolves False.
    assert recovered.status is JournalledTurnStatus.COMMITTED
    assert recovered.total_step_count == 3
    assert recovered.describes_a_complete_turn() is False


def test_a_store_that_cannot_answer_says_unknown_rather_than_guessing(
    store: SqliteMatchStore,
) -> None:
    """The mechanical test: can the record ever say "I do not know"? It must, and it does."""
    _twin(store, "run-killed", committed=False)

    class _NarrowStore:
        """The 002 ``MatchStore`` floor only -- no deliverable-3 attempt addressing."""

        def __init__(self, inner: SqliteMatchStore) -> None:
            self._inner = inner

        def list_run_events(self, run_id: Any, **kwargs: Any) -> Any:
            return self._inner.list_run_events(run_id, **kwargs)

    recovered = reconstruct_journalled_turns(_NarrowStore(store), RunId("run-killed"))[0]  # type: ignore[arg-type]
    assert recovered.status is JournalledTurnStatus.UNKNOWN
    assert recovered.total_step_count is None
    assert recovered.unresolved_reason is not None
    assert "no list_turn_attempts read" in recovered.unresolved_reason
    # Unknown is not "fine": it does not describe a complete turn either.
    assert recovered.describes_a_complete_turn() is False


def test_the_journal_event_makes_no_claim_about_its_turn(store: SqliteMatchStore) -> None:
    """An immutable record must not assert something the future can falsify (FR-006).

    The event says "this step completed"; it never says "this turn has not committed". The turn
    below commits *after* its steps were journalled, and the very same event rows then read as
    committed -- which they could not, had the writer stamped a verdict into them.
    """
    _seed_run(store, "run-late")
    record = make_turn_cycle_record("run-late", turn_number=1, num_steps=2)
    store.write_save_point(make_save_point("run-late-sp1-0", "run-late", 1))
    _journal_a_turn(store, "run-late", record)

    before = reconstruct_journalled_turns(store, RunId("run-late"))[0]
    assert before.status is JournalledTurnStatus.UNCOMMITTED

    store.write_turn_cycle(record)

    after = reconstruct_journalled_turns(store, RunId("run-late"))[0]
    assert after.status is JournalledTurnStatus.COMMITTED
    assert after.describes_a_complete_turn() is True
    # No event was edited or deleted to make that happen -- the store has no such operation.
    journal = store.list_run_events(RunId("run-late"), event_types=[STEP_JOURNAL_EVENT_TYPE])
    assert len(journal) == 2
    assert all(event.event_type is RunEventType.DECISION_STEP_COMPLETED for event in journal)
