"""The run orchestrator (T116).

Drives a run's lifecycle from ``preparing`` to ``playing`` and across every era to its stop
condition (FR-003, FR-009, SC-002), recording every lifecycle transition
(``run.lifecycle.transition``) and resolving the stop condition (``run.stop.evaluate_stop``) after
every turn. Implements :class:`~civsim_harness.operator.runner_protocol.RunnerProtocol` verbatim --
``src/civsim_harness/operator/runner_protocol.py`` now exists (built by the operator-surface wave)
and this module is written *against* it, not around a guess at its shape.

**Synchronous control surface over an asyncio play loop.** ``turn_cycle.run_turn_cycle`` and
everything under it is ``async`` (Nexus is an asyncio client), but ``RunnerProtocol``'s methods are
all plain, synchronous, thread-safe calls -- exactly the seam its own docstring names: "a runner
that owns its own event loop (in a background thread) and exposes a synchronous, thread-safe
control interface." :class:`Runner` does exactly that: one background thread owns one ``asyncio``
event loop for the lifetime of this instance; every synchronous method below schedules work onto it
via ``asyncio.run_coroutine_threadsafe``.

**What this module does *not* wire up itself.** Building a live ``DecisionLoopContext`` for a given
turn needs a connected Nexus client, a resolved capability registry, a model provider, a host
adapter, and the two decision-loop I/O seams (``ObservationReader``/``ActionExecutor``) -- real
implementations of all of which are other waves' modules (some, like the model-provider chain and a
production Nexus-backed observation reader, do not exist as reusable constructors anywhere in this
codebase yet). Rather than hand-wiring a guess at that composition here, :class:`Runner` takes it as
one injected seam, :attr:`RunnerDependencies.prepare_run`: *given* a loaded
:class:`~civsim_harness.models.config.RunConfiguration`, it performs whatever preflight the
composition root wants (build pin, host tier, catalog/model-chain preflight -- each already exists
as its own module elsewhere in this codebase) and returns a :class:`PreparedRun` carrying the
resulting ``Run`` and the configured ``StopCondition``, plus everything
:attr:`RunnerDependencies.build_turn_dependencies` and
:attr:`RunnerDependencies.evaluate_stop_facts` need to do their own per-turn jobs. This module's
own responsibility is exactly what T116 names: lifecycle sequencing, stop resolution, and
recording what actually happened -- not re-implementing preflight modules other tasks already
built.

**FR-009 -- the game auto-advancing a turn.** :meth:`Runner._play_run` never assumes it drove the
game to exactly the turn number it expected: every iteration re-reads ``outcome.run`` and asks
``evaluate_stop_facts`` what actually happened *this* turn (has the game itself resolved to a
victory or defeat, was an operator stop requested, did an unrecoverable failure occur) rather than
advancing a counter of its own and trusting it. The record reflects what happened, not what was
planned.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civsim_harness.config.run_config import load_run_configuration_file
from civsim_harness.errors import HarnessError, ProviderChainExhausted, RecoveryLimitReached
from civsim_harness.models.common import EventId, RunId, Timestamp
from civsim_harness.models.config import RunConfiguration, StopCondition
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.run import LifecycleState, Run, StopResolution
from civsim_harness.operator.runner_protocol import RunnerProtocol
from civsim_harness.operator.schemas import (
    ConnectionHealth,
    LastError,
    LastKnownGoodSave,
    RunStatusView,
)
from civsim_harness.run.decision_loop import UnknownScreenEncountered
from civsim_harness.run.lifecycle import TERMINAL_STATES, transition
from civsim_harness.run.stop import StopEvaluation, evaluate_stop
from civsim_harness.run.turn_cycle import TurnCycleDependencies, run_turn_cycle
from civsim_harness.saves.addressing import SaveAddressingError
from civsim_harness.store.port import MatchStore
from civsim_harness.telemetry.logging import get_harness_logger, log_event


def _utcnow() -> Timestamp:
    return datetime.now(UTC)


async def _await_on_this_loop(awaitable: Awaitable[PreparedRun]) -> PreparedRun:
    """Wrap any ``Awaitable[PreparedRun]`` as a plain ``Coroutine`` -- what
    ``asyncio.run_coroutine_threadsafe`` requires -- regardless of what kind of awaitable a
    ``RunnerDependencies.prepare_run`` implementation happened to hand back. See
    :meth:`Runner._resolve_prepared_run`.
    """
    return await awaitable


@dataclass(frozen=True)
class PreparedRun:
    """What preflight (``RunnerDependencies.prepare_run``) hands back to the runner: the ``Run``
    record as it exists once preparation finished (either ``playing`` or already ``failed`` at
    preflight, per contracts/operator-surface.md's ``start`` error table) and the run's configured
    ``StopCondition`` (data-model.md SS2) -- everything :class:`Runner` itself needs to read.
    Per-turn collaborators (Nexus connection, provider, host adapter, ...) are the composition
    root's own closures, captured by ``RunnerDependencies.build_turn_dependencies`` rather than
    carried on this type.
    """

    run: Run
    stop_condition: StopCondition


class RunPreparationFailed(HarnessError):
    """Raised by an injected ``prepare_run`` when *no* run could be created at all -- an invalid
    configuration, "rejected; nothing prepared" (contracts/operator-surface.md, D2 in
    ``operator/runner_protocol.py``'s own docstring). Distinct from a preflight mismatch that still
    creates a run in ``failed`` state: that case returns a :class:`PreparedRun` normally, with
    ``run.lifecycle_state == LifecycleState.FAILED``, rather than raising."""


@dataclass(frozen=True)
class RunnerDependencies:
    """Everything :class:`Runner` needs, supplied once by the composition root.

    ``prepare_run`` performs the whole preflight pipeline for one configuration -- build pin, host
    capability gate, catalog/model-chain preflight, applying and verifying the configured setup --
    and returns the resulting :class:`PreparedRun`. It may raise :class:`RunPreparationFailed` for
    "invalid configuration, nothing prepared"; any other :class:`~civsim_harness.errors.
    HarnessError` it raises is treated as a preparation failure the same way.

    ``prepare_run`` may be a plain synchronous callable (every test in this codebase before T209
    passes one) **or** a coroutine function. The composition root's own real implementation
    (:mod:`civsim_harness.run.composition`) needs the latter: connecting its ``NexusClient`` is
    inherently ``async``, and -- critically -- it must run on *this runner's own* background event
    loop (:attr:`Runner._loop`), the same loop every later per-turn Nexus call runs on, never a
    throwaway loop of its own (an ``asyncio`` socket/lock binds to whichever loop first awaits it;
    connecting on a different one than the loop that later plays turns would break the first real
    call). :meth:`Runner._resolve_prepared_run` is where that bridging happens -- see its own
    docstring -- so this field's declared type accepts either shape without any caller needing to
    know which one a given composition supplies.

    ``build_turn_dependencies`` builds one turn's :class:`~civsim_harness.run.turn_cycle.
    TurnCycleDependencies` given the prepared run and the turn number about to be played.
    ``evaluate_stop_facts`` reports what :func:`~civsim_harness.run.stop.evaluate_stop` needs to
    know after that turn finished -- whether the game itself resolved to a victory or defeat, an
    operator stop was requested, or an unrecoverable failure occurred.
    """

    store: MatchStore
    prepare_run: Callable[[RunConfiguration], PreparedRun | Awaitable[PreparedRun]]
    build_turn_dependencies: Callable[[PreparedRun, int], TurnCycleDependencies]
    evaluate_stop_facts: Callable[[PreparedRun, int], StopEvaluation]
    connection_health: Callable[[], ConnectionHealth] = field(
        default=lambda: ConnectionHealth(tuner="unknown", client="unknown", store="unknown")
    )
    disk_headroom_gb: Callable[[], float] = field(default=lambda: 0.0)
    clock: Callable[[], Timestamp] = field(default=_utcnow)


@dataclass
class _RunState:
    """Mutable, lock-protected bookkeeping for one run the runner is driving."""

    run: Run
    prepared: PreparedRun
    current_turn: int | None = None
    current_step: int | None = None
    pause_requested: bool = False
    stop_requested: bool = False
    last_error: tuple[str, Timestamp] | None = None


def _coincident_event(
    run_id: RunId, turn_number: int, resolution: StopResolution, occurred_at: Timestamp
) -> RunEvent:
    """One coincident-but-not-recorded stop condition, as a plain timeline event (spec edge case:
    "the run reaches its stop condition on the same turn as a victory, defeat, or crash -- exactly
    one stop condition is recorded, with the others present as events", invariant I10)."""
    return RunEvent(
        event_id=EventId(uuid.uuid4().hex),
        run_id=run_id,
        turn_number=turn_number,
        event_type=RunEventType.LIFECYCLE_TRANSITION,
        occurred_at=occurred_at,
        detail={"coincident_stop_resolution": resolution.value},
    )


class Runner(RunnerProtocol):
    """Implements :class:`~civsim_harness.operator.runner_protocol.RunnerProtocol` (T116).

    One instance drives as many runs as are started against it, each on the same background
    ``asyncio`` event loop (Nexus is single-connection per client anyway -- research R4 -- so there
    is no concurrency benefit to a loop per run, only bookkeeping cost). The loop is started in
    ``__init__`` and lives for the lifetime of this instance.
    """

    def __init__(self, deps: RunnerDependencies) -> None:
        self._deps = deps
        self._lock = threading.Lock()
        self._runs: dict[RunId, _RunState] = {}

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="civsim-runner-loop", daemon=True
        )
        self._thread.start()

    # -- RunnerProtocol -----------------------------------------------------

    def start(self, config_path: Path) -> RunId:
        config = load_run_configuration_file(config_path)
        try:
            prepared = self._resolve_prepared_run(config)
        except RunPreparationFailed:
            raise
        except HarnessError as exc:
            raise RunPreparationFailed(
                "run preparation failed; no run was created", detail={"reason": str(exc)}
            ) from exc

        run_id = prepared.run.run_id
        with self._lock:
            self._runs[run_id] = _RunState(run=prepared.run, prepared=prepared)

        if prepared.run.lifecycle_state is LifecycleState.PLAYING:
            self._schedule(self._play_run(run_id))
        # A run that preflight already landed in `failed` (contracts/operator-surface.md D2, T101's
        # host-tier gate, T072's build pin, etc.) is returned as-is: nothing to play, and the caller
        # discovers the failure through get_status, exactly as RunnerProtocol's docstring specifies.
        return run_id

    def _resolve_prepared_run(self, config: RunConfiguration) -> PreparedRun:
        """Call ``self._deps.prepare_run(config)`` and, if it returns something awaitable rather
        than a plain :class:`PreparedRun`, run it to completion on *this runner's own* background
        event loop (:attr:`self._loop`) -- never a throwaway loop of this method's own (T210).

        **Why this matters.** :class:`~civsim_harness.run.composition`'s real ``prepare_run``
        connects a ``NexusClient`` whose ``asyncio.Lock`` and socket-backed stream reader/writer
        bind to whichever event loop first awaits them (``nexus/client.py``'s own module
        docstring). That same connected client is what every later per-turn Nexus call
        (``build_turn_dependencies`` -> ``TurnCycleDependencies`` -> ``run_turn_cycle`` ->
        ``run_decision_loop``) dispatches through -- and those calls are always awaited from
        *this* runner's own coroutine (:meth:`_play_run`, scheduled onto ``self._loop`` via
        :meth:`_schedule`). If ``prepare_run`` connected on a *different* loop (e.g. one created
        and immediately closed by a bare ``asyncio.run(...)``), the very first per-turn call would
        fail cross-loop -- or, once the throwaway loop is closed, simply be unable to perform I/O
        at all. Scheduling the *same* coroutine object onto ``self._loop`` instead -- rather than
        creating it there in the first place -- is safe: constructing a coroutine object (calling
        an ``async def`` function) needs no running loop of its own, only *awaiting* it does.

        Calling ``self._deps.prepare_run(config)`` happens on *this* thread (whichever thread
        called :meth:`start`), exactly as it always did -- only the awaiting, when there is
        anything to await, moves onto ``self._loop``. A plain synchronous ``prepare_run`` (every
        pre-T209 test in this codebase passes one) never produces anything awaitable, so it takes
        the fast path below unchanged: this method is a strict superset of the old
        ``self._deps.prepare_run(config)`` call it replaces, not a behaviour change for any
        existing caller.
        """
        result = self._deps.prepare_run(config)
        if inspect.isawaitable(result):
            # `run_coroutine_threadsafe` wants a `Coroutine` specifically, not any `Awaitable` --
            # `_await_on_this_loop` below is a trivial wrapper that makes that true regardless of
            # what kind of awaitable `result` actually is, at the cost of one extra `await` layer.
            return asyncio.run_coroutine_threadsafe(
                _await_on_this_loop(result), self._loop
            ).result()
        return result

    def request_pause(self, run_id: RunId) -> None:
        state = self._require_state(run_id)
        with self._lock:
            if state.run.lifecycle_state is not LifecycleState.PLAYING:
                raise HarnessError(
                    "pause is only a legal request while the run is playing",
                    detail={"run_id": run_id, "lifecycle_state": state.run.lifecycle_state.value},
                )
            state.pause_requested = True

    def request_resume(self, run_id: RunId) -> None:
        state = self._require_state(run_id)
        with self._lock:
            if state.run.lifecycle_state is LifecycleState.PAUSED:
                state.run, _event = self._advance(state, LifecycleState.PLAYING)
                state.pause_requested = False
                self._schedule(self._play_run(run_id))
                return
            if state.run.lifecycle_state is LifecycleState.PLAYING and state.pause_requested:
                # The pause request had not yet landed at a turn boundary -- withdrawing it is a
                # legal no-op rather than an error (FR-004, FR-008: a turn is never cut short).
                state.pause_requested = False
                return
            current_state = state.run.lifecycle_state
        raise HarnessError(
            "resume is only a legal request for a paused run",
            detail={"run_id": run_id, "lifecycle_state": current_state.value},
        )

    def request_stop(self, run_id: RunId) -> None:
        state = self._require_state(run_id)
        with self._lock:
            if state.run.lifecycle_state in (LifecycleState.FINISHED, LifecycleState.FAILED):
                raise HarnessError(
                    "a run already in a terminal lifecycle state cannot be stopped",
                    detail={"run_id": run_id, "lifecycle_state": state.run.lifecycle_state.value},
                )
            state.stop_requested = True

    def get_status(self, run_id: RunId) -> RunStatusView:
        state = self._require_state(run_id)
        with self._lock:
            requested_state: LifecycleState | None = None
            if state.pause_requested:
                requested_state = LifecycleState.PAUSED
            elif state.stop_requested:
                requested_state = LifecycleState.FINISHED

            last_error = (
                LastError(type=state.last_error[0], at=state.last_error[1])
                if state.last_error is not None
                else None
            )

            good_save = self._deps.store.get_last_known_good(run_id)
            last_known_good = (
                LastKnownGoodSave(turn=good_save.turn_number, save_point_id=good_save.save_point_id)
                if good_save is not None
                else None
            )

            return RunStatusView(
                run_id=run_id,
                lifecycle_state=state.run.lifecycle_state,
                requested_state=requested_state,
                current_turn=state.current_turn,
                current_step=state.current_step,
                last_known_good_save=last_known_good,
                last_error=last_error,
                connection_health=self._deps.connection_health(),
                record_completeness_status=state.run.record_completeness_status,
                comparability_status=state.run.comparability_status,
                archived=state.run.archived_at is not None,
                disk_headroom_gb=self._deps.disk_headroom_gb(),
            )

    def branch(self, *, parent_run_id: RunId, turn: int, config_path: Path) -> RunId:
        """``RunnerProtocol``'s T173 addendum (branch-and-replay, US4) -- not part of this wave's
        assignment (T110-T117, T152) and not implemented here. Raises rather than silently doing
        nothing or pretending to branch; a future wave (T173/T199) replaces this with the real
        branch orchestration (resolving the parent's save, catalog/build/host-tier agreement, and
        starting a new run with that lineage)."""
        raise HarnessError(
            "Runner.branch is not implemented in this wave (T173/US4 branch-and-replay is a "
            "later, separate task from T110-T117/T152)",
            detail={"parent_run_id": parent_run_id, "turn": turn},
        )

    def resume_from(self, run_id: RunId, turn: int) -> None:
        """``RunnerProtocol``'s T173 addendum -- likewise not implemented here; see
        :meth:`branch`."""
        raise HarnessError(
            "Runner.resume_from is not implemented in this wave (T173/US4 is a later, separate "
            "task from T110-T117/T152)",
            detail={"run_id": run_id, "turn": turn},
        )

    # -- the play loop --------------------------------------------------------

    async def _play_run(self, run_id: RunId) -> None:
        """Drive *run_id* across every era to its stop condition (FR-003, FR-009, SC-002).

        Each iteration plays exactly one turn (``turn_cycle.run_turn_cycle``) and then resolves the
        stop condition against what actually happened (``run.stop.evaluate_stop``) -- never against
        what was planned, per FR-009's "the game auto-advancing a turn... recording what actually
        happened". A pause or stop request lands here, at the turn boundary, never mid-turn
        (FR-004, FR-008, SC-022).

        **Every exception path below is deliberately made loud.** This coroutine is always
        scheduled fire-and-forget (``_schedule``, ``asyncio.run_coroutine_threadsafe``) -- nothing
        awaits its ``Future`` or inspects ``Future.exception()`` -- so an exception that escapes
        this method uncaught is silently discarded and the run is stranded exactly as it was the
        moment this coroutine stopped running: stuck in ``playing`` forever, recorded nowhere,
        with nothing surfacing that anything went wrong. The outer ``except Exception`` exists only
        to catch a failure *inside* ``_handle_run_failure`` itself (most plausibly ``transition()``
        raising a second time, because the run reached a state its routing did not anticipate) --
        it still re-raises after recording what it can, because a truly unexpected failure
        surfacing loudly is strictly better than it vanishing here a second time.
        """
        state = self._require_state(run_id)
        try:
            try:
                while True:
                    with self._lock:
                        if state.stop_requested:
                            self._finish(state, resolution=StopResolution.OPERATOR_STOP)
                            return
                        if state.pause_requested:
                            state.run, _event = self._advance(state, LifecycleState.PAUSED)
                            return

                        turn_number = (state.current_turn or 0) + 1
                        state.current_turn = turn_number
                        state.current_step = None
                        turn_deps = self._deps.build_turn_dependencies(state.prepared, turn_number)

                    outcome = await run_turn_cycle(turn_deps, run=state.run)

                    with self._lock:
                        state.run = outcome.run
                        facts = self._deps.evaluate_stop_facts(state.prepared, turn_number)
                        decision = evaluate_stop(state.prepared.stop_condition, facts)
                        for coincident in decision.coincident:
                            self._deps.store.write_run_event(
                                _coincident_event(
                                    run_id, turn_number, coincident, self._deps.clock()
                                )
                            )
                        if decision.resolution is not None:
                            self._finish(state, resolution=decision.resolution)
                            return
            except HarnessError as exc:
                self._handle_run_failure(state, exc)
        except Exception as exc:
            self._record_unexpected_failure(state, exc)
            raise

    def _finish(self, state: _RunState, *, resolution: StopResolution) -> None:
        """Caller holds ``self._lock``. Transition to ``finished`` with exactly one recorded stop
        resolution (FR-005, invariant I10)."""
        state.run, event = transition(
            state.run,
            LifecycleState.FINISHED,
            occurred_at=self._deps.clock(),
            turn_number=state.current_turn,
            stop_resolution=resolution,
        )
        self._deps.store.write_run_event(event)
        self._deps.store.update_run(
            state.run.run_id,
            lifecycle_state=state.run.lifecycle_state,
            ended_at=state.run.ended_at,
            stop_resolution=state.run.stop_resolution,
        )

    def _handle_run_failure(self, state: _RunState, exc: HarnessError) -> None:
        """Route one ``HarnessError`` raised out of a turn's play loop to the lifecycle state
        data-model.md SS4 actually permits for its kind (FR-003), and unconditionally record
        ``state.last_error`` first so ``get_status`` reflects that something went wrong even if
        everything below it fails too.

        - ``RecoveryLimitReached``/``SaveAddressingError``: ``resilience.recovery.RecoveryEngine``
          has already legally transitioned the run all the way to ``failed``
          (``resuming -> failed``, identifying the last-known-good save, FR-048/FR-036) and
          persisted it *before* raising -- this runner's own ``state.run`` is stale here (it is
          only ever updated when ``run_turn_cycle`` *returns*, which never happens on this path),
          so the fix is to resync from the store, never to call ``transition()`` again (that would
          attempt an illegal ``failed -> failed`` no-op and raise a second time).
        - ``ProviderChainExhausted`` (FR-042: "pause the run in a recorded state") and
          ``UnknownScreenEncountered`` (FR-049: "stall the run visibly") both land in ``paused`` --
          legal from ``playing``, unlike ``failed``, and exactly the recorded-but-not-terminal
          character both FRs describe. ``UnknownScreenEncountered`` additionally carries an
          ``unknown_screen`` ``RunEvent`` (``act/prompts.py``'s ``route_prompt``) that nothing else
          in this codebase persists -- written here before the pause.
        - Anything else: ``failed`` is reachable only from ``preparing`` or ``resuming`` -- never
          from ``playing`` -- per the legal transition graph this module must not widen, so every
          other unclassified mid-play ``HarnessError`` lands in ``paused`` too: the one legal,
          recorded, non-destructive state ``playing`` can still reach.
        """
        with self._lock:
            state.last_error = (type(exc).__name__, self._deps.clock())
            if isinstance(exc, (RecoveryLimitReached, SaveAddressingError)):
                self._resync_after_external_termination(state, exc)
                return
            if isinstance(exc, UnknownScreenEncountered):
                self._deps.store.write_run_event(exc.event)
                self._pause_on_failure(state, exc)
                return
            if isinstance(exc, ProviderChainExhausted):
                self._pause_on_failure(state, exc)
                return
            # Anything else unclassified: `paused` too -- see this method's own docstring.
            self._pause_on_failure(state, exc)

    def _resync_after_external_termination(self, state: _RunState, exc: HarnessError) -> None:
        """Caller holds ``self._lock`` with ``state.last_error`` already set. See
        :meth:`_handle_run_failure` -- *exc* here is always ``RecoveryLimitReached`` or
        ``SaveAddressingError``, both raised only after ``RecoveryEngine`` already drove the run
        to ``failed`` through the legal ``resuming -> failed`` edge and persisted it."""
        refreshed = self._deps.store.get_run(state.run.run_id)
        if refreshed is not None:
            state.run = refreshed
        if state.run.lifecycle_state not in TERMINAL_STATES:
            # Defensive only: RecoveryEngine's own contract guarantees `failed` is already
            # recorded before either exception is raised. Reaching here would mean that contract
            # was violated elsewhere -- still not ours to swallow, so fall back to the same legal
            # pause every other unclassified failure gets, rather than trusting a stale `playing`.
            self._pause_on_failure(state, exc)

    def _pause_on_failure(self, state: _RunState, exc: HarnessError) -> None:
        """Caller holds ``self._lock`` with ``state.last_error`` already set. Transition to
        ``paused`` -- legal from ``playing`` (data-model.md SS4) -- recording *exc* on the
        transition event. A no-op if the run already reached a terminal state (a second failure
        racing the first)."""
        if state.run.lifecycle_state in TERMINAL_STATES:
            return
        state.run, event = transition(
            state.run,
            LifecycleState.PAUSED,
            occurred_at=self._deps.clock(),
            turn_number=state.current_turn,
            detail={"reason": str(exc), "error_type": type(exc).__name__},
        )
        self._deps.store.write_run_event(event)
        self._deps.store.update_run(state.run.run_id, lifecycle_state=state.run.lifecycle_state)

    def _record_unexpected_failure(self, state: _RunState, exc: Exception) -> None:
        """Last-resort safety net for :meth:`_play_run`: something failed *while this runner was
        already trying to record why the run stopped* (most plausibly ``transition()`` raising a
        second time because the run reached a state ``_handle_run_failure`` did not anticipate).
        ``state.last_error`` -- read by ``get_status``, and settable purely in-memory under
        ``self._lock`` -- is set here unconditionally, since it is the one guarantee that survives
        even this; the failure is also logged loudly (``telemetry.logging``, T015) rather than left
        for the scheduled coroutine's ``Future`` to silently discard, which is the defect this
        module exists to close."""
        with self._lock:
            state.last_error = (type(exc).__name__, self._deps.clock())
        log_event(
            get_harness_logger(),
            logging.ERROR,
            "run/runner: unhandled failure while recording why a run stopped playing",
            extra={"run_id": state.run.run_id, "error_type": type(exc).__name__},
            exc_info=True,
        )

    def _advance(self, state: _RunState, to_state: LifecycleState) -> tuple[Run, RunEvent]:
        """Caller holds ``self._lock``. Record and persist one lifecycle transition."""
        updated_run, event = transition(
            state.run, to_state, occurred_at=self._deps.clock(), turn_number=state.current_turn
        )
        self._deps.store.write_run_event(event)
        self._deps.store.update_run(state.run.run_id, lifecycle_state=updated_run.lifecycle_state)
        return updated_run, event

    # -- internals --------------------------------------------------------------

    def _require_state(self, run_id: RunId) -> _RunState:
        with self._lock:
            state = self._runs.get(run_id)
        if state is None:
            raise HarnessError("unknown run_id", detail={"run_id": run_id})
        return state

    def _schedule(self, coro: Any) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)
