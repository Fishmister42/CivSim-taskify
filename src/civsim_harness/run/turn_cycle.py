"""The turn cycle orchestrator (T114, T117, T152).

Order: **quicksave -> the T110/T112 decision-step loop -> persist -> end turn** (research R14,
data-model.md SS5's turn-attempt state machine). :func:`run_turn_cycle` is this codebase's single
entry point for that sequence, and per T114 it is the *only* place permitted to reach the store's
write-before-advance guard (``store/guard.py``) for a turn -- nothing else in this wave calls
``persist_turn_before_advance``/``advance_turn``/``write_then_advance`` directly.

**Quicksave (FR-007, invariant I2).** Before anything else for this turn: check disk headroom
(``saves.headroom.check_headroom``, research R17 -- never deletes anything, only halts), take the
named save through the injected :class:`~civsim_harness.saves.save_game.SaveCapability`, and
verify it on the filesystem (``saves.verify.verify_save``). Any failure here -- headroom, the save
call, or filesystem verification -- raises straight out of :func:`run_turn_cycle` before a single
``TurnCycle`` attempt is constructed: per data-model.md SS5's failure table, "the turn never comes
into existence as an attempt." The caller (``run/runner.py``, T116) is responsible for halting the
run on this raise.

**Persist, then end (FR-013, invariant I3).** The whole finished attempt -- every decision step in
order, each with its observation, decision, execution outcome, and model call, plus the turn's
yields -- is handed to ``store.guard.write_then_advance`` as one ``TurnCycleRecord`` (T117). That
function's own contract is what makes "no turn ends before its complete record is durably
persisted" structural rather than a convention this module has to remember: there is no code path
here, or anywhere else, that can reach an end-turn callable without first having
``write_turn_cycle`` return successfully.

**What "issue the end-turn action" means here, concretely.** The literal ``Game.EndTurn()`` Lua
call, when the agent's own decision names it, already happened *inside* the T110 loop -- dispatched
and verified through the exact same ``act.dispatch``/``act.verify`` pipeline as every other
decision (FR-008: "recorded like any other decision"), which is also why the loop can end on that
decision regardless of whether its own verification reported ``applied`` or ``rejected`` (the loop
watches the agent's *decision*, per research R14's pseudocode "decision was end_turn =>
ended_by_agent", not the outcome of dispatching it -- see ``run/decision_loop.py``). What this
module's own "end turn" phase performs, strictly after the acknowledged store commit, is the
*harness's own* seal on the attempt: advancing through ``store.guard`` is what turns "the record is
durable" into "the run may now be told this turn is over." No further Nexus dispatch happens here
for the ``ended_by_agent`` case, because none is needed.

**The one acknowledged gap, stated plainly rather than hidden.** For ``ended_on_no_progress``, T113
forbids synthesising an ``end_turn`` decision to represent the backstop, so no step in that
attempt ever calls ``Game.EndTurn()``. This module does not invent an unaccounted-for, non-decision
Nexus dispatch to force the game turn forward either -- no task, invariant, or owned test in this
wave specifies that mechanism, and fabricating one unreviewed felt like a worse defect than leaving
it named. The harness's own bookkeeping still closes the attempt correctly (``TurnCycle.outcome =
ended_on_no_progress``, durably persisted, the run proceeds to whatever ``run/runner.py`` does
next); whether the live Civilization VI client's own turn number has actually advanced at that
point is left for the runner/resilience integration to resolve. Flagged explicitly in this wave's
report.

**T152 -- attempt bookkeeping.** A mid-turn observation-assembly failure (T096) is not recoverable
in place: :func:`~civsim_harness.run.decision_loop.run_decision_loop` raises
:class:`~civsim_harness.run.decision_loop.MidTurnObservationFailure`, carrying every step that
*did* complete. This module persists that partial attempt directly (``outcome=abandoned``,
``is_authoritative=False``, via a plain ``store.write_turn_cycle`` call -- never through the
write-before-advance guard, since an abandoned attempt is not an ending) when it produced at least
one step, delegates the actual crash-style recovery (event trail, lifecycle transitions, reloading
the start quicksave) to the already-built ``resilience.recovery.RecoveryEngine``, and starts a
fresh attempt (``attempt_index`` incremented) from the very same turn-start quicksave. Exactly one
attempt per ``(run_id, turn_number)`` is ever written with ``is_authoritative=True`` -- the one this
function returns from -- because this function never writes more than one such record: every
earlier iteration of the loop below wrote its own attempt as non-authoritative before retrying.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civsim_harness.errors import NexusError
from civsim_harness.host.port import HostPlatform
from civsim_harness.models.common import EventId, RunId, SavePointId, Timestamp, TurnCycleId
from civsim_harness.models.records import RunEvent, RunEventType, SavePoint
from civsim_harness.models.run import Run
from civsim_harness.models.turn import TurnCycle, TurnOutcome
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import (
    DecisionLoopContext,
    DecisionLoopResult,
    MidTurnObservationFailure,
    run_decision_loop,
)
from civsim_harness.saves.headroom import check_headroom
from civsim_harness.saves.save_game import SaveCapability
from civsim_harness.saves.save_point import build_save_point, save_name_for, write_save_point
from civsim_harness.saves.verify import SaveVerificationError, verify_save
from civsim_harness.store.guard import TurnPersistedToken, write_then_advance
from civsim_harness.store.port import DecisionStepBundle, MatchStore, TurnCycleRecord


def _utcnow() -> Timestamp:
    return datetime.now(UTC)


def _no_yields(_: DecisionLoopResult) -> dict[str, Any]:
    """The default ``compute_yields``: no per-turn yield derivation exists in this wave -- turning
    game state into "science/gold/culture this turn" is domain logic no catalog entry or task in
    T110-T117's scope names yet. A caller with a real source passes its own callable."""
    return {}


@dataclass(frozen=True)
class TurnCycleDependencies:
    """Everything :func:`run_turn_cycle` needs for one turn of one run.

    ``build_loop_context`` is called once per attempt (initial and every replay) with a fresh
    ``turn_cycle_id``, so the caller can hand back a :class:`~civsim_harness.run.decision_loop.
    DecisionLoopContext` whose own identity fields are correct for *this* attempt -- everything
    else in it (registry, provider, host, the two injected I/O seams) is expected to stay the
    same across replays of the same turn.
    """

    run_id: RunId
    turn_number: int
    store: MatchStore
    save_capability: SaveCapability
    host: HostPlatform
    min_free_disk_gb: float
    disk_check_path: Path
    build_loop_context: Callable[[TurnCycleId], DecisionLoopContext]
    recovery: RecoveryEngine
    compute_yields: Callable[[DecisionLoopResult], dict[str, Any]] = field(default=_no_yields)
    home: Path | None = None
    clock: Callable[[], Timestamp] = field(default=_utcnow)


@dataclass(frozen=True)
class TurnCycleOutcome:
    """What :func:`run_turn_cycle` hands back to its caller (``run/runner.py``, T116)."""

    turn_cycle_id: TurnCycleId
    outcome: TurnOutcome
    run: Run
    """The run, updated in place if a mid-turn recovery drove it through
    ``interrupted -> resuming -> playing``; identical to the *run* argument otherwise."""


async def _take_quicksave(deps: TurnCycleDependencies) -> SavePoint:
    """FR-007, invariant I2: a named, filesystem-verified quicksave before anything else for this
    turn. Raises on any failure -- the turn attempt never comes into existence."""
    check_headroom(
        host=deps.host, path=deps.disk_check_path, min_free_disk_gb=deps.min_free_disk_gb
    )

    save_name = save_name_for(deps.run_id, deps.turn_number)
    taken_at = deps.clock()
    try:
        await deps.save_capability.save_game(save_name)
        verified_save = verify_save(deps.host, save_name, home=deps.home)
    except (NexusError, SaveVerificationError) as exc:
        deps.store.write_run_event(
            RunEvent(
                event_id=EventId(uuid.uuid4().hex),
                run_id=deps.run_id,
                turn_number=deps.turn_number,
                event_type=RunEventType.SAVE_FAILED,
                occurred_at=deps.clock(),
                detail={"save_name": save_name, "reason": str(exc)},
            )
        )
        raise

    save_point = build_save_point(
        run_id=deps.run_id,
        turn_number=deps.turn_number,
        save_name=save_name,
        verified_save=verified_save,
        taken_at=taken_at,
    )
    write_save_point(deps.store, save_point)
    deps.store.write_run_event(
        RunEvent(
            event_id=EventId(uuid.uuid4().hex),
            run_id=deps.run_id,
            turn_number=deps.turn_number,
            event_type=RunEventType.SAVE_TAKEN,
            occurred_at=deps.clock(),
            detail={"save_point_id": save_point.save_point_id, "save_name": save_name},
        )
    )
    return save_point


def _persist_abandoned_attempt(
    deps: TurnCycleDependencies,
    *,
    turn_cycle_id: TurnCycleId,
    attempt_index: int,
    save_point_id: SavePointId,
    steps: tuple[DecisionStepBundle, ...],
    events: tuple[RunEvent, ...],
    no_progress_streak: int,
    started_at: Timestamp,
    ended_at: Timestamp,
) -> None:
    """T152: retain the abandoned attempt with every step it completed, never authoritative.

    Written directly through ``store.write_turn_cycle`` -- deliberately **not** through
    ``store.guard``'s write-before-advance functions, since abandoning an attempt is not an ending
    and must never be able to invoke an end-turn callable.
    """
    turn_cycle = TurnCycle(
        turn_cycle_id=turn_cycle_id,
        run_id=deps.run_id,
        turn_number=deps.turn_number,
        attempt_index=attempt_index,
        is_authoritative=False,
        save_point_id=save_point_id,
        step_count=len(steps),
        outcome=TurnOutcome.ABANDONED,
        final_no_progress_streak=no_progress_streak,
        visually_degraded=any(bundle.step.visually_degraded for bundle in steps),
        yields={},
        started_at=started_at,
        ended_at=ended_at,
        persisted_at=deps.clock(),
    )
    record = TurnCycleRecord(turn_cycle=turn_cycle, steps=list(steps))
    deps.store.write_turn_cycle(record)
    for event in events:
        deps.store.write_run_event(event)


async def run_turn_cycle(deps: TurnCycleDependencies, *, run: Run) -> TurnCycleOutcome:
    """Run one turn of one run to completion (T114, T117): quicksave, the T110/T112 decision-step
    loop (replayed per T152 on a mid-turn observation failure), persist, end turn.

    *run* must reflect the run's currently persisted state (expected ``playing``) -- passed
    through to :class:`~civsim_harness.resilience.recovery.RecoveryEngine` unchanged if no replay
    is needed, or updated across the recovery-driven ``interrupted -> resuming -> playing`` cycle
    if one is.

    Propagates, uncaught, whatever :func:`~civsim_harness.run.decision_loop.run_decision_loop`
    itself does not resolve: quicksave/headroom failures (FR-007), a persistence failure from
    ``store.write_turn_cycle`` (FR-013 -- the run must halt, never advance), a model-chain
    exhaustion (``ProviderChainExhausted``, FR-042), an unrecognised screen
    (``UnknownScreenEncountered``, FR-049), and a recovery bound exceeded (``RecoveryLimitReached``,
    FR-048). None of these are this module's to resolve; ``run/runner.py`` (T116) decides what each
    means for the run's own lifecycle state.
    """
    save_point = await _take_quicksave(deps)

    current_run = run
    attempt_index = 0
    result: DecisionLoopResult
    while True:
        turn_cycle_id = TurnCycleId(uuid.uuid4().hex)
        loop_ctx = deps.build_loop_context(turn_cycle_id)
        started_at = deps.clock()
        try:
            result = await run_decision_loop(loop_ctx)
        except MidTurnObservationFailure as exc:
            ended_at = deps.clock()
            if exc.steps:
                _persist_abandoned_attempt(
                    deps,
                    turn_cycle_id=turn_cycle_id,
                    attempt_index=attempt_index,
                    save_point_id=save_point.save_point_id,
                    steps=exc.steps,
                    events=exc.events,
                    no_progress_streak=exc.no_progress_streak,
                    started_at=started_at,
                    ended_at=ended_at,
                )
            recovery_result = await deps.recovery.recover_from_observation_assembly_error(
                current_run,
                turn_number=deps.turn_number,
                turn_start_save=save_point,
                error=exc.cause,
            )
            current_run = recovery_result.run
            attempt_index += 1
            continue
        break

    ended_at = deps.clock()
    turn_cycle = TurnCycle(
        turn_cycle_id=turn_cycle_id,
        run_id=deps.run_id,
        turn_number=deps.turn_number,
        attempt_index=attempt_index,
        is_authoritative=True,
        save_point_id=save_point.save_point_id,
        step_count=len(result.steps),
        outcome=result.outcome,
        final_no_progress_streak=result.final_no_progress_streak,
        visually_degraded=any(bundle.step.visually_degraded for bundle in result.steps),
        yields=deps.compute_yields(result),
        started_at=started_at,
        ended_at=ended_at,
        persisted_at=deps.clock(),
    )
    record = TurnCycleRecord(turn_cycle=turn_cycle, steps=list(result.steps))

    # Ancillary timeline events (image_withheld/capture_failed per step, and
    # turn_ended_on_no_progress if this attempt tripped the backstop) are written ahead of the
    # atomic turn record so the timeline never shows an "ended" turn whose own supporting events
    # are still missing.
    for event in result.events:
        deps.store.write_run_event(event)

    def _end_turn(token: TurnPersistedToken) -> TurnCycleId:
        # The actual Game.EndTurn() Lua dispatch, when this attempt ended_by_agent, already
        # happened inside the loop as the agent's own decision (see module docstring) -- this
        # closure is the harness's own write-before-advance seal, nothing more.
        return token.turn_cycle_id

    final_turn_cycle_id = write_then_advance(deps.store, record, _end_turn)

    return TurnCycleOutcome(
        turn_cycle_id=final_turn_cycle_id, outcome=result.outcome, run=current_run
    )
