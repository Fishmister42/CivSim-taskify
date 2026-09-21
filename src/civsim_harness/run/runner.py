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

**FR-004 -- resuming from a named save (T215).** :meth:`Runner.resume_from` is :meth:`Runner.start`
with a different starting point and nothing else changed: it resolves the rewind synchronously on
this runner's own loop (exactly as ``start`` resolves preparation), then schedules the same
:meth:`Runner._play_run` to play forward from the chosen turn. The rewind itself -- abandon,
``playing -> interrupted -> resuming``, load the save, back to ``playing`` -- is delegated whole to
``resilience.recovery.RecoveryEngine``, reached through the seam that already carries it
(``RunnerDependencies.build_turn_dependencies(...).recovery``), so the ``SaveLoader`` an operator
rewind uses is the same one mid-turn recovery uses. See that method's own docstring for the
Principle IV lineage record it writes and for the ordering that keeps a failed rewind
non-destructive.

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

from civsim_harness.config.run_config import (
    BranchFrom,
    load_branch_configuration_file,
    load_run_configuration_file,
    peek_branch_from,
)
from civsim_harness.errors import (
    HarnessError,
    ProviderChainExhausted,
    RecoveryLimitReached,
    StoreWriteError,
)
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
from civsim_harness.resilience.recovery import RecoveryEngine
from civsim_harness.run.decision_loop import UnknownScreenEncountered
from civsim_harness.run.lifecycle import TERMINAL_STATES, transition
from civsim_harness.run.stop import StopEvaluation, evaluate_stop
from civsim_harness.run.turn_cycle import TurnCycleDependencies, run_turn_cycle
from civsim_harness.saves.addressing import SaveAddressingError, require_available_save_point
from civsim_harness.store.completeness import (
    record_completeness_status,
    refresh_run_completeness,
)
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


class ResumeFromRefused(HarnessError):
    """:meth:`Runner.resume_from` refused the request before touching the game or the record at
    all -- an unknown run, a terminal run, a run still actively playing (its turn must not be cut
    short, FR-004/FR-008/SC-022), or a turn number that is not a turn. Nothing was abandoned,
    nothing was loaded, nothing was written: the run is exactly as it was."""


class ResumeFromFailed(HarnessError):
    """:meth:`Runner.resume_from` was accepted and then could not complete -- almost always
    because the save could not actually be loaded back into the client. Carries the underlying
    cause verbatim in ``detail['reason']`` / ``detail['error_type']`` rather than flattening it
    into a generic "resume failed", so an operator learns *which* step refused and why.

    Distinct from :class:`ResumeFromRefused`: by the time this is raised the rewind is already on
    the run's timeline (``turn_abandoned`` carrying this resume's lineage, and the
    ``playing -> interrupted -> resuming`` transitions ``resilience.recovery.RecoveryEngine``
    records before it ever attempts the load), and the run is left wherever that engine's own
    contract leaves it -- ``resuming`` below the recovery bound, ``failed`` at or above it.
    """


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
    prepare_branch: (
        Callable[[RunConfiguration, BranchFrom], PreparedRun | Awaitable[PreparedRun]] | None
    ) = None
    """T226, FR-033: preparation for a **branch** of an existing run, or `None` for a composition
    that cannot branch.

    Separate from ``prepare_run`` only because it needs one more argument -- the lineage the branch
    starts from -- not because it is a second pipeline: the production implementation
    (:mod:`civsim_harness.run.composition`) is literally the same call with ``branch_from`` set, so
    a branch passes through every gate an ordinary run does plus the branch-specific ones.

    ``None`` is the honest default for the many test compositions that supply a hand-built
    ``prepare_run`` and have no store records to branch from: :meth:`Runner.start` refuses a branch
    document against such a runner by name, rather than silently starting it as a fresh,
    unbranched run -- which is the one outcome worse than refusing (FR-033, Principle IV)."""

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


def _resume_lineage_event(
    run_id: RunId, turn: int, lineage: dict[str, Any], occurred_at: Timestamp
) -> RunEvent:
    """The Principle IV lineage record for one completed ``resume_from`` rewind: which run and
    turn this run resumed from, which save point was loaded, which turn it was abandoned from, and
    every ``(turn_number, attempt_index)`` the rewind superseded.

    ``RecoveryEngine`` writes its own ``resumed`` event carrying only the save point id -- correct
    for the crash-recovery case it was built for, where nothing was abandoned beyond the
    interrupted turn itself. An operator rewind abandons a *span* of recorded turns and will
    overwrite their quicksaves on replay, which is exactly the "branch that mutates or abandons a
    save" Principle IV requires to record which lineage it came from; this event is that record.
    """
    return RunEvent(
        event_id=EventId(uuid.uuid4().hex),
        run_id=run_id,
        turn_number=turn,
        event_type=RunEventType.RESUMED,
        occurred_at=occurred_at,
        detail=dict(lineage),
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
        """Start *config_path* -- an ordinary run configuration, **or a branch document** (T226).

        ``runner_protocol.py``'s own design note says ``run branch`` "needs no new seam at all:
        a branch document is a run configuration plus a ``branch_from`` block ... route it through
        the *existing* ``start``". That note was never carried into this method, which meant every
        branch command died here: ``load_run_configuration_file`` is the **non-branch** loader and
        ``RunConfiguration`` is ``extra="forbid"``, so a ``branch_from`` block failed validation
        outright and ``civsim run branch`` could not reach a branch at all.

        The two loaders are not interchangeable -- the branch loader needs the *parent's*
        ``RunConfiguration`` to inherit from, which cannot be fetched before the parent run id is
        known -- so which one applies is decided first, by
        :func:`~civsim_harness.config.run_config.peek_branch_from`, on one cheap parse.
        """
        branch_from = peek_branch_from(config_path)
        if branch_from is not None:
            return self.start_branch(config_path, branch_from)

        config = load_run_configuration_file(config_path)
        try:
            prepared = self._resolve_prepared_run(config)
        except RunPreparationFailed:
            raise
        except HarnessError as exc:
            raise RunPreparationFailed(
                "run preparation failed; no run was created", detail={"reason": str(exc)}
            ) from exc

        return self._begin(prepared, first_turn=None)

    def _begin(self, prepared: PreparedRun, *, first_turn: int | None) -> RunId:
        """Register *prepared* and schedule play, from turn 1 or from *first_turn* (T226).

        *first_turn* is ``None`` for a fresh run (play begins at turn 1) and the branch point for
        a branch: the parent's save at turn N is N's **start** quicksave, so the branch replays
        turn N itself -- the same arithmetic :meth:`resume_from` uses (``current_turn = turn - 1``,
        which ``_play_run``'s own ``(state.current_turn or 0) + 1`` lands exactly on N).

        A run that preflight already landed in `failed` (contracts/operator-surface.md D2, T101's
        host-tier gate, T072's build pin) is returned as-is: nothing to play, and the caller
        discovers the failure through `get_status`, exactly as `RunnerProtocol` specifies.
        """
        run_id = prepared.run.run_id
        with self._lock:
            state = _RunState(run=prepared.run, prepared=prepared)
            if first_turn is not None:
                state.current_turn = first_turn - 1
            self._runs[run_id] = state

        if prepared.run.lifecycle_state is LifecycleState.PLAYING:
            self._schedule(self._play_run(run_id))
        return run_id

    def start_branch(self, config_path: Path, branch_from: BranchFrom) -> RunId:
        """Start *config_path* as a branch of ``branch_from``'s run (T226, FR-033, FR-034).

        Every refusal below happens **before** a child `Run` exists, and each names what was
        missing. That matters more here than anywhere else in this module: the one outcome worse
        than refusing a branch is starting it as a fresh, unbranched run while calling it a branch
        -- Principle IV's "any branch that mutates or abandons a save MUST record which lineage it
        branched from" is unsatisfiable after the fact.

        The parent's own `RunConfiguration` is read back through
        `MatchStore.get_run_configuration`: a branch inherits seed, civilization, ruleset, mod
        set, map and game settings from it and is refused if it restates any of them differently
        (`config/run_config.py`), so there is no way to validate a branch document without it.

        **The save load is the production `LuaSaveLoader` (T217).** `run/composition.py` binds
        `saves/load_game.py`'s verified front-end `Network.LoadGame` path to the run's own
        connected client, so a real `civsim run branch` reaches an actual load; a load that
        fails -- or lands anywhere but the named save's exact position -- still refuses the
        branch by name, exactly as :meth:`resume_from` does. The failure is surfaced with the
        branch's own lineage attached rather than flattened into a generic "preparation failed".
        """
        prepare_branch = self._deps.prepare_branch
        if prepare_branch is None:
            raise RunPreparationFailed(
                "this runner was composed without branch support, so a branch document cannot be "
                "started through it; nothing was created (FR-033)",
                detail={"parent_run_id": branch_from.run_id, "turn": branch_from.turn},
            )

        store = self._deps.store
        if store.get_run(branch_from.run_id) is None:
            raise RunPreparationFailed(
                "cannot branch: no such parent run is on record",
                detail={"parent_run_id": branch_from.run_id, "turn": branch_from.turn},
            )
        parent_config = store.get_run_configuration(branch_from.run_id)
        if parent_config is None:
            raise RunPreparationFailed(
                "cannot branch: the parent run's own configuration is not on record, so the "
                "fields a branch inherits from it (seed, civilization, ruleset, mod set, map and "
                "game settings) cannot be resolved (FR-033)",
                detail={"parent_run_id": branch_from.run_id, "turn": branch_from.turn},
            )

        child_config, parsed = load_branch_configuration_file(
            config_path, parent_config=parent_config
        )

        try:
            prepared = self._resolve_prepared(prepare_branch(child_config, parsed))
        except RunPreparationFailed:
            raise
        except HarnessError as exc:
            raise RunPreparationFailed(
                f"branch of run {parsed.run_id} at turn {parsed.turn} could not be prepared, so "
                f"no branch was created: {exc}",
                detail={
                    "parent_run_id": parsed.run_id,
                    "turn": parsed.turn,
                    "reason": str(exc),
                    "error_type": type(exc).__name__,
                    **dict(getattr(exc, "detail", {}) or {}),
                },
            ) from exc

        return self._begin(prepared, first_turn=parsed.turn)

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
        return self._resolve_prepared(self._deps.prepare_run(config))

    def _resolve_prepared(self, result: PreparedRun | Awaitable[PreparedRun]) -> PreparedRun:
        """Await *result* on this runner's own loop if it is awaitable (see
        :meth:`_resolve_prepared_run` for why that loop specifically). Shared verbatim by the
        fresh-run and branch paths (T226), so a branch can never end up connecting its client on
        a different loop than the one that later plays its turns."""
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
                # T239, FR-052, Principle III: derived fresh from the store on every read --
                # never the in-memory Run's own field, which was a creation-time constant for
                # the whole life of the run before this. Deriving at read time is what makes a
                # run that died mid-flight report the gaps it left behind, rather than whatever
                # was last stamped on it.
                record_completeness_status=record_completeness_status(
                    self._deps.store, run_id
                ),
                comparability_status=state.run.comparability_status,
                archived=state.run.archived_at is not None,
                disk_headroom_gb=self._deps.disk_headroom_gb(),
            )

    def branch(self, *, parent_run_id: RunId, turn: int, config_path: Path) -> RunId:
        """Branch *parent_run_id* at *turn* using *config_path*'s branch document (T226, FR-033).

        The same path :meth:`start` takes for a ``branch_from``-bearing document, plus the one
        check only this entry point can make: that the document's own ``branch_from`` block
        **agrees with the arguments the operator typed**. `civsim run branch <run_id> --turn N
        --config doc.yaml` states the lineage twice, and a document naming a different parent or
        turn than the command is not a detail to reconcile silently -- under Principle IV the
        lineage is the record's load-bearing claim, so a disagreement is refused with both values
        named and nothing created.
        """
        branch_from = peek_branch_from(config_path)
        if branch_from is None:
            raise RunPreparationFailed(
                "this configuration is not a branch document: it carries no branch_from block "
                "naming the run and turn to branch from (contracts/run-configuration.md)",
                detail={"parent_run_id": parent_run_id, "turn": turn, "config": str(config_path)},
            )
        if branch_from.run_id != parent_run_id or branch_from.turn != turn:
            raise RunPreparationFailed(
                "the branch document's branch_from block names a different lineage than this "
                "command does; nothing was created (FR-033, Principle IV)",
                detail={
                    "command_parent_run_id": parent_run_id,
                    "command_turn": turn,
                    "document_parent_run_id": branch_from.run_id,
                    "document_turn": branch_from.turn,
                    "config": str(config_path),
                },
            )
        return self.start_branch(config_path, branch_from)

    def resume_from(self, run_id: RunId, turn: int) -> None:
        """Rewind *run_id* -- the same run, never a branch -- to its recorded turn-start save at
        *turn* and continue playing from there (T215, FR-004, FR-036,
        ``operator/runner_protocol.py``'s own ``resume_from`` contract).

        **Structurally, this is :meth:`start` with a different starting point.** ``start``
        resolves a :class:`PreparedRun` synchronously (:meth:`_resolve_prepared_run`, on this
        runner's own loop) and then schedules :meth:`_play_run` to play forward from turn 1. This
        method resolves the *rewind* synchronously -- on the same loop, for the same reason -- and
        then schedules the same :meth:`_play_run` to play forward from *turn*. Nothing about how a
        turn is played, recorded, or stopped differs; only where the loop starts counting
        (``state.current_turn`` is set to ``turn - 1``, so ``_play_run``'s own
        ``(state.current_turn or 0) + 1`` lands exactly on *turn*).

        **Nothing here re-implements recovery.** ``resilience.recovery.RecoveryEngine`` already
        does precisely this job -- abandon the in-progress attempt, drive
        ``playing -> interrupted -> resuming``, load a named save, return to ``playing``, and put
        ``turn_abandoned``/``resumed`` on the timeline -- and ``runner_protocol.py`` names it
        outright as what ``resume_from`` is "an operator-invoked counterpart to". The engine is
        reached through the seam that already carries it, ``RunnerDependencies.
        build_turn_dependencies(...)``'s :attr:`~civsim_harness.run.turn_cycle.
        TurnCycleDependencies.recovery`, so the ``SaveLoader`` used here is the *same* one the
        composition root wired for mid-turn recovery -- never a second, parallel load path of this
        module's own.

        **Principle IV -- lineage.** A rewind abandons every attempt at *turn* and after it, and
        the replay will overwrite those turns' quicksaves, so which lineage the run resumed from
        MUST be recorded. It is recorded twice, on purpose: once *before* the load, as the
        ``turn_abandoned`` event's ``trigger_detail`` (so even a rewind that never completes
        leaves an auditable claim of what it was trying to do), and once *after*, as a ``resumed``
        event naming the run and turn resumed from, the save point that was loaded, the turn the
        run was abandoned from, and every ``(turn_number, attempt_index)`` this rewind superseded.

        **Why the supersede happens after the load, not before.** Marking those attempts
        non-authoritative is what makes the replay writable at all (``store/sqlite_adapter.py``
        refuses a second authoritative attempt for a ``(run_id, turn_number)`` that already has
        one, invariant I9) and is the FR-047-shaped record of the abandonment -- never a delete.
        But doing it first would mean a rewind that then fails to load had stripped the run's
        authoritative record for nothing. A live load can still genuinely fail -- the production
        ``LuaSaveLoader`` (T217, ``saves/load_game.py``) refuses a load that does not land on the
        named save's exact position, and the client can simply not come back up -- so ordering
        the supersede after the load is what keeps every such failure non-destructive.

        **Why the replayed turn can persist at all (T223).** Superseding an attempt does not free
        its ``(run_id, turn_number, attempt_index)`` triple -- ``store/sqlite_adapter.py``'s D4
        idempotency check rejects a second write of that triple carrying different content -- so a
        replay that started at ``attempt_index = 0``, as ``run/turn_cycle.py`` once unconditionally
        did, would collide with the very attempt this rewind just superseded and halt the run at
        its first persist. The index is now supplied by ``TurnCycleDependencies.
        attempt_index_base``, resolved per turn in ``run/composition.py``'s
        ``build_turn_dependencies`` as one past the highest attempt already on record. Nothing here
        computes it: this method's ordering (supersede *after* the load) is what leaves those
        records in place for that resolution to see.

        Raises :class:`ResumeFromRefused` when the request is not legal right now,
        :class:`~civsim_harness.saves.addressing.SaveAddressingError` when the save at *turn* is
        missing or already removed (FR-036 -- never retargeted to a nearby turn), and
        :class:`ResumeFromFailed` when the rewind was accepted but could not be carried out.
        """
        if turn < 1:
            raise ResumeFromRefused(
                "resume-from needs a real turn number: turns are numbered from 1",
                detail={"run_id": run_id, "turn": turn},
            )

        state = self._require_state(run_id)
        with self._lock:
            current = state.run.lifecycle_state
            abandoned_from_turn = state.current_turn
        self._require_resumable(run_id, current, turn)

        # FR-032/FR-036: resolved purely from store records -- never the filesystem, never the
        # client -- and refused outright if already recorded absent, rather than retargeted.
        save_point = require_available_save_point(self._deps.store, run_id, turn)

        recovery = self._recovery_engine_for(run_id, state.prepared, turn)
        lineage: dict[str, Any] = {
            "command": "resume-from",
            "resumed_from_run_id": str(run_id),
            "resumed_from_turn": turn,
            "save_point_id": str(save_point.save_point_id),
            "save_name": save_point.save_name,
            "abandoned_from_turn": abandoned_from_turn,
        }

        with self._lock:
            if state.run.lifecycle_state is LifecycleState.PAUSED:
                # `RecoveryEngine.recover` drives `playing -> interrupted -> resuming`; `paused`
                # is not one of the three states it accepts, and `paused -> playing` is the one
                # legal edge out of it (data-model.md SS4). Recorded like any other transition.
                state.run, _event = self._advance(state, LifecycleState.PLAYING)
            run_snapshot = state.run

        try:
            # Same loop, same reason as `_resolve_prepared_run`: the SaveLoader this engine was
            # wired with dispatches through the composition root's connected NexusClient, whose
            # lock and streams are bound to `self._loop`. The lock is deliberately NOT held across
            # this blocking wait -- `_play_run` takes it on that very loop.
            result = asyncio.run_coroutine_threadsafe(
                recovery.recover(
                    run_snapshot,
                    turn_number=turn,
                    turn_start_save=save_point,
                    trigger_event_type=RunEventType.LIFECYCLE_COMMAND_RECEIVED,
                    trigger_detail=lineage,
                ),
                self._loop,
            ).result()
        except SaveAddressingError:
            # FR-036 again, discovered at load time rather than in the store: RecoveryEngine has
            # already recorded `save_missing` and failed the run. Surfaced unchanged -- it already
            # names the exact save, which is the whole contract.
            self._note_resume_failure(state, run_id)
            raise
        except Exception as exc:
            self._note_resume_failure(state, run_id)
            raise ResumeFromFailed(
                f"resume-from could not reload run {run_id}'s save at turn {turn}: {exc}",
                detail={
                    **lineage,
                    "reason": str(exc),
                    "error_type": type(exc).__name__,
                    **dict(getattr(exc, "detail", {}) or {}),
                },
            ) from exc

        with self._lock:
            state.run = result.run
            superseded = self._supersede_replayed_turns(
                run_id, from_turn=turn, through_turn=abandoned_from_turn
            )
            self._deps.store.write_run_event(
                _resume_lineage_event(
                    run_id, turn, {**lineage, "superseded_turns": superseded}, self._deps.clock()
                )
            )
            # T239: superseding is one of the moments record_completeness_status can change --
            # the rewound-past attempts just stopped being authoritative -- so the persisted
            # field is re-derived here rather than carried stale into the replay.
            refresh_run_completeness(self._deps.store, run_id)
            state.current_turn = turn - 1
            state.current_step = None
            state.pause_requested = False
            self._schedule(self._play_run(run_id))

    def _require_resumable(self, run_id: RunId, current: LifecycleState, turn: int) -> None:
        """The three refusals :meth:`resume_from` makes before anything is touched.

        ``playing`` is refused rather than pre-empted: a turn is never cut short to honour an
        operator command (FR-004, FR-008, SC-022), and rewinding under a live ``_play_run`` would
        do exactly that -- the in-flight turn would go on to write a record for a turn this rewind
        has already abandoned. Pause first, then resume-from; the message says so.
        """
        if current in TERMINAL_STATES:
            raise ResumeFromRefused(
                "a run already in a terminal lifecycle state cannot be resumed from a save",
                detail={"run_id": run_id, "turn": turn, "lifecycle_state": current.value},
            )
        if current not in (
            LifecycleState.PAUSED,
            LifecycleState.INTERRUPTED,
            LifecycleState.RESUMING,
        ):
            raise ResumeFromRefused(
                "resume-from is only legal for a run that is not actively playing: pause the run "
                "first (a turn is never cut short to honour an operator command, FR-004/FR-008), "
                "then resume it from the turn you want",
                detail={"run_id": run_id, "turn": turn, "lifecycle_state": current.value},
            )

    def _recovery_engine_for(
        self, run_id: RunId, prepared: PreparedRun, turn: int
    ) -> RecoveryEngine:
        """The composition root's own ``RecoveryEngine`` for this run, reached through the seam
        that already carries it. ``build_turn_dependencies`` is the composition root's closure and
        may fail for reasons of its own (``run/composition.py`` looks the run up in a dict it
        populated at preparation time, so a run it never prepared raises ``KeyError``) -- wrapped
        here so an operator sees a named refusal rather than a bare traceback.
        """
        try:
            return self._deps.build_turn_dependencies(prepared, turn).recovery
        except Exception as exc:
            raise ResumeFromFailed(
                f"resume-from could not assemble run {run_id}'s per-turn collaborators, so there "
                f"is no save loader to resume through: {exc}",
                detail={
                    "run_id": run_id,
                    "turn": turn,
                    "reason": str(exc),
                    "error_type": type(exc).__name__,
                },
            ) from exc

    def _supersede_replayed_turns(
        self, run_id: RunId, *, from_turn: int, through_turn: int | None
    ) -> list[dict[str, int]]:
        """Caller holds ``self._lock``. Mark every authoritative attempt at *from_turn* and after
        it abandoned and non-authoritative (``MatchStore.mark_turn_superseded``, FR-047) -- never
        deleted, always still retrievable -- so the replay may write its own attempts.

        The upper bound comes from the store, not from this runner's in-memory
        ``state.current_turn``: every turn takes an FR-007 quicksave before it does anything else,
        so the highest recorded save point is the highest turn this run ever began, which is
        correct even for a run whose loop this process never drove.

        A turn some branch recorded as its lineage point is refused by the store itself
        (parent immutability, FR-034/I12) -- surfaced as :class:`ResumeFromFailed` naming the
        child run, never worked around.
        """
        store = self._deps.store
        highest = max(
            (save.turn_number for save in store.list_save_points(run_id)), default=from_turn
        )
        last_turn = max(highest, through_turn or from_turn, from_turn)

        superseded: list[dict[str, int]] = []
        for turn_number in range(from_turn, last_turn + 1):
            record = store.get_turn_cycle(run_id, turn_number, authoritative_only=True)
            if record is None:
                continue
            attempt_index = record.turn_cycle.attempt_index
            try:
                store.mark_turn_superseded(run_id, turn_number, attempt_index)
            except StoreWriteError as exc:
                raise ResumeFromFailed(
                    f"resume-from reloaded run {run_id}'s save at turn {from_turn}, but turn "
                    f"{turn_number}'s recorded attempt may not be superseded: {exc}",
                    detail={
                        "run_id": run_id,
                        "resumed_from_turn": from_turn,
                        "turn_number": turn_number,
                        "attempt_index": attempt_index,
                        "superseded_before_refusal": list(superseded),
                        "reason": str(exc),
                        **dict(getattr(exc, "detail", {}) or {}),
                    },
                ) from exc
            superseded.append({"turn_number": turn_number, "attempt_index": attempt_index})
        return superseded

    def _note_resume_failure(self, state: _RunState, run_id: RunId) -> None:
        """Record that a resume-from attempt failed, and resync this runner's in-memory ``Run``
        with whatever ``RecoveryEngine`` persisted on its way out (``resuming``, or ``failed``
        once the recovery bound was reached) -- the same resync
        :meth:`_resync_after_external_termination` performs for the play loop's own version of
        this situation, and for the same reason: ``state.run`` is stale after any transition the
        engine drove itself.
        """
        with self._lock:
            state.last_error = ("ResumeFromFailed", self._deps.clock())
            refreshed = self._deps.store.get_run(run_id)
            if refreshed is not None:
                state.run = refreshed

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
        # T239: a terminal transition is one of the moments record_completeness_status can
        # change (the trailing-turn rule in turn_gaps widens once the run stops advancing), so
        # the persisted Run's field is re-derived here rather than left at whatever it was.
        refresh_run_completeness(self._deps.store, state.run.run_id)

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
            return
        # T239: the engine drove the run terminal before raising -- the same
        # terminal-transition moment `_finish` covers, so the persisted field is re-derived
        # here too. A run failed mid-flight with a quicksave taken and no TurnCycle behind it
        # is exactly the gap Principle III requires this field to surface.
        refresh_run_completeness(self._deps.store, state.run.run_id)

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
        # T239: `paused` leaves the actively-playing set, which widens turn_gaps' checked range
        # -- the FR-042 chain-exhaustion shape (a quicksave taken, no TurnCycle ever persisted)
        # becomes a real, reportable gap at exactly this moment. Persist the derivation.
        refresh_run_completeness(self._deps.store, state.run.run_id)

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
