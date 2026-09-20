"""Recovery (T151, T153, T154).

FR-045 - FR-048, research R12's recovery paragraph. On detecting a crash,
hang, unresponsive client, or unknown screen (the four `resilience.detector`
signals), or on a mid-turn `ObservationAssemblyError` (T153), the harness
must:

- preserve the last-known-good save and state (FR-045) -- nothing here
  ever deletes or overwrites a save; `MatchStore.get_last_known_good`
  remains available throughout, and is what the failure path below
  reports;
- resume from *this interrupted turn's own* start quicksave as the same
  continuous run (FR-045) -- not "the most recent good save" in the
  abstract, which is a different question `get_last_known_good` answers
  only once recovery itself has given up (T154);
- re-observe and re-capture before acting (FR-046) -- enforced
  structurally here by `RecoveryResult` carrying no observation or capture
  of its own: there is nothing in it a caller could act on without first
  re-observing from scratch;
- put both the abandonment and the resumption on the run's timeline
  (`turn_abandoned`, `resumed`) so FR-047's "both attempts recorded" is
  visible on the event log (the store-level bookkeeping of which turn
  *attempt* is authoritative is `run/turn_cycle.py`'s job, T152, not this
  module's); and
- stop after `recovery_attempt_limit` consecutive failed recovery attempts
  rather than retrying indefinitely (FR-048, SC-021, T154).

**Nothing obtained before the interruption survives into `RecoveryResult`.**
That is deliberate, not an oversight: FR-046 forbids acting on anything
captured before the interruption, and the only way to make that
structurally true -- rather than a convention a caller might forget -- is
for this module to simply never hand any of it back.

**Nothing here measures elapsed turn time.** `RecoveryEngine` counts
*consecutive failed recovery attempts* (an integer, incremented once per
failed call to :meth:`RecoveryEngine.recover`), never a duration -- the
recovery bound is orthogonal to how long the interrupted turn itself had
been running, which FR-014 leaves unbounded.

`SaveLoader` is a small local protocol, not an import from `saves/`: that
package (as of this wave) only knows how to *take* a quicksave
(`saves/save_point.py`, `saves/save_game.py`), not load one back in, and it
is being built concurrently besides. `run/turn_cycle.py` (T152, a
concurrent wave) wires a concrete loader in once one exists.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from civsim_harness.errors import HarnessError, ObservationAssemblyError, RecoveryLimitReached
from civsim_harness.models.common import EventId, RunId, Timestamp
from civsim_harness.models.records import RunEvent, RunEventType, SavePoint
from civsim_harness.models.run import LifecycleState, Run, StopResolution
from civsim_harness.run.lifecycle import transition
from civsim_harness.store.port import MatchStore


def _utcnow() -> Timestamp:
    return datetime.now(UTC)


class SaveLoader(Protocol):
    """What recovery needs from the save-management layer: load a specific
    save point back into the running game client. Which save to load is
    entirely `RecoveryEngine`'s decision -- this protocol only executes it.
    """

    async def load(self, save: SavePoint) -> None:
        """Load *save* into the game client, replacing its current state."""
        ...


def _event(
    *,
    run_id: RunId,
    event_type: RunEventType,
    occurred_at: Timestamp,
    turn_number: int | None = None,
    detail: Mapping[str, Any] | None = None,
) -> RunEvent:
    return RunEvent(
        event_id=EventId(uuid.uuid4().hex),
        run_id=run_id,
        turn_number=turn_number,
        event_type=event_type,
        occurred_at=occurred_at,
        detail=dict(detail) if detail else {},
    )


@dataclass(frozen=True)
class RecoveryResult:
    """What the caller must do next: resume play from `resumed_from`.

    Carries no observation, capture, or any other pre-interruption artifact
    -- see the module docstring's FR-046 note. `events` is every `RunEvent`
    this call produced, in order, already durably written via
    `MatchStore.write_run_event` -- the caller does not need to persist
    them again.
    """

    run: Run
    resumed_from: SavePoint
    events: tuple[RunEvent, ...]


class RecoveryEngine:
    """Orchestrates FR-045 - FR-048 recovery for one run.

    One instance per active run: it is the sole keeper of that run's
    consecutive-failed-recovery count (FR-048), which must survive across
    separate interruptions within the same run but must never leak into
    another run's count. A *successful* recovery resets the count to zero
    -- the bound is on consecutive failures, not on how many times a run
    has ever recovered in total.

    `recover()` expects *run* to reflect the run's currently persisted
    `lifecycle_state` (re-fetch via `MatchStore.get_run` first if unsure,
    e.g. after a prior failed call) -- it drives `playing -> interrupted ->
    resuming` itself and is idempotent across repeated calls for the *same*
    interruption (a run already at `interrupted` or `resuming` is carried
    forward without repeating the abandon step), but it is not idempotent
    across calls for *different* runs or stale snapshots.
    """

    def __init__(
        self,
        *,
        run_id: RunId,
        store: MatchStore,
        loader: SaveLoader,
        recovery_attempt_limit: int,
        clock: Callable[[], Timestamp] = _utcnow,
    ) -> None:
        if recovery_attempt_limit < 1:
            raise ValueError("recovery_attempt_limit must be >= 1")
        self._run_id = run_id
        self._store = store
        self._loader = loader
        self._recovery_attempt_limit = recovery_attempt_limit
        self._clock = clock
        self._consecutive_failures = 0

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def consecutive_failures(self) -> int:
        """How many recovery attempts have failed in a row, right now."""
        return self._consecutive_failures

    async def recover(
        self,
        run: Run,
        *,
        turn_number: int,
        turn_start_save: SavePoint,
        trigger_event_type: RunEventType,
        trigger_detail: Mapping[str, Any] | None = None,
    ) -> RecoveryResult:
        """Abandon the in-progress attempt at *turn_number* and resume from
        *turn_start_save* as the same continuous run (FR-045, FR-046).

        *trigger_event_type* is whichever detection produced this call --
        `crash_detected`, `hang_detected`, `unresponsive_detected`,
        `unknown_screen`, or `observation_assembly_failed` (see
        :meth:`recover_from_observation_assembly_error`) -- recorded on the
        `turn_abandoned` event's detail so the timeline shows *why* the
        attempt was abandoned, not only that it was.

        Raises `RecoveryLimitReached` once `recovery_attempt_limit`
        consecutive attempts have failed: at that point the run is
        transitioned to `failed` (identifying `MatchStore.get_last_known_
        good`, FR-048), a `recovery_limit_reached` event is recorded, and
        `RecoveryLimitReached` replaces the underlying error. Below the
        limit, the underlying error propagates unchanged and the run is
        left at `resuming` for a subsequent call to retry (FR-048, T154 --
        no indefinite retry loop, but no silent swallowing either).
        """
        occurred_at = self._clock()
        if turn_start_save.turn_number != turn_number:
            raise ValueError(
                "turn_start_save does not belong to the turn being recovered: "
                f"save is for turn {turn_start_save.turn_number}, recovering turn {turn_number}"
            )

        resuming, phase_a_events = self._ensure_interrupted_and_resuming(
            run,
            turn_number=turn_number,
            occurred_at=occurred_at,
            trigger_event_type=trigger_event_type,
            trigger_detail=trigger_detail,
        )

        try:
            await self._loader.load(turn_start_save)

            playing, playing_event = transition(
                resuming,
                LifecycleState.PLAYING,
                occurred_at=occurred_at,
                turn_number=turn_number,
            )
            resumed_event = _event(
                run_id=run.run_id,
                event_type=RunEventType.RESUMED,
                occurred_at=occurred_at,
                turn_number=turn_number,
                detail={"save_point_id": turn_start_save.save_point_id},
            )
            self._store.write_run_event(playing_event)
            self._store.write_run_event(resumed_event)
            self._store.update_run(run.run_id, lifecycle_state=playing.lifecycle_state)

            self._consecutive_failures = 0
            return RecoveryResult(
                run=playing,
                resumed_from=turn_start_save,
                events=(*phase_a_events, playing_event, resumed_event),
            )
        except Exception as exc:
            await self._on_recovery_attempt_failed(resuming, occurred_at=occurred_at, cause=exc)
            raise

    async def recover_from_observation_assembly_error(
        self,
        run: Run,
        *,
        turn_number: int,
        turn_start_save: SavePoint,
        error: ObservationAssemblyError,
    ) -> RecoveryResult:
        """Wire an `ObservationAssemblyError` into the same abandon-and-
        replay path as a crash (T153, FR-046): a turn cannot continue on a
        stale board once earlier steps in it have already executed, so a
        mid-turn observation failure is treated exactly like a detected
        crash or hang, not repaired in place.

        Records the `observation_assembly_failed` event itself (unlike the
        four `resilience.detector` signals, nothing else produces this
        event), then delegates to :meth:`recover`.
        """
        occurred_at = self._clock()
        self._store.write_run_event(
            _event(
                run_id=run.run_id,
                event_type=RunEventType.OBSERVATION_ASSEMBLY_FAILED,
                occurred_at=occurred_at,
                turn_number=turn_number,
                detail={"message": error.message, **error.detail},
            )
        )
        return await self.recover(
            run,
            turn_number=turn_number,
            turn_start_save=turn_start_save,
            trigger_event_type=RunEventType.OBSERVATION_ASSEMBLY_FAILED,
            trigger_detail={"message": error.message, **error.detail},
        )

    # -- internals ----------------------------------------------------------

    def _ensure_interrupted_and_resuming(
        self,
        run: Run,
        *,
        turn_number: int,
        occurred_at: Timestamp,
        trigger_event_type: RunEventType,
        trigger_detail: Mapping[str, Any] | None,
    ) -> tuple[Run, tuple[RunEvent, ...]]:
        """Drive *run* to `resuming`, recording `turn_abandoned` exactly once.

        Idempotent across repeated `recover()` calls for the same
        interruption: a run already at `interrupted` or `resuming` (a
        retried recovery attempt) is carried forward without repeating the
        abandon step, since the turn was already abandoned by an earlier
        call. Deliberately kept separate from the risky
        `SaveLoader.load()` step (in :meth:`recover`) so that a failure
        there always finds *this* run already, reliably, at `resuming` --
        which is what guarantees the failure path can always legally reach
        `failed` (data-model.md SS4: `failed` is reachable only from
        `resuming` or `preparing`).
        """
        if run.lifecycle_state == LifecycleState.RESUMING:
            return run, ()
        if run.lifecycle_state == LifecycleState.INTERRUPTED:
            resuming, resuming_event = transition(
                run, LifecycleState.RESUMING, occurred_at=occurred_at, turn_number=turn_number
            )
            self._store.write_run_event(resuming_event)
            self._store.update_run(run.run_id, lifecycle_state=resuming.lifecycle_state)
            return resuming, (resuming_event,)
        if run.lifecycle_state == LifecycleState.PLAYING:
            abandoned_event = _event(
                run_id=run.run_id,
                event_type=RunEventType.TURN_ABANDONED,
                occurred_at=occurred_at,
                turn_number=turn_number,
                detail={"trigger": trigger_event_type.value, **dict(trigger_detail or {})},
            )
            self._store.write_run_event(abandoned_event)

            interrupted, interrupt_event = transition(
                run, LifecycleState.INTERRUPTED, occurred_at=occurred_at, turn_number=turn_number
            )
            self._store.write_run_event(interrupt_event)
            self._store.update_run(run.run_id, lifecycle_state=interrupted.lifecycle_state)

            resuming, resuming_event = transition(
                interrupted,
                LifecycleState.RESUMING,
                occurred_at=occurred_at,
                turn_number=turn_number,
            )
            self._store.write_run_event(resuming_event)
            self._store.update_run(run.run_id, lifecycle_state=resuming.lifecycle_state)
            return resuming, (abandoned_event, interrupt_event, resuming_event)

        raise HarnessError(
            f"cannot recover a run from lifecycle_state={run.lifecycle_state.value!r}",
            detail={"run_id": run.run_id, "lifecycle_state": run.lifecycle_state.value},
        )

    async def _on_recovery_attempt_failed(
        self, resuming: Run, *, occurred_at: Timestamp, cause: Exception
    ) -> None:
        """*resuming* is always at `resuming` here (see
        `_ensure_interrupted_and_resuming`'s docstring) -- that is what
        guarantees the `failed` transition below is always legal.
        """
        self._consecutive_failures += 1
        if self._consecutive_failures < self._recovery_attempt_limit:
            return

        last_known_good = self._store.get_last_known_good(resuming.run_id)
        detail: dict[str, Any] = {
            "consecutive_failures": self._consecutive_failures,
            "recovery_attempt_limit": self._recovery_attempt_limit,
            "cause": str(cause),
            "last_known_good_save_point_id": (
                last_known_good.save_point_id if last_known_good is not None else None
            ),
        }
        self._store.write_run_event(
            _event(
                run_id=resuming.run_id,
                event_type=RunEventType.RECOVERY_LIMIT_REACHED,
                occurred_at=occurred_at,
                detail=detail,
            )
        )

        failed_run, failed_event = transition(
            resuming,
            LifecycleState.FAILED,
            occurred_at=occurred_at,
            stop_resolution=StopResolution.UNRECOVERABLE_FAILURE,
            detail=detail,
        )
        self._store.write_run_event(failed_event)
        self._store.update_run(
            resuming.run_id,
            lifecycle_state=failed_run.lifecycle_state,
            ended_at=failed_run.ended_at,
            stop_resolution=failed_run.stop_resolution,
        )

        raise RecoveryLimitReached(
            "recovery_attempt_limit consecutive recovery attempts failed",
            detail=detail,
        ) from cause
