"""Record-completeness derivation (T158; FR-052, SC-003, SC-011, invariant I11).

``Run.record_completeness_status`` must reflect gaps at *two* grains -- a missing turn
(``MatchStore.turn_gaps``) and a missing step *within* an otherwise-present turn
(``MatchStore.step_gaps``) -- so a turn that is present but internally incomplete (e.g. steps
1, 2, 4 -- step 3 silently missing) is never reported ``complete`` just because the turn number
itself has an authoritative attempt (data-model.md: "``complete`` requires a contiguous
authoritative turn sequence from 1 to the stop turn with no gap markers, **and** a contiguous step
sequence within each of those turns").

**Why this needs more than ``turn_gaps`` alone.** ``MatchStore`` (contracts/match-store-port.md)
has no "highest recorded turn" accessor of its own, and ``turn_gaps`` cannot distinguish "no turns
attempted yet" from "every attempted turn is gap-free" -- both resolve to an empty list (see
``store.sqlite_adapter.turn_gaps``: an empty *present* set of authoritative turn numbers returns
``[]`` exactly like a fully-contiguous one does). ``operator/audit.py`` already solves this the
same way this module does: FR-007 guarantees a quicksave at the start of every turn attempt, so the
distinct turn numbers across ``MatchStore.list_save_points`` are exactly the turns this run has
ever attempted, and their maximum is the range this function checks ``step_gaps`` against.

**A trailing attempted-but-unrecorded turn is ``turn_gaps``'s job, not this function's.**
``store.sqlite_adapter.turn_gaps`` (contracts/match-store-port.md) already accounts for the case
where the *highest attempted* turn (per ``list_save_points``) has a quicksave but no ``TurnCycle``
at all on a run that has *stopped* advancing -- ``paused`` (FR-042, e.g. after chain exhaustion),
``interrupted``, ``resuming``, or either terminal state -- rather than one still actively cycling
through its own turn loop (``playing``, ``waiting_on_model``, ``waiting_on_game``). This function
relies on that: it calls ``turn_gaps`` first and returns ``HAS_GAPS`` immediately if it names
anything, so the trailing case surfaces here for free, with no separate check against
``list_save_points`` needed on top of the one ``turn_gaps`` already does internally. A run still
actively playing the highest attempted turn is deliberately left alone by that same check -- its
quicksave legitimately precedes its ``TurnCycle`` (FR-007), and that is not a gap.

**A branch owes its record from its branch point, not from turn 1 (T239).** A branch replays its
``parent_turn`` from the parent's turn-start save (``run/runner.py``'s ``_begin``); every turn
before that lives in the *parent's* record, reachable through the lineage Principle IV requires
the branch to carry (``parent_run_id``/``parent_turn``, FR-033). ``turn_gaps`` reports turn
numbers from 1 regardless, so an unfiltered derivation would stamp every branch from turn N > 1
``has_gaps`` forever on a perfect record -- a false disqualification in exactly the field
Deliverable 1's trend-eligibility gate consumes. :func:`first_owed_turn` is the one definition of
that floor; :func:`record_completeness_status` and ``operator/audit.py``'s ``audit_completeness``
both apply it, so the rolled-up status and the audit's own gap enumeration cannot disagree about
what a branch owes.

**A turn the game never took is a gap in the record too (R14, revised 2026-09-21).**
``record_completeness_status`` answers "is every turn and every step *present*"; it deliberately
still does, unchanged. But Principle III's other half -- a run whose turn-by-turn record has gaps
must not feed trending -- also covers a run whose record contains game turns that did not advance:
gameplay block 7 (``run-480aa573``) has five consecutive cycles all at game turn 35, each
recorded ``ended_by_agent``, because each end turn was dispatched and then ``verification_failed``
after the bound. :func:`turns_whose_game_turn_did_not_advance` is that rule, and
``store/trends.py``'s ``exclusion_for`` applies it beside ``has_gaps``. It reads both the new
``TurnCycle.game_turn_advanced`` flag *and* the game turn numbers already recorded in consecutive
cycles' observations, so historical records are covered without a migration.

**T239 -- the served and persisted value must be this derivation.** ``Run.record_completeness_
status`` was a constructor constant (fresh runs ``complete``, branches ``unknown``) that nothing
in production ever re-derived. :func:`refresh_run_completeness` is the derive-and-persist half:
production calls it at the moments the answer can change -- a turn persisted
(``run/turn_cycle.py``), attempts superseded (``run/runner.py``'s ``resume_from``,
``saves/branching.py``'s abandonment), and every lifecycle stop the runner records
(``run/runner.py``'s ``_finish``/failure paths) -- and ``run status`` serves a fresh derivation
on every read (``Runner.get_status``), so a run that died mid-flight still reports the gaps it
left behind (Principle III).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from civsim_harness.models.common import DeclarationId, RunId
from civsim_harness.models.run import RecordCompletenessStatus, Run
from civsim_harness.models.turn import Observation
from civsim_harness.store.port import MatchStore

#: The catalog declaration that carries the *game's* own turn counter
#: (``catalogs/observations/game.yaml``: ``turn_number`` is ``Game.GetCurrentGameTurn()``). It is
#: the only place a recorded attempt says which game turn it was played on -- ``TurnCycle.
#: turn_number`` is the *harness's* turn, and the two came apart in gameplay block 7.
GAME_TURN_STATE_DECLARATION_ID = DeclarationId("game.turn_state")


def first_owed_turn(run: Run | None) -> int:
    """The first turn number *run*'s own record owes an authoritative attempt for.

    ``1`` for an ordinary run (and for an unknown/not-yet-persisted one -- the conservative
    floor). For a branch, its recorded ``parent_turn``: the branch replays that turn from the
    parent's save, and everything before it is the parent's record (FR-033, Principle IV). One
    definition, shared by :func:`record_completeness_status` and ``operator/audit.py`` -- see the
    module docstring.
    """
    if run is not None and run.parent_run_id is not None and run.parent_turn is not None:
        return run.parent_turn
    return 1


def game_turn_of(observation: Observation | None) -> int | None:
    """The *game* turn number *observation* recorded, or ``None`` if it recorded none.

    Read from the ``game.turn_state`` entry and nowhere else -- the same declaration
    ``turn.end_turn``'s own verification predicate reads back, so "the record's game turn" and
    "what the harness verified an end turn against" can never be two different numbers.
    """
    if observation is None:
        return None
    for entry in observation.entries:
        if entry.declaration_id != GAME_TURN_STATE_DECLARATION_ID:
            continue
        value = entry.value
        if isinstance(value, Mapping):
            number = value.get("turn_number")
            if isinstance(number, int) and not isinstance(number, bool):
                return number
        return None
    return None


@dataclass(frozen=True)
class CycleGameTurn:
    """One authoritative turn attempt, as the game-turn rule below needs to see it.

    ``turn_number`` is the harness's turn; ``game_turn`` is the game's own counter as that
    attempt's **last recorded observation** saw it (the board the agent was looking at when it
    decided to end the turn -- the post-end-turn read is never recorded as a step's observation);
    ``game_turn_advanced`` is the attempt's own flag, ``None`` on a record written before that
    field existed.
    """

    turn_number: int
    game_turn_advanced: bool | None
    game_turn: int | None


def turns_whose_game_turn_did_not_advance(
    cycles: Sequence[CycleGameTurn],
) -> tuple[int, ...]:
    """The harness turns whose attempt did not move the game's own turn counter (R14, 2026-09-21).

    Two independent signals, deliberately, because neither alone covers the record:

    - **The flag.** ``game_turn_advanced is False`` is the writer's own statement that this
      attempt's end turn was dispatched and never confirmed (``outcome =
      end_turn_unconfirmed``). It is authoritative where present.
    - **The recorded game turn numbers.** A cycle whose game turn equals the previous
      authoritative cycle's played the same game turn twice over. This is what covers records
      written *before* the flag existed, with no migration and no rewrite: gameplay block 7
      (``run-480aa573``) recorded five cycles all at game turn 35, every one of them
      ``ended_by_agent``, and this rule finds them from what is already on disk.

    ``None`` is never read as "yes": an attempt with no flag and no recorded game turn
    contributes nothing here, and the last *known* game turn stays the baseline for the next
    comparison rather than being reset -- a run's authoritative game turns are monotonic, so
    seeing a number twice is a stall however many unrecorded attempts sat between.

    *cycles* must be this run's authoritative attempts in ascending ``turn_number``.
    """
    stalled: list[int] = []
    previous_game_turn: int | None = None
    for cycle in cycles:
        if cycle.game_turn_advanced is False:
            stalled.append(cycle.turn_number)
        elif (
            cycle.game_turn is not None
            and previous_game_turn is not None
            and cycle.game_turn == previous_game_turn
        ):
            stalled.append(cycle.turn_number)
        if cycle.game_turn is not None:
            previous_game_turn = cycle.game_turn
    return tuple(stalled)


def record_completeness_status(store: MatchStore, run_id: RunId) -> RecordCompletenessStatus:
    """Derive ``Run.record_completeness_status`` from ``turn_gaps`` and ``step_gaps`` (T158).

    - :attr:`~civsim_harness.models.run.RecordCompletenessStatus.UNKNOWN` -- this run has not
      produced a single turn attempt yet (no save points at all, per FR-007): there is nothing yet
      to judge complete or incomplete.
    - :attr:`~civsim_harness.models.run.RecordCompletenessStatus.HAS_GAPS` -- ``turn_gaps`` names a
      turn number with no authoritative attempt, **or** ``step_gaps`` names a missing
      ``step_index`` within some attempted turn's authoritative attempt (T145: either alone is
      sufficient, checked independently).
    - :attr:`~civsim_harness.models.run.RecordCompletenessStatus.COMPLETE` -- every turn from 1 to
      the highest attempted turn has an authoritative attempt, and every one of those attempts has
      a contiguous step sequence -- the only case data-model.md's own definition of ``complete``
      permits (SC-003).

    Derived, never asserted (data-model.md): this is the one function a caller (``run/runner.py``,
    ``operator/audit.py``) is meant to call to obtain the value at all, rather than setting it by
    hand from partial information.
    """
    attempted_turns = {save.turn_number for save in store.list_save_points(run_id)}
    if not attempted_turns:
        return RecordCompletenessStatus.UNKNOWN

    # A branch owes its record only from its branch point onward -- see the module docstring
    # (T239). Gaps below that floor are the parent's record, not this run's.
    floor = first_owed_turn(store.get_run(run_id))
    if any(gap >= floor for gap in store.turn_gaps(run_id)):
        return RecordCompletenessStatus.HAS_GAPS

    highest_turn = max(attempted_turns)
    for turn in range(floor, highest_turn + 1):
        if store.step_gaps(run_id, turn):
            return RecordCompletenessStatus.HAS_GAPS

    return RecordCompletenessStatus.COMPLETE


def refresh_run_completeness(store: MatchStore, run_id: RunId) -> RecordCompletenessStatus:
    """Derive ``record_completeness_status`` and persist it onto the stored ``Run`` (T239).

    The persisted field exists for readers on the other side of the port (Deliverable 1's
    trend-eligibility gate chief among them), so it must track the derivation rather than the
    constructor default. Called by production at the moments the answer can change: a turn
    persisted, attempts superseded, and every lifecycle stop the runner records. A run the store
    does not know (a test's hand-built ``PreparedRun`` that was never persisted) is left alone --
    there is no row to update, and inventing one here would not be this function's job.

    Returns the derived status either way, so a caller may serve it directly.
    """
    status = record_completeness_status(store, run_id)
    run = store.get_run(run_id)
    if run is not None and run.record_completeness_status is not status:
        store.update_run(run_id, record_completeness_status=status)
    return status
