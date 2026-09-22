"""The per-step durability journal: a decision step becomes durable when it completes, not
when its turn commits (Constitution Principle III).

**The defect this exists for, measured.** ``run-0fc1d14640d24f85b4a5b93695f7dfa2`` ran one turn
for 67 minutes (2026-09-22, 16:34Z-17:41Z) and was killed. Re-derived read-only from the store,
with positive controls (the same queries return 93 / 44 / 43 decision steps for other runs): 285
captures, 285 distinct ``decision_step_id``s, **0 decision_steps, 0 turn_cycles, 0 model_calls**,
and two ``run_events``. Nothing timed out and nothing retried -- 285 distinct step ids downstream
of the ``DECISION_RETURNED`` check means at least 284 provider calls each returned a decision.
Every step succeeded; the turn simply never ended, and when the process died all 285 steps' whole
reasoning died with it. The only durable per-step trace was the capture row: a picture of the
board with no record of what the agent concluded from it.

That is Principle III failing today -- "every turn of every run MUST be persisted to the
match-tracking store before the next turn begins, with enough game state captured to reconstruct
decisions, yields, and outcomes without replaying the game" -- because the persistence boundary
was the *turn*, and a turn that never ends never crosses it.

**What this module does NOT do, deliberately.** It does not bound the turn. There is no step cap,
no wall clock, no spend ceiling, no watchdog and no timer anywhere in this file or in the call it
adds to ``run/decision_loop.py``; that loop's no-clock property is unchanged, FR-008's unbounded
step count is unchanged, and ``tests/unit/test_no_truncation.py``'s 500-step turn is unaffected.
A long turn is not the defect -- the invisibility was. Detecting "this turn has run 67 minutes"
is spec 004's escalating classification ladder, not this module's business.

**Why a ``RunEvent`` rather than a new store write.** 003 T047 closed the store's mutating
surface: ``store/contract.py``'s ``MUTATING_OPERATIONS`` is a published, exhaustive list, and
FR-006 forbids any operation that deletes or edits a turn, step, capture, event or model call.
A new ``write_decision_step`` would have to join that list, and worse, it would have to write a
``decision_steps`` row whose ``turn_cycle_id`` has no ``turn_cycles`` parent -- a half-formed
record that ``step_gaps``, ``get_turn_cycle`` and ``export_run`` would then have to be taught to
read around. The alternative, growing a ``turn_cycles`` row step by step, is an update-in-place,
which D4 refuses outright ("a retried write under the same key with different content raises")
and which FR-006 rules out of this store entirely.

``write_run_event`` already is what this needs and nothing more:

- **append-only** -- write-once on ``event_id``, no edit path, no delete path;
- **already published** in ``MUTATING_OPERATIONS``, so the closed surface stays closed and
  ``tests/contract/test_store_boundary.py``'s count is unchanged;
- **parent-integral** -- the adapter refuses an event whose ``run_id`` has no ``runs`` row, so a
  journal entry can never dangle;
- **invisible to every completeness and trending read** -- ``run_events`` is read by
  ``list_run_events`` and ``export_run`` and by nothing else; ``turn_gaps``, ``step_gaps``,
  ``store/completeness.py`` and ``store/trends.py`` read ``turn_cycles`` and ``decision_steps``
  only. That is the structural guarantee behind the next paragraph: a journalled step cannot
  make a turn that never committed *look* committed, because nothing that judges completeness
  can see it.

**A journalled turn is not a turn, and must never read as one.** ``ReconstructedTurn`` is a
separate type with no ``TurnCycle`` in it: it cannot be handed to ``trends.turn_metrics``,
``trends.turn_fingerprint`` or anything else that takes a ``TurnCycleRecord``, so "reconstruct the
lost reasoning" and "count this turn in a trend" are not the same capability wearing one name.
When the turn never committed, the number of steps it *would* have had is not recorded anywhere,
and this module says so by name -- ``total_step_count is None`` with a populated
``unresolved_reason`` -- rather than reporting the journalled count as though it were the total.
That is commit ``882758e``'s discipline applied to the store: there, a Lua accessor that had to
skip rows reported ``<field>_rows_without_hash`` and ``<field>_reason = no_local_player`` instead
of returning a short list that reads like a complete one. A silent ``[]``, or a
``total_step_count`` quietly equal to however many entries survived, is a fabricated measurement.

**Every absence here resolves toward "less is known", never toward "it was fine".**
:attr:`JournalledTurnStatus.COMMITTED` requires a positive match against the store's own attempt
listing; no listing, an unreadable listing, or no matching attempt all resolve to something other
than committed. :attr:`~ReconstructedTurn.total_step_count` is ``None``, never ``0``.
:meth:`ReconstructedTurn.describes_a_complete_turn` is ``False`` unless every one of its
conditions is positively established. Nothing in this module resolves an absent fact to ``True``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from civsim_harness.models.common import EventId, RunId, Timestamp, TurnCycleId
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.store.port import DecisionStepBundle, MatchStore

#: The one event type this journal writes and reads. Named as a constant so a caller filtering
#: ``list_run_events`` and the writer below can never drift apart.
STEP_JOURNAL_EVENT_TYPE = RunEventType.DECISION_STEP_COMPLETED

#: ``RunEvent.detail`` keys. The whole ``DecisionStepBundle`` is carried under ``step_bundle`` --
#: the step, its observation, its decision (reasoning included) and its model call -- because
#: Principle III's standard is "reconstruct decisions, yields, and outcomes without replaying the
#: game", and a projection of the bundle would be this module deciding in advance which part of a
#: lost turn somebody will later need.
BUNDLE_KEY = "step_bundle"
TURN_CYCLE_KEY = "turn_cycle_id"
STEP_ID_KEY = "decision_step_id"


def build_step_journal_event(
    *,
    run_id: RunId,
    turn_number: int,
    turn_cycle_id: TurnCycleId,
    bundle: DecisionStepBundle,
    occurred_at: Timestamp,
    event_id: EventId | None = None,
) -> RunEvent:
    """One completed decision step, as a timeline event.

    The event makes **no claim about the turn**. It does not carry "this turn has not committed
    yet", because that would be a durable statement about the future written by something that
    cannot see it: the turn may commit a second later, the event can never be edited (FR-006),
    and the record would then assert something false forever. It states only what was true at the
    moment it was written -- this step completed, here is all of it -- and whether the turn
    carrying it ever committed is resolved at *read* time, against the store, by
    :func:`reconstruct_journalled_turns`.
    """
    return RunEvent(
        event_id=event_id if event_id is not None else EventId(uuid.uuid4().hex),
        run_id=run_id,
        turn_number=turn_number,
        step_index=bundle.step.step_index,
        event_type=STEP_JOURNAL_EVENT_TYPE,
        occurred_at=occurred_at,
        detail={
            TURN_CYCLE_KEY: str(turn_cycle_id),
            STEP_ID_KEY: str(bundle.step.decision_step_id),
            BUNDLE_KEY: bundle.model_dump(mode="json"),
        },
    )


def journal_step(
    store: MatchStore,
    *,
    run_id: RunId,
    turn_number: int,
    turn_cycle_id: TurnCycleId,
    bundle: DecisionStepBundle,
    occurred_at: Timestamp,
) -> EventId:
    """Make *bundle* durable now, synchronously, before the loop takes another step.

    A failed write is **not** swallowed. ``write_run_event`` either returns after the record is
    durable or raises (D2), and this function does not catch it: the run halts on a store that
    cannot record, exactly as FR-013 already requires of the turn write. Swallowing it here would
    reproduce the very defect this module exists for -- a run that keeps playing while its record
    quietly stops accumulating.
    """
    return store.write_run_event(
        build_step_journal_event(
            run_id=run_id,
            turn_number=turn_number,
            turn_cycle_id=turn_cycle_id,
            bundle=bundle,
            occurred_at=occurred_at,
        )
    )


# --------------------------------------------------------------------------
# Reading the journal back
# --------------------------------------------------------------------------


class JournalledTurnStatus(StrEnum):
    """Whether the turn these journalled steps belong to ever reached ``write_turn_cycle``.

    ``UNKNOWN`` is a first-class answer, not a placeholder. A reader that cannot establish
    committed-ness -- a store with no attempt listing, a listing that could not be read, a journal
    entry that does not say which turn it belongs to -- says so, rather than defaulting to either
    of the two answers it has not earned.
    """

    COMMITTED = "committed"
    UNCOMMITTED = "uncommitted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReconstructedTurn:
    """The steps of one turn attempt, recovered from the journal -- **not** a turn record.

    Deliberately not a :class:`~civsim_harness.store.port.TurnCycleRecord` and deliberately
    carrying no :class:`~civsim_harness.models.turn.TurnCycle`: a turn that never committed has no
    outcome, no yields, no ``ended_at`` and no authoritative step count, and manufacturing any of
    them to fit the shape trending consumes is how a gapped record gets averaged in with real
    play (Principle III). Nothing here can be passed to ``store/trends.py``.
    """

    turn_cycle_id: TurnCycleId
    #: ``None`` when no journal entry for this attempt recorded a turn number.
    turn_number: int | None
    status: JournalledTurnStatus
    #: Every step recovered from the journal, ascending by ``step_index``.
    steps: tuple[DecisionStepBundle, ...]
    #: The number of steps the turn record says this attempt had -- available **only** when the
    #: attempt committed. ``None`` otherwise, never ``0`` and never silently ``len(steps)``.
    total_step_count: int | None
    #: Why ``total_step_count`` is ``None``, in words (882758e: name the gap, do not emit a
    #: silent short answer). ``None`` exactly when ``total_step_count`` is known.
    unresolved_reason: str | None
    #: ``step_index`` of every journal entry whose bundle could not be read back, or ``None`` for
    #: an entry that did not even say which step it was. Reported, never dropped.
    unreadable_steps: tuple[int | None, ...] = ()

    def describes_a_complete_turn(self) -> bool:
        """``True`` only when every condition is positively established.

        Absence resolves ``False`` in every clause: an unknown status, an unknown total, a single
        unreadable entry, or a journalled count short of the recorded total all make this
        ``False``. There is no branch here that an unknown fact can satisfy.
        """
        return (
            self.status is JournalledTurnStatus.COMMITTED
            and not self.unreadable_steps
            and self.total_step_count is not None
            and self.total_step_count == len(self.steps)
        )


_NO_ATTEMPT_LISTING = (
    "this store exposes no list_turn_attempts read, so whether attempt {turn_cycle_id} ever "
    "committed cannot be established from here; {journalled} step(s) were journalled"
)
_LISTING_UNREADABLE = (
    "the attempt listing for turn {turn_number} could not be read ({error}), so whether attempt "
    "{turn_cycle_id} ever committed cannot be established; {journalled} step(s) were journalled"
)
_NO_TURN_NUMBER = (
    "no journal entry for attempt {turn_cycle_id} recorded a turn number, so its attempt listing "
    "cannot be looked up; {journalled} step(s) were journalled"
)
_NEVER_COMMITTED = (
    "attempt {turn_cycle_id} has no turn record: it never reached write_turn_cycle, so nothing "
    "states how many steps it had. {journalled} step(s) completed and were journalled before the "
    "run stopped -- that is a floor, not the total"
)


def _resolve_commitment(
    store: MatchStore,
    *,
    run_id: RunId,
    turn_number: int | None,
    turn_cycle_id: TurnCycleId,
    journalled: int,
) -> tuple[JournalledTurnStatus, int | None, str | None]:
    """``(status, total_step_count, unresolved_reason)`` for one journalled attempt.

    Only a positive match -- this exact ``turn_cycle_id`` present in the store's own listing of
    that turn's attempts -- yields ``COMMITTED`` and a total. Every other path yields a status
    that is not ``COMMITTED`` and a named reason.
    """
    if turn_number is None:
        return (
            JournalledTurnStatus.UNKNOWN,
            None,
            _NO_TURN_NUMBER.format(turn_cycle_id=turn_cycle_id, journalled=journalled),
        )

    # Attempt addressing is a deliverable-3 read (`store/contract.py`'s `list_turn_attempts`),
    # not part of the 002 `MatchStore` floor the harness binds to (FR-016). Probed rather than
    # required, so this reader works against the narrow port too -- and says "unknown" there
    # instead of guessing from `get_turn_cycle`, which returns *an* attempt for a turn and
    # cannot tell a superseded attempt's absence from a missing one.
    lister = getattr(store, "list_turn_attempts", None)
    if not callable(lister):
        return (
            JournalledTurnStatus.UNKNOWN,
            None,
            _NO_ATTEMPT_LISTING.format(turn_cycle_id=turn_cycle_id, journalled=journalled),
        )
    try:
        attempts: Sequence[Any] = lister(run_id, turn_number)
    except Exception as exc:  # noqa: BLE001 - a read that fails must not take the journal with it
        return (
            JournalledTurnStatus.UNKNOWN,
            None,
            _LISTING_UNREADABLE.format(
                turn_number=turn_number,
                error=type(exc).__name__,
                turn_cycle_id=turn_cycle_id,
                journalled=journalled,
            ),
        )

    for attempt in attempts:
        if str(getattr(attempt, "turn_cycle_id", "")) == str(turn_cycle_id):
            return JournalledTurnStatus.COMMITTED, int(attempt.step_count), None
    return (
        JournalledTurnStatus.UNCOMMITTED,
        None,
        _NEVER_COMMITTED.format(turn_cycle_id=turn_cycle_id, journalled=journalled),
    )


def reconstruct_journalled_turns(store: MatchStore, run_id: RunId) -> tuple[ReconstructedTurn, ...]:
    """Every turn attempt this run journalled, in the order the journal first saw each attempt.

    A turn killed mid-flight appears here with its completed steps and a ``status`` of
    ``uncommitted``: the reasoning survives, and the record says in the same breath that it is not
    a whole turn.
    """
    events = store.list_run_events(run_id, event_types=[STEP_JOURNAL_EVENT_TYPE])

    order: list[TurnCycleId] = []
    turn_numbers: dict[TurnCycleId, int | None] = {}
    parsed: dict[TurnCycleId, list[DecisionStepBundle]] = {}
    unreadable: dict[TurnCycleId, list[int | None]] = {}

    for event in events:
        detail = event.detail or {}
        raw_cycle = detail.get(TURN_CYCLE_KEY)
        cycle_id = TurnCycleId(str(raw_cycle)) if raw_cycle is not None else TurnCycleId("")
        if cycle_id not in parsed:
            order.append(cycle_id)
            parsed[cycle_id] = []
            unreadable[cycle_id] = []
            turn_numbers[cycle_id] = event.turn_number
        elif turn_numbers[cycle_id] is None:
            turn_numbers[cycle_id] = event.turn_number

        raw_bundle = detail.get(BUNDLE_KEY)
        try:
            parsed[cycle_id].append(DecisionStepBundle.model_validate(raw_bundle))
        except ValidationError:
            # 882758e: an entry that cannot be read back is *named*, not dropped. Dropping it
            # would shorten the recovered turn silently, which is the same fabrication as
            # reporting a partial turn as whole.
            unreadable[cycle_id].append(event.step_index)

    reconstructed: list[ReconstructedTurn] = []
    for cycle_id in order:
        bundles = tuple(sorted(parsed[cycle_id], key=lambda item: item.step.step_index))
        status, total, reason = _resolve_commitment(
            store,
            run_id=run_id,
            turn_number=turn_numbers[cycle_id],
            turn_cycle_id=cycle_id,
            journalled=len(bundles),
        )
        reconstructed.append(
            ReconstructedTurn(
                turn_cycle_id=cycle_id,
                turn_number=turn_numbers[cycle_id],
                status=status,
                steps=bundles,
                total_step_count=total,
                unresolved_reason=reason,
                unreadable_steps=tuple(unreadable[cycle_id]),
            )
        )
    return tuple(reconstructed)


__all__ = [
    "BUNDLE_KEY",
    "STEP_ID_KEY",
    "STEP_JOURNAL_EVENT_TYPE",
    "TURN_CYCLE_KEY",
    "JournalledTurnStatus",
    "ReconstructedTurn",
    "build_step_journal_event",
    "journal_step",
    "reconstruct_journalled_turns",
]
