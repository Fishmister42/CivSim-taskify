"""The turn cycle orchestrator (T114, T117, T152).

Order: **quicksave -> the T110/T112 decision-step loop -> persist -> end turn** (research R14,
data-model.md SS5's turn-attempt state machine). :func:`run_turn_cycle` is this codebase's single
entry point for that sequence, and per T114 it is the *only* place permitted to reach the store's
write-before-advance guard (``store/guard.py``) for a turn -- nothing else in this wave calls
``persist_turn_before_advance``/``advance_turn``/``write_then_advance`` directly.

**Game over (2026-09-21, Principle VII).** Before anything else for this turn -- ahead of the
pre-save prompt probe and well ahead of the quicksave -- :func:`_detect_game_over_before_save`
asks the client whether the game has already ended. MEASURED at 18:50 EDT: Persia was defeated at
game turn 59, the next turn's quicksave could not land because the game was over, and the run
paused with ``SaveVerificationError`` -- a save error standing in for a defeat. A finished game
refuses to save, so that quicksave is not a thing to attempt and recover from; it is a thing that
must not be attempted. When the game is over this module writes the ``game_over_detected`` event
and raises :class:`~civsim_harness.run.game_over.GameOverDetected`, which ``run/runner.py`` turns
into ``playing -> finished`` with a ``victory``/``defeat`` stop resolution. A read that errors is
not a game over: the turn proceeds exactly as it did before the check existed.

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

**What "issue the end-turn action" means here, concretely.** The literal ``Game.EndTurn()``-
equivalent Lua call (``UI.RequestAction(ActionTypes.ACTION_ENDTURN)``, ``catalogs/actions/
turn.yaml``'s ``turn.end_turn``), when the agent's own decision names it, already happened *inside*
the T110 loop -- dispatched and verified through the exact same ``act.dispatch``/``act.verify``
pipeline as every other decision (FR-008: "recorded like any other decision"), which is also why
the loop can end on that decision regardless of whether its own verification reported ``applied``
or ``rejected`` (the loop watches the agent's *decision*, not the outcome of dispatching it -- see
``run/decision_loop.py``) -- with two measured exceptions. An end-turn decision the dispatcher
refused *before* it reached the game (``unavailable_to_human_now``, e.g. a blocking prompt is up)
ended nothing, so the loop treats it as any other refused step and continues (gameplay block 4,
2026-09-21: three "ended_by_agent" turns at the same game turn under an unacknowledged popup).
And an end turn that *was* dispatched but that the game never confirmed within its bound ends this
module's turn all the same -- the liveness half of R14 -- but is recorded ``end_turn_unconfirmed``
with ``game_turn_advanced=False`` rather than ``ended_by_agent`` (gameplay block 7, 2026-09-21:
five cycles all at game turn 35, every one of them recorded as the agent's own ending).
What this module's own "end turn" phase performs, strictly after the
acknowledged store commit, is the *harness's own* seal on the attempt: advancing through
``store.guard`` is what turns "the record is durable" into "the run may now be told this turn is
over." No further Nexus dispatch happens here for the ``ended_by_agent`` or
``end_turn_unconfirmed`` cases, because in both the order already left the harness -- re-issuing
it for the unconfirmed one would be a second end turn against a client that may simply have been
slow.

**``ended_on_no_progress`` does need one, and this module is where it happens.** T113 forbids
synthesising an ``end_turn`` *decision* to represent the backstop -- no step in that attempt ever
calls ``Game.EndTurn()`` as the agent's own choice, and nothing below fabricates a ``Decision``,
``DecisionStep``, or ``ModelCall`` to pretend otherwise. But FR-014 says the backstop *ends the
turn*, and ending it only in this module's own bookkeeping while the live client stays on turn N
forever is a defect, not a scope boundary -- a silent stall dressed up as a normal ending. So,
strictly *after* ``write_then_advance``'s own persist step (never before -- I3 still holds), and
strictly as the harness rather than the agent, :func:`_dispatch_backstop_end_turn` issues
``turn.end_turn`` through the exact same ``act.dispatch``/``act.verify`` path any other action
uses, against a fresh, unattributed observation that is never written anywhere (there is no
``DecisionStep`` for it to belong to). ``UI.RequestAction``'s own return value is ``nil`` and
carries no information, so -- exactly as for the agent's own end-turn decision -- only the
turn-number readback counts as proof; a non-error dispatch is never treated as success. When
``turn.end_turn``'s own ``availability_predicate`` refuses the attempt (most commonly: a blocking
prompt is still up) or the post-dispatch readback does not confirm it, that is a genuine stuck
state -- this module raises :class:`BackstopEndTurnNotConfirmed` rather than looping, retrying, or
fabricating success, propagating uncaught exactly like ``ProviderChainExhausted``/
``UnknownScreenEncountered`` already do below, for ``run/runner.py`` (T116) to turn into a visible,
recorded run failure rather than a silent hang.

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

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civsim_harness.act.dispatch import DispatchStatus, dispatch_action
from civsim_harness.act.verify import verify_execution
from civsim_harness.errors import (
    DiskHeadroomError,
    HarnessError,
    NexusError,
    RecoveryLimitReached,
)
from civsim_harness.host.port import HostPlatform
from civsim_harness.models.common import (
    DecisionStepId,
    DeclarationId,
    EventId,
    LuaContext,
    ObservationId,
    RunId,
    SavePointId,
    Timestamp,
    TurnCycleId,
)
from civsim_harness.models.decision import ExecutionOutcome
from civsim_harness.models.records import RunEvent, RunEventType, SavePoint
from civsim_harness.models.run import Run
from civsim_harness.models.turn import Observation, TurnCycle, TurnOutcome
from civsim_harness.observe.assemble import assemble_observation
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import (
    DecisionLoopContext,
    DecisionLoopResult,
    MidTurnObservationFailure,
    run_decision_loop,
)
from civsim_harness.run.detection import (
    DEFAULT_DETECTION_INTERVAL_S,
    ClientFaultDetected,
    DetectionWatch,
    primary_fault,
    run_under_detection,
)
from civsim_harness.run.game_over import (
    GameOverDetected,
    GameOverReader,
    interpret_game_over,
)
from civsim_harness.saves.headroom import build_disk_headroom_event, check_headroom
from civsim_harness.saves.save_game import SaveCapability
from civsim_harness.saves.save_point import build_save_point, save_name_for, write_save_point
from civsim_harness.saves.verify import SaveVerificationError, verify_save
from civsim_harness.store.completeness import refresh_run_completeness
from civsim_harness.store.guard import TurnPersistedToken, write_then_advance
from civsim_harness.store.port import DecisionStepBundle, MatchStore, TurnCycleRecord
from civsim_harness.telemetry.logging import get_harness_logger, log_event

#: catalogs/actions/turn.yaml's own declaration_id -- the only action this module ever dispatches
#: on the harness's own behalf (T114), and always through the same act.dispatch/act.verify path
#: every other action goes through.
_END_TURN_DECLARATION_ID = DeclarationId("turn.end_turn")

#: How long the no-progress backstop keeps re-reading the game for its end-turn confirmation, and
#: how often (measured: the client confirms asynchronously, after the AI players' turns; see the
#: backstop body). Bounded so an unconfirmed turn still fails closed.
BACKSTOP_CONFIRM_TIMEOUT_S = 45.0
BACKSTOP_CONFIRM_POLL_S = 2.0

#: How many times in a row this turn may be replayed (T152) after an attempt that completed no
#: step at all, before the run stops instead.
#:
#: MEASURED (2026-09-21, the full suite hanging at ~40% on this head): the
#: ``MidTurnObservationFailure`` arm of the replay loop below had no bound of its own, and
#: ``RecoveryEngine`` resets its consecutive-failure count on every recovery that *succeeds* --
#: so a reload that works perfectly against a board the harness still cannot read never reaches
#: the FR-048 bound, and :func:`run_turn_cycle` replays the turn forever without ever raising.
#: The ``ClientFaultDetected`` arm already carries the same backstop in its own shape (one more
#: liveness check after recovery); this is that guard for the arm that has no client-death signal
#: to lean on. Counted only over *consecutive* attempts that completed **no** step: an attempt
#: that got even one step done made real progress and resets it, so an ordinary mid-turn failure
#: is replayed exactly as freely as it always was.
MAX_UNPRODUCTIVE_REPLAYS = 3


def _utcnow() -> Timestamp:
    return datetime.now(UTC)


def _no_yields(_: DecisionLoopResult) -> dict[str, Any]:
    """The default ``compute_yields`` for callers that wire no yield source of their own: an
    honest empty record. Production does not use it -- ``run/composition.py`` passes
    ``run/yields.py``'s ``compute_yields_from_result`` (T258, Constitution III), which reads the
    turn's last ``player.yields`` observation (the human's top bar)."""
    return {}


@dataclass(frozen=True)
class TurnCycleDependencies:
    """Everything :func:`run_turn_cycle` needs for one turn of one run.

    ``build_loop_context`` is called once per attempt (initial and every replay) with a fresh
    ``turn_cycle_id``, so the caller can hand back a :class:`~civsim_harness.run.decision_loop.
    DecisionLoopContext` whose own identity fields are correct for *this* attempt -- everything
    else in it (registry, provider, host, the two injected I/O seams) is expected to stay the
    same across replays of the same turn. When an attempt ends ``ended_on_no_progress``, this
    module calls it one further time with that *same* (already-finished) attempt's
    ``turn_cycle_id`` -- not a new attempt, just borrowing the same registry/observation-reader/
    action-executor collaborators to issue that attempt's own harness-initiated end-turn action
    (see :func:`_dispatch_backstop_end_turn`). A caller's ``build_loop_context`` must stay a pure,
    side-effect-free construction for this to be safe, exactly as its own "everything else...
    expected to stay the same across replays" contract already implies.
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
    attempt_index_base: int = 0
    """T223, FR-004/FR-047: the ``attempt_index`` this turn's **first** attempt is written under.

    Zero for a turn being played for the first time, which is every ordinary forward turn. It is
    non-zero only when this ``(run_id, turn_number)`` already carries attempts on record and is
    being played again -- a rewind (``Runner.resume_from``) or a branch replay, both of which mark
    the earlier attempts non-authoritative but do **not** free their
    ``(run_id, turn_number, attempt_index)`` triples: ``store/sqlite_adapter.py``'s D4 idempotency
    check rejects a second write of a triple whose content differs. Replaying turn N at index 0
    would therefore collide with the very attempt the rewind just superseded, and FR-047's "record
    both the abandoned attempt and the replayed attempt" would be unsatisfiable.

    Resolved by the caller, not here: this module deliberately has no store query of its own for
    it, so that the one place that decides what a replay's index should be
    (``run/composition.py``'s ``build_turn_dependencies``) stays the one place to audit. Replays
    *within* a single :func:`run_turn_cycle` call (T152's mid-turn observation failure) still count
    up from this base, so the two mechanisms compose rather than competing.
    """

    detection: DetectionWatch | None = None
    """T233, FR-044/FR-045/SC-010: this run's binding of research R12's detection signals.

    ``None`` disables detection entirely, which is the right default for a caller driving a
    scripted in-memory game that has no client process and no tuner to probe. The production
    composition root (``run/composition.py``) always supplies one: without it, SC-010 has no
    mechanism behind it and a client that dies is noticed only when the next Nexus call happens
    to fail, if it does. See ``run/detection.py`` for what runs where.
    """

    detection_interval_s: float = DEFAULT_DETECTION_INTERVAL_S
    """How often the in-turn watchdog asks whether the client is still healthy (T233).

    **Not a turn timer** (research R12, FR-014): it bounds how long this module may go without
    *asking*, and is never compared against how long the turn or any step has been running.
    """

    read_game_over: GameOverReader | None = None
    """2026-09-21: the pre-save game-over read (``run/game_over.py``, ``lua/ingame/game_over.lua``).

    ``None`` disables the check entirely, which is the right default for a caller driving a
    scripted in-memory game that has no client to ask -- such a game never ends by victory or
    defeat, and every existing caller of this module keeps the behaviour it had. The production
    composition root (``run/composition.py``) always supplies one: without it, a run whose game
    has already ended discovers that fact only when the FR-007 quicksave the finished game refuses
    fails, and pauses with a save error instead of finishing with the defeat it actually suffered
    (measured on the live client at 18:50 EDT, game turn 59).
    """

    def __post_init__(self) -> None:
        if self.attempt_index_base < 0:
            raise ValueError(
                f"attempt_index_base must be >= 0, got {self.attempt_index_base}"
            )
        if self.detection_interval_s <= 0:
            raise ValueError(
                f"detection_interval_s must be > 0, got {self.detection_interval_s}"
            )


@dataclass(frozen=True)
class TurnCycleOutcome:
    """What :func:`run_turn_cycle` hands back to its caller (``run/runner.py``, T116)."""

    turn_cycle_id: TurnCycleId
    outcome: TurnOutcome
    run: Run
    """The run, updated in place if a mid-turn recovery drove it through
    ``interrupted -> resuming -> playing``; identical to the *run* argument otherwise."""


class BackstopEndTurnNotConfirmed(HarnessError):
    """T113/T114: the no-progress backstop ended this attempt, but this module -- the only one
    permitted to issue the end-turn action to the game (T114) -- could not get the game to confirm
    the turn actually ended, through the same ``act.dispatch``/``act.verify`` path and the same
    turn-number-readback proof every other action uses. ``UI.RequestAction``'s own return value is
    ``nil`` and carries no information, so a non-error dispatch is never treated as success here.

    Raised, never silently retried and never papered over with a fabricated success, for exactly
    two reasons, named in ``detail["stage"]``:

    - ``"dispatch"`` -- ``turn.end_turn``'s own ``availability_predicate`` refused the attempt
      outright. The most common cause is a blocking prompt still being up (FR-010's own ordering:
      the prompt must be answered before the turn can end) -- a genuine stuck state, not a
      transient condition worth spinning on.
    - ``"verify"`` -- the action was authorized and executed, but the turn-number readback
      afterward did not confirm it: the click was swallowed, exactly the ambiguity
      ``turn.end_turn``'s own ``verification_predicate`` exists to catch.

    Propagates uncaught out of :func:`run_turn_cycle`, exactly like this module's other "not mine
    to resolve" conditions (``ProviderChainExhausted``, ``UnknownScreenEncountered``,
    ``RecoveryLimitReached``) -- ``run/runner.py`` (T116) already treats any ``HarnessError`` raised
    from here as a reason to fail the run visibly (a recorded ``lifecycle_transition`` to
    ``failed``, carrying this exception's own message as ``detail["reason"]``), which is exactly
    the "recorded, visible stall" this case needs.

    Raised strictly *after* :func:`~civsim_harness.store.guard.write_then_advance` has already
    durably persisted this attempt's ``TurnCycleRecord`` (``outcome=ended_on_no_progress``): the
    record of what the *agent* did is complete and correct regardless of whether the *harness*
    could then get the game to act on it, so this failure never corrupts or blocks that write --
    it only, and accurately, stops the run from silently believing a turn ended that the live
    client never actually advanced past.
    """


async def _dispatch_backstop_end_turn(
    deps: TurnCycleDependencies, *, turn_cycle_id: TurnCycleId
) -> TurnCycleId:
    """T113/T114: after ``ended_on_no_progress``, issue ``turn.end_turn`` to the game *as the
    harness*, never as a synthesised agent decision.

    Uses ``deps.build_loop_context(turn_cycle_id)`` -- the same collaborators (registry, the two
    injected I/O seams) the just-finished attempt used, the *same* ``turn_cycle_id`` since this is
    that attempt's own harness-initiated ending rather than a new attempt -- and the exact same
    ``act.dispatch``/``act.verify`` path any other action goes through, so the end-turn action is
    still verified rather than assumed (T114). Neither observation this function assembles is
    written anywhere and neither is attributed to any ``DecisionStep`` -- there is no step for
    either of them to belong to, and T113 forbids fabricating one. Raises
    :class:`BackstopEndTurnNotConfirmed` -- never a default, a guess, or a silent retry -- when the
    game does not confirm the turn actually ended.
    """
    ctx = deps.build_loop_context(turn_cycle_id)

    async def _fresh_observation() -> Observation:
        results, screen_identity = await ctx.read_observation_inputs()
        return assemble_observation(
            observation_id=ObservationId(uuid.uuid4().hex),
            decision_step_id=DecisionStepId(uuid.uuid4().hex),
            catalog_version=ctx.catalog_version,
            registry=ctx.registry,
            results=results,
            screen_identity=screen_identity,
            assembled_at=ctx.clock(),
        )

    pre_observation = await _fresh_observation()
    dispatch_outcome = dispatch_action(
        registry=ctx.registry,
        context=LuaContext.IN_GAME,
        action_declaration_id=_END_TURN_DECLARATION_ID,
        observation=pre_observation,
    )
    if dispatch_outcome.status is DispatchStatus.rejected:
        assert dispatch_outcome.rejection_reason is not None
        raise BackstopEndTurnNotConfirmed(
            "the no-progress backstop ended this turn's attempt, but turn.end_turn could not be "
            "dispatched to the game -- a blocking prompt still being up is the most likely cause "
            "(FR-010); this is a genuine stuck state, recorded rather than silently retried",
            detail={
                "run_id": str(deps.run_id),
                "turn_number": deps.turn_number,
                "turn_cycle_id": str(turn_cycle_id),
                "stage": "dispatch",
                "rejection_reason": dispatch_outcome.rejection_reason.value,
                **dispatch_outcome.detail,
            },
        )

    declaration = dispatch_outcome.declaration
    assert declaration is not None
    await ctx.execute_action(declaration.declaration_id, {}, None)
    # MEASURED (2026-09-21, first model-driven run, Linux 1.0.12.9): the game confirms an end
    # turn asynchronously -- `UI.RequestAction(ACTION_ENDTURN)` returns, the AI players take
    # their turns, and only then does `Game.GetCurrentGameTurn()` advance. A single readback
    # taken the instant the dispatch returned saw turn 5 still turn 5 and raised here, while a
    # fresh connection a minute later read turn 6: the turn HAD ended. So the readback is
    # repeated, bounded, until the declared predicate holds -- the same shape `saves/verify.py`
    # gives a quicksave to land. The bound still fails closed: a turn the game never confirms
    # within it is still `BackstopEndTurnNotConfirmed`, never assumed.
    deadline = time.monotonic() + BACKSTOP_CONFIRM_TIMEOUT_S
    while True:
        post_observation = await _fresh_observation()
        verification = verify_execution(
            declaration=declaration,
            pre_observation=pre_observation,
            post_observation=post_observation,
            verified_at=ctx.clock(),
        )
        if verification.execution.outcome is ExecutionOutcome.APPLIED:
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(BACKSTOP_CONFIRM_POLL_S)
    if verification.execution.outcome is not ExecutionOutcome.APPLIED:
        raise BackstopEndTurnNotConfirmed(
            "the no-progress backstop ended this turn's attempt and turn.end_turn was dispatched, "
            "but the turn-number readback afterward did not confirm the game actually ended the "
            "turn -- UI.RequestAction's own return value is nil, so a non-error dispatch is never "
            "treated as success here",
            detail={
                "run_id": str(deps.run_id),
                "turn_number": deps.turn_number,
                "turn_cycle_id": str(turn_cycle_id),
                "stage": "verify",
                **verification.execution.verification,
            },
        )

    return turn_cycle_id


async def _detect_between_turns(deps: TurnCycleDependencies) -> None:
    """T233, FR-044/SC-010: is the client still there *before* this turn commits to anything?

    Liveness only, and deliberately so -- see ``run/detection.py``'s module docstring. It is a
    single point-in-time ``psutil`` call against the client's own PID: no Nexus traffic outside
    the turn's own work, nothing that can block on the client's command lock, and the
    authoritative answer to the question a turn boundary actually asks. A client that hung rather
    than died is caught a moment later by the quicksave command's own per-operation bound.

    Raises :class:`~civsim_harness.run.detection.ClientFaultDetected` rather than recovering: no
    quicksave for this turn exists yet, so there is no attempt to abandon and no start save for
    `RecoveryEngine` to resume from. Recording the crash and naming it is strictly better than
    letting the quicksave a moment later fail with a transport error that says nothing about why.
    """
    if deps.detection is None:
        return
    events = await deps.detection.check_liveness_now(turn_number=deps.turn_number)
    fault = primary_fault(events)
    if fault is None:
        return
    raise ClientFaultDetected(
        "the game client was detected faulty before this turn's quicksave was taken; the turn "
        "never came into existence as an attempt, and there is no start save for this turn to "
        "resume from (FR-044, SC-010)",
        events=events,
        primary=fault,
        detail={"run_id": str(deps.run_id), "turn_number": deps.turn_number},
    )


async def _detect_game_over_before_save(deps: TurnCycleDependencies) -> None:
    """2026-09-21: has the game already ended, *before* this turn's quicksave is attempted?

    MEASURED at 18:50 EDT on the live client: Persia was defeated at game turn 59, the next turn's
    start-of-turn quicksave could not land because the game was over, and the run paused with
    ``SaveVerificationError``. A finished game refuses to save, so the FR-007 quicksave is not a
    thing to attempt-and-recover-from here -- it is a thing that must not be attempted at all. The
    read therefore sits beside the pre-save prompt probe, in the same before-any-quicksave
    position, and runs *first*: if the game is over there is no prompt worth answering and no
    reason to spend a model call on one.

    **A read that errors is not a game over.** Any exception -- a transport failure, a Lua
    accessor this build does not have, a malformed body -- is swallowed here and the turn proceeds
    exactly as it did before this check existed. So is any result
    :func:`~civsim_harness.run.game_over.interpret_game_over` cannot name an outcome from. The
    cost of a missed detection is the save error this function exists to replace; the cost of a
    fabricated one is a run recorded as defeated that was still being played, which is
    categorically worse.

    When the game *is* over: the ``game_over_detected`` event is written first (Principle III --
    the timeline says why the run stopped even if everything after this fails), then
    :class:`~civsim_harness.run.game_over.GameOverDetected` is raised. No quicksave is attempted,
    no ``TurnCycle`` is constructed, and the record carries no gap -- a turn with no quicksave was
    never an attempted turn, so ``store/completeness.py`` never owes it a record.
    """
    reader = deps.read_game_over
    if reader is None:
        return
    try:
        raw = await reader()
    except Exception as exc:  # noqa: BLE001 - see this function's docstring: never a game over
        log_event(
            get_harness_logger(),
            logging.WARNING,
            "run/turn_cycle: the pre-save game-over read failed; the turn proceeds as usual "
            "(a read that errors is not a game over)",
            extra={
                "run_id": str(deps.run_id),
                "turn_number": deps.turn_number,
                "error_type": type(exc).__name__,
            },
        )
        return

    read = interpret_game_over(raw)
    resolution = read.stop_resolution
    if resolution is None:
        return

    deps.store.write_run_event(
        RunEvent(
            event_id=EventId(uuid.uuid4().hex),
            run_id=deps.run_id,
            turn_number=deps.turn_number,
            event_type=RunEventType.GAME_OVER_DETECTED,
            occurred_at=deps.clock(),
            detail={"stop_resolution": resolution.value, **read.as_detail()},
        )
    )
    raise GameOverDetected(
        "the game was already over when this turn tried to begin, so no start-of-turn quicksave "
        "was attempted -- a finished game refuses to save, and the run's honest terminal state is "
        f"a {resolution.value}, recorded as its stop resolution rather than as a save error",
        stop_resolution=resolution,
        read=read,
        detail={
            "run_id": str(deps.run_id),
            "turn_number": deps.turn_number,
            **read.as_detail(),
        },
    )


async def _run_decision_loop_watched(
    deps: TurnCycleDependencies, loop_ctx: DecisionLoopContext
) -> DecisionLoopResult:
    """:func:`~civsim_harness.run.decision_loop.run_decision_loop`, watched on a fixed cadence.

    Identical to calling it directly when no detection is wired (``deps.detection is None``), so
    every caller that drives a scripted in-memory game keeps the behaviour it had. When one *is*
    wired, the loop runs concurrently with the watchdog described in ``run/detection.py`` -- which
    is what makes SC-010's 60 s budget a property of the harness rather than of how long a
    decision step happens to take.
    """
    if deps.detection is None:
        return await run_decision_loop(loop_ctx)
    return await run_under_detection(
        lambda: run_decision_loop(loop_ctx),
        watch=deps.detection,
        turn_number=deps.turn_number,
        interval_s=deps.detection_interval_s,
    )


async def _clear_blocking_prompt_before_save(
    deps: TurnCycleDependencies, *, turn_cycle_id: TurnCycleId
) -> DecisionLoopResult:
    """Answer a blocking prompt through the ordinary decision path *before* this turn's quicksave.

    MEASURED (2026-09-21, gameplay blocks 16 and 17, ``run-…`` paused in 12 s): Gathering
    Storm's eruption cinematic (``prompt.natural_disaster``) makes the game refuse every save
    while it is up. The turn-start quicksave used to be the first thing this module did, before
    any observation, so a save-blocking prompt deadlocked the run before the agent could answer
    it. Principle IV still holds -- the quicksave is taken at the start of the turn, before any
    non-prompt action -- but the *prompt answer* is the one kind of step that may precede it,
    because it is the only way a save can exist at all on such a turn.

    Runs the decision loop in its ``stop_after_prompt_answer`` mode under this attempt's own
    ``turn_cycle_id``: zero steps when no blocking prompt is up (the common case, one fresh
    read), one or more real prompt-answer decisions otherwise, each recorded exactly as a
    mid-turn step would be. The steps are carried into the attempt that follows the save (same
    id, continued numbering), so the record shows the answer where it happened. A prompt that
    will not clear trips the backstop here; the save is still attempted afterwards and, if the
    game still refuses it, the run pauses with the save error recorded -- as it always did.
    """
    loop_ctx = replace(
        deps.build_loop_context(turn_cycle_id), stop_after_prompt_answer=True
    )
    return await _run_decision_loop_watched(deps, loop_ctx)


def _clearance_detail(clearance: DecisionLoopResult | None) -> dict[str, Any]:
    if clearance is None or not clearance.steps:
        return {}
    return {
        "prompt_answered_before_save": {
            "step_count": len(clearance.steps),
            "prompt_types": sorted(
                {
                    str(bundle.decision.prompt_type)
                    for bundle in clearance.steps
                    if bundle.decision.prompt_type is not None
                }
            ),
            "cleared": clearance.outcome is None,
        }
    }


async def _take_quicksave(
    deps: TurnCycleDependencies, *, clearance: DecisionLoopResult | None = None
) -> SavePoint:
    """FR-007, invariant I2: a named, filesystem-verified quicksave before anything else for this
    turn. Raises on any failure -- the turn attempt never comes into existence.

    *clearance* is the pre-save prompt clearance that ran just before (see
    :func:`_clear_blocking_prompt_before_save`); its shape rides on the ``save_taken`` /
    ``save_failed`` event so the timeline says a prompt was answered ahead of this save. If the
    save then fails, the clearance's model calls are written directly first -- they never reach
    a turn record, and a call the agent paid for must not vanish with the attempt (FR-042)."""
    try:
        check_headroom(
            host=deps.host, path=deps.disk_check_path, min_free_disk_gb=deps.min_free_disk_gb
        )
    except DiskHeadroomError as exc:
        # T229, Principle III, data-model.md SS14: "the warning before the halt ... without it
        # the halt looks arbitrary". `check_headroom` is a pure check by design (R17 -- it never
        # deletes and never records), so the event is this module's to write, and it is written
        # *before* the re-raise: the run is about to halt, and a halt whose cause is absent from
        # the turn-by-turn record is exactly the gap that disqualifies a run from trending.
        deps.store.write_run_event(
            build_disk_headroom_event(
                run_id=deps.run_id,
                turn_number=deps.turn_number,
                occurred_at=deps.clock(),
                detail=dict(exc.detail),
            )
        )
        raise

    save_name = save_name_for(deps.run_id, deps.turn_number)
    taken_at = deps.clock()
    try:
        await deps.save_capability.save_game(save_name)
        verified_save = verify_save(deps.host, save_name, home=deps.home)
    except (NexusError, SaveVerificationError) as exc:
        if clearance is not None:
            for bundle in clearance.steps:
                deps.store.write_model_call(bundle.model_call)
        deps.store.write_run_event(
            RunEvent(
                event_id=EventId(uuid.uuid4().hex),
                run_id=deps.run_id,
                turn_number=deps.turn_number,
                event_type=RunEventType.SAVE_FAILED,
                occurred_at=deps.clock(),
                detail={
                    "save_name": save_name,
                    "reason": str(exc),
                    **_clearance_detail(clearance),
                },
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
            detail={
                "save_point_id": save_point.save_point_id,
                "save_name": save_name,
                **_clearance_detail(clearance),
            },
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
    FR-048) -- plus, from this module's own end-turn phase,
    :class:`BackstopEndTurnNotConfirmed` when an ``ended_on_no_progress`` attempt's own end-turn
    dispatch to the game cannot be confirmed. None of these are this module's to resolve;
    ``run/runner.py`` (T116) decides what each means for the run's own lifecycle state.

    Additionally raises :class:`~civsim_harness.run.detection.ClientFaultDetected` (T233, FR-044,
    SC-010) when a detection signal trips **between** turns, before this turn's own quicksave has
    been taken: there is no attempt to abandon and no start save to resume from at that point, so
    the fault is recorded and raised by name rather than surfacing a moment later as a generic
    transport failure from the quicksave. A fault detected *during* the turn is not raised -- it
    is routed into ``deps.recovery`` below, exactly as a mid-turn observation failure is.

    The same two things hold for the **pre-save prompt probe** (2026-09-21, gameplay blocks
    16/17), which runs in that same before-any-quicksave position: a ``ClientFaultDetected`` from
    it is re-raised named as pre-save, and a :class:`~civsim_harness.run.decision_loop.
    MidTurnObservationFailure` from it -- the shape a client death takes when it surfaces as a
    transport failure on the probe's own read rather than as a watchdog signal -- has the
    client's liveness checked once through the ordinary detection path, so it becomes a named,
    recorded ``ClientFaultDetected`` when the client really is gone and is otherwise re-raised
    named as pre-save too. Neither ever replays: there is no start save for this turn yet.

    Also raises :class:`~civsim_harness.run.game_over.GameOverDetected` (2026-09-21, Principle
    VII) when the pre-save game-over read says the game has already ended: the
    ``game_over_detected`` event is written first, no quicksave is attempted, and no ``TurnCycle``
    is constructed -- the turn never comes into existence as an attempt, exactly as for a headroom
    halt, so ``store/completeness.py`` never owes it a record and the run's ledger carries no gap.
    ``run/runner.py`` routes it to ``finished`` with the read's own stop resolution, not to the
    ``paused`` every other unclassified mid-play ``HarnessError`` lands in.

    Finally, :class:`~civsim_harness.errors.RecoveryLimitReached` is raised from this module's
    own replay loop when ``MAX_UNPRODUCTIVE_REPLAYS`` consecutive replays each complete no step
    at all -- the FR-048 bound for the case ``RecoveryEngine``'s own count cannot see, because
    every one of those recoveries *succeeded*.
    """
    await _detect_between_turns(deps)

    # 2026-09-21 (18:50 EDT, game turn 59): is the game already over? Asked *before* the pre-save
    # prompt probe and *before* the quicksave -- a finished game refuses to save, and there is no
    # prompt on an end-game screen worth spending a model call answering. Raises
    # `GameOverDetected` when it is (see _detect_game_over_before_save); returns silently on every
    # other result, including every kind of read failure.
    await _detect_game_over_before_save(deps)

    # 2026-09-21 (gameplay blocks 16/17): a save-blocking prompt is answered *before* the
    # quicksave, under the first attempt's own id, so the steps land in that attempt's record.
    first_turn_cycle_id = TurnCycleId(uuid.uuid4().hex)
    try:
        clearance = await _clear_blocking_prompt_before_save(
            deps, turn_cycle_id=first_turn_cycle_id
        )
    except ClientFaultDetected as exc:
        # Same position as _detect_between_turns: no quicksave exists yet, so there is no
        # attempt to abandon and no start save to resume from -- say so by name rather than
        # letting the mid-turn wording claim a replay that cannot happen.
        raise ClientFaultDetected(
            "the game client was detected faulty during the pre-save prompt probe, before this "
            "turn's quicksave was taken; the turn never came into existence as an attempt, and "
            "there is no start save for this turn to resume from (FR-044, SC-010)",
            events=exc.events,
            primary=exc.primary,
            detail={"run_id": str(deps.run_id), "turn_number": deps.turn_number},
        ) from exc
    except MidTurnObservationFailure as exc:
        # The probe could not read the board at all -- which is the shape a client that died
        # during the probe takes whenever the death surfaces as a transport failure on the read
        # rather than as a watchdog signal first. There is no quicksave yet, so there is no
        # attempt to abandon and nothing to replay from; but the run must not lose what the
        # probe already did or *why* it stopped, and it must not sit here waiting on a dead
        # client either. So: the clearance's own model calls are written directly (FR-042 -- a
        # call the agent paid for must never vanish with an attempt that never came into
        # existence, exactly as `_take_quicksave` does when the save then fails), the events it
        # carried go on the timeline, and the client's liveness is checked once through the
        # ordinary detection path so a death here is *named* a death and recorded as one
        # (FR-044, SC-010) instead of surfacing as a generic assembly failure.
        for bundle in exc.steps:
            deps.store.write_model_call(bundle.model_call)
        for event in exc.events:
            deps.store.write_run_event(event)
        liveness_events = (
            await deps.detection.check_liveness_now(turn_number=deps.turn_number)
            if deps.detection is not None
            else ()
        )
        fault = primary_fault(liveness_events)
        if fault is not None:
            raise ClientFaultDetected(
                "the game client was detected faulty during the pre-save prompt probe, before "
                "this turn's quicksave was taken; the turn never came into existence as an "
                "attempt, and there is no start save for this turn to resume from (FR-044, "
                "SC-010)",
                events=liveness_events,
                primary=fault,
                detail={"run_id": str(deps.run_id), "turn_number": deps.turn_number},
            ) from exc
        raise MidTurnObservationFailure(
            "a fresh observation could not be assembled for the pre-save prompt probe, before "
            "this turn's quicksave was taken; the turn never came into existence as an attempt, "
            "so there is no abandoned attempt to retain and no start save to replay it from",
            cause=exc.cause,
            steps=(),
            events=(),
            no_progress_streak=exc.no_progress_streak,
        ) from exc
    clearance_steps = clearance.steps
    clearance_events = clearance.events

    save_point = await _take_quicksave(deps, clearance=clearance)

    current_run = run
    # T223: the base is 0 for an ordinary forward turn and past the highest attempt already on
    # record when this turn is being replayed, so a rewind's replay never collides with the
    # attempt it superseded (FR-047, and sqlite_adapter's D4 check).
    attempt_index = deps.attempt_index_base
    result: DecisionLoopResult
    first_attempt = True
    #: Consecutive replays that completed no step at all -- see MAX_UNPRODUCTIVE_REPLAYS.
    unproductive_replays = 0
    while True:
        # Replays start from the quicksave, which was taken *after* the prompt was answered, so
        # only the first attempt carries the clearance steps and continues their numbering.
        if first_attempt:
            turn_cycle_id = first_turn_cycle_id
            loop_ctx = replace(
                deps.build_loop_context(turn_cycle_id), step_index_base=len(clearance_steps)
            )
        else:
            turn_cycle_id = TurnCycleId(uuid.uuid4().hex)
            loop_ctx = deps.build_loop_context(turn_cycle_id)
        started_at = deps.clock()
        try:
            result = await _run_decision_loop_watched(deps, loop_ctx)
            if first_attempt and clearance_steps:
                result = replace(
                    result,
                    steps=clearance_steps + result.steps,
                    events=clearance_events + result.events,
                )
        except ClientFaultDetected as exc:
            # T233, FR-044/FR-045/SC-010. Same shape as the mid-turn observation failure below,
            # for the same reason: the board this attempt was reading can no longer be trusted, so
            # the attempt is abandoned and replayed from this turn's own start quicksave rather
            # than continued. Unlike that path, this one carries no completed steps -- the
            # watchdog cancelled the loop mid-await and the loop has no way to hand its partial
            # work back through a cancellation -- which is exactly the "losing at most the turn in
            # progress" SC-010 permits. The detections themselves are already durably recorded
            # (`DetectionWatch` writes each event before reporting it), so the run's timeline
            # still says why this attempt ended.
            recovery_result = await deps.recovery.recover(
                current_run,
                turn_number=deps.turn_number,
                turn_start_save=save_point,
                trigger_event_type=exc.primary,
                trigger_detail=dict(exc.detail),
            )
            current_run = recovery_result.run
            # Replaying into a client that is *still* gone would spin: `RecoveryEngine` resets its
            # consecutive-failure count on every successful recovery, so a load that "succeeds"
            # against a dead client would never reach the FR-048 bound. One more liveness check --
            # the cheap, non-blocking one -- turns that into a loud, recorded stop instead.
            still_faulty = (
                await deps.detection.check_liveness_now(turn_number=deps.turn_number)
                if deps.detection is not None
                else ()
            )
            fault = primary_fault(still_faulty)
            if fault is not None:
                raise ClientFaultDetected(
                    "the game client is still detected faulty after recovery reloaded this "
                    "turn's start quicksave; the run stops rather than replaying the turn into "
                    "a client that is not there (FR-044, FR-048)",
                    events=still_faulty,
                    primary=fault,
                    detail={"run_id": str(deps.run_id), "turn_number": deps.turn_number},
                ) from exc
            attempt_index += 1
            first_attempt = False
            continue
        except MidTurnObservationFailure as exc:
            ended_at = deps.clock()
            abandoned_steps = exc.steps
            abandoned_events = exc.events
            if first_attempt and clearance_steps:
                abandoned_steps = clearance_steps + abandoned_steps
                abandoned_events = clearance_events + abandoned_events
            if abandoned_steps:
                _persist_abandoned_attempt(
                    deps,
                    turn_cycle_id=turn_cycle_id,
                    attempt_index=attempt_index,
                    save_point_id=save_point.save_point_id,
                    steps=abandoned_steps,
                    events=abandoned_events,
                    no_progress_streak=exc.no_progress_streak,
                    started_at=started_at,
                    ended_at=ended_at,
                )
            # The bound this arm was missing (MAX_UNPRODUCTIVE_REPLAYS). Measured against the
            # attempt's *own* steps, not the clearance steps merged into them above: those were
            # taken before the quicksave and are carried forward unchanged by every replay, so
            # counting them would make the first attempt look productive forever.
            if exc.steps:
                unproductive_replays = 0
            else:
                unproductive_replays += 1
                if unproductive_replays >= MAX_UNPRODUCTIVE_REPLAYS:
                    raise RecoveryLimitReached(
                        "this turn was replayed from its start quicksave "
                        f"{unproductive_replays} times in a row without a single decision step "
                        "completing -- the board cannot be read after a reload that itself "
                        "succeeds, so replaying again would spin rather than recover; the run "
                        "stops visibly instead (FR-048)",
                        detail={
                            "run_id": str(deps.run_id),
                            "turn_number": deps.turn_number,
                            "unproductive_replays": unproductive_replays,
                            "max_unproductive_replays": MAX_UNPRODUCTIVE_REPLAYS,
                            "reason": str(exc.cause),
                        },
                    ) from exc
            recovery_result = await deps.recovery.recover_from_observation_assembly_error(
                current_run,
                turn_number=deps.turn_number,
                turn_start_save=save_point,
                error=exc.cause,
            )
            current_run = recovery_result.run
            attempt_index += 1
            first_attempt = False
            continue
        break

    # Only the pre-save clearance mode returns without a turn exit, and the main loop never
    # runs in that mode.
    assert result.outcome is not None

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
        # R14 (revised 2026-09-21, gameplay block 7): whether the *game's* turn advanced is the
        # loop's own finding -- it is the only thing that saw the end turn's verification. None
        # on the backstop exit, where the end turn has not been issued yet (see _end_turn below).
        game_turn_advanced=result.game_turn_advanced,
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

    async def _end_turn(token: TurnPersistedToken) -> TurnCycleId:
        # ended_on_no_progress (T113/T114): nothing inside the loop ever told the game the turn
        # is over -- this module, and only this module, now does, as the harness itself rather
        # than a fabricated agent decision. See _dispatch_backstop_end_turn.
        if result.outcome is TurnOutcome.ENDED_ON_NO_PROGRESS:
            return await _dispatch_backstop_end_turn(deps, turn_cycle_id=token.turn_cycle_id)
        # ended_by_agent *and* end_turn_unconfirmed: the actual Game.EndTurn()-equivalent Lua
        # dispatch already happened inside the loop as the agent's own decision (see module
        # docstring), so this branch is the harness's own write-before-advance seal, nothing
        # more. Re-issuing it for the unconfirmed case would be a second end turn against a
        # client that may simply have been slow -- the record says it was not confirmed, which
        # is the honest answer, and the run keeps its liveness either way (R14, 2026-09-21).
        return token.turn_cycle_id

    # write_then_advance itself stays the plain, synchronous guard defined in store/guard.py --
    # persist_turn_before_advance's write already runs to completion, synchronously, before
    # end_turn(token) is even called; because _end_turn is `async def`, that call only *builds*
    # a coroutine (T114's actual Nexus work has not run yet) which write_then_advance then hands
    # straight back here to await -- so the await below, and everything it runs, still happens
    # strictly after the record is durable (I3), even though store.guard's own signatures never
    # changed to know about asyncio.
    final_turn_cycle_id = await write_then_advance(deps.store, record, _end_turn)

    # T239 (FR-052, Principle III): a turn just became durably persisted -- one of the moments
    # `Run.record_completeness_status` can change -- so the persisted field is re-derived from
    # the record (store/completeness.py's single derivation) rather than left at whatever the
    # run was created with. Strictly after write_then_advance: the derivation must see the turn
    # it is being asked about.
    refresh_run_completeness(deps.store, deps.run_id)

    return TurnCycleOutcome(
        turn_cycle_id=final_turn_cycle_id, outcome=result.outcome, run=current_run
    )
