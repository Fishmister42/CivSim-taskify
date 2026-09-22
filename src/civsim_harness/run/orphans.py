"""Orphaned-run detection and the honest transition out of one (2026-09-21).

**The blemish this closes.** `uv run civsim store runs` showed
`run-02168773ad79486c9eb7c7a0f6f7b958` in lifecycle state `playing` with completeness
`complete` for hours after its driver process had been killed. The turn records were
intact; only the lifecycle was wrong. Nothing in the harness transitions a run whose
driver never comes back -- a killed driver, an API outage that takes the agent down
mid-turn (20:47 that night), or a host reboot all leave the same wreck: a run record
that claims to be in flight, indefinitely.

That is a Principle III problem before it is an ergonomics one. A `playing` run is
read as *live* by everything downstream -- `store runs`, the web interface, and
`store/sqlite_reads.py`'s own `turn_gaps`, which deliberately forgives a trailing
quicksave with no `TurnCycle` behind it **while a run is actively playing** because
that is the normal in-flight shape (FR-007). A dead run left in `playing` therefore
does not merely look wrong: it suppresses the gap report that would otherwise name its
last, unfinished turn. Completeness reads `complete` for a record that stops mid-turn.

**What proves a run is orphaned.** The run-identity lock (`run/identity_lock.py`,
FR-006/V8/R4) is already the harness's claim on a run: acquired during preparation,
before turn 1, keyed to the located Civ VI client's PID, and released when the run
reaches a terminal state (`run/composition.py`'s T214/T227 sweep). So a run in
`preparing` or `playing` whose lock file is **absent**, **malformed**, or names a
**dead PID** is a run nobody is holding. Nothing will ever transition it, because the
process that would have is gone.

A run whose lock is held by a live process is never touched here, on any path. That is
the single invariant this module is built around: the cost of pausing a live run
mid-turn is far higher than the cost of leaving a dead one mis-stated for another hour.

**What it transitions to, and why not anything else.** `paused`. The run's records are
intact and `paused -> playing` already leads back, so `paused` is both true and
recoverable. Never `finished` -- that asserts exactly one `stop_resolution` the run
never reached, which is Principle VII's silent data loss dressed as tidiness. Never
`failed` -- terminal, unrecoverable, and a killed driver is not a failed run. And never
silently: every transition writes its `lifecycle_transition` `RunEvent` first, carrying
`reason: orphaned` and the whole of what was checked -- lock path, whether the file was
there, the PID and its liveness, the last recorded activity and how stale it was. An
operator reading the timeline can re-derive the decision from the event alone.

**The grace window.** A run's lock is claimed *during* preparation, so there is a brief
moment between `create_run` and `acquire` in which a genuinely live run has no lock
file yet. Where the store knows when the run was last active (its `started_at`, or the
timestamp of its most recent `RunEvent`), a run idle for less than
:data:`DEFAULT_ORPHAN_GRACE_SECONDS` is left alone. A run that has not
started and has recorded nothing is dated by its configuration's `created_at` (written
by the same `create_run`), so the window covers it too. Only a run with no lock holder,
no activity *and* no configuration on record -- which a real store cannot produce -- is
read as orphaned on the absent lock alone (`last_activity_source: none`).

**Two entry points, one rule.** :func:`sweep_orphans` runs automatically on a
write-mode store open (`store/sqlite_adapter.py`) and in the runner's start path
(`run/runner.py`) -- the two moments something is about to act on the store and would
otherwise build on a lie. `civsim store repair [--dry-run]`
(`operator/store_cli.py`) is the operator's hand on the same function: `--dry-run`
calls :func:`scan_orphans` and prints the evidence without writing a byte.

----

**The other direction (2026-09-22).** Everything above starts from a run *row* and asks
about its lock. A lock whose run has no row is therefore outside all of it, and that is
not a hypothetical: `run-09110770797041989212fe6559f57900` (client pid 2459457, acquired
12:45:58Z) reached `finished`, its driver exited, its lock file stayed -- and the run was
never written to any store, so there was no row to pause. The next run could not start.
`RunIdentityLock.acquire` refuses a second run identity on a client PID an existing lock
already names, and the Civ VI client is long-lived, so the dead run's lock went on naming
a *live* PID. Nothing in the harness could say why, because nothing was looking at the
lock directory at all.

:func:`scan_stray_locks` closes that asymmetry: it enumerates the lock directory and asks
of each file whether it still stands for anything. :func:`clean_stray_locks` removes the
ones that provably do not, and `civsim store repair` is the same operator surface as
above. What "provably" means is :class:`StrayLockDisposition`, and it is deliberately
narrow -- see that class; a lock this module cannot settle is *reported loudly and left
alone*, because deleting a live run's lock is a far worse failure than naming a dead
one.

One asymmetry survives on purpose. The run-side sweep repairs automatically on a store
open; the lock side only ever *reports* there (:func:`sweep_stray_locks`) and deletes
nothing unless an operator asks. A store open is one process's intention to use one
store; the lock directory is shared by every store on the host, and a lock with no row
*here* may be a live run recorded *there*. That is the same reasoning the grace window
already encodes, applied to a file rather than a row.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from civsim_harness.models.common import EventId, RunId, Timestamp
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.records import RunEvent
from civsim_harness.models.run import LifecycleState, Run
from civsim_harness.run.identity_lock import LockInspection, RunIdentityLock
from civsim_harness.run.lifecycle import TERMINAL_STATES, transition
from civsim_harness.telemetry.logging import get_harness_logger, log_event

#: The `detail["reason"]` every transition this module records carries. One spelling,
#: so a timeline query for orphan repairs is exact rather than a text search.
ORPHAN_REASON = "orphaned"

#: The states a run can be orphaned *in*: the two in which the harness claims to be
#: holding the run and from which nothing but its own driver ever transitions it.
#:
#: `waiting_on_model` and `waiting_on_game` are deliberately excluded even though they
#: are also non-terminal in-flight states. Both are *transient sub-states of a turn* --
#: `run/decision_loop.py` enters and leaves them within one decision step -- and a run
#: parked in one of them with a dead lock is a rarer wreck whose correct resting state
#: is not obviously `paused` (the SS4 graph gives them one outgoing edge each, back to
#: `playing`). This module repairs the case that was actually observed and does not
#: guess at the ones that were not.
ORPHAN_CANDIDATE_STATES: frozenset[LifecycleState] = frozenset(
    {LifecycleState.PREPARING, LifecycleState.PLAYING}
)

#: How long a candidate run must have been idle before an absent lock is read as
#: orphaned rather than as a run still claiming its identity. Two minutes: long enough
#: to cover the `create_run` -> `acquire` window of a live preparation (a few seconds of
#: local gates, a tuner connect and a state-index resolution), short enough that an
#: operator who kills a driver does not wait on it.
DEFAULT_ORPHAN_GRACE_SECONDS = 120.0


@dataclass(frozen=True)
class OrphanFinding:
    """One run diagnosed as orphaned, with everything that was checked to say so.

    This is the evidence, not the act: constructing one writes nothing. It is what
    `--dry-run` prints and what :func:`pause_orphan` quotes into the transition event,
    so the two can never disagree about why a run was repaired.
    """

    run_id: RunId
    lifecycle_state: LifecycleState
    lock: LockInspection
    last_activity_at: Timestamp | None
    last_activity_source: str
    idle_seconds: float | None
    checked_at: Timestamp

    @property
    def why(self) -> str:
        """The one-phrase cause, for a listing line and for the event detail."""
        if not self.lock.present:
            return "identity lock absent"
        if not self.lock.readable:
            return "identity lock unreadable"
        return f"lock holder pid {self.lock.client_pid} is not alive"

    def as_detail(self) -> dict[str, Any]:
        """The finding as the `RunEvent.detail` payload (FR-003, Principle VII).

        Everything an operator needs to re-derive the decision from the timeline alone,
        without the lock file -- which by then may be gone for good.
        """
        detail: dict[str, Any] = {
            "reason": ORPHAN_REASON,
            "orphan_cause": self.why,
            "checked_at": self.checked_at.isoformat(),
            "last_activity_at": (
                self.last_activity_at.isoformat() if self.last_activity_at else None
            ),
            "last_activity_source": self.last_activity_source,
            "idle_seconds": (
                round(self.idle_seconds, 3) if self.idle_seconds is not None else None
            ),
        }
        detail.update(self.lock.as_detail())
        return detail

    def render(self) -> str:
        """One operator-facing line: the run, its state, and the evidence."""
        idle = "unknown" if self.idle_seconds is None else f"{self.idle_seconds:.0f}s"
        last = self.last_activity_at.isoformat() if self.last_activity_at else "-"
        return (
            f"{self.run_id:<38} {self.lifecycle_state.value:<10} -> paused  "
            f"{self.why}; lock={self.lock.lock_path} "
            f"pid={self.lock.client_pid if self.lock.client_pid is not None else '-'} "
            f"last_activity={last} ({self.last_activity_source}) idle={idle}"
        )


class OrphanScanStore(Protocol):
    """The reads :func:`scan_orphans` needs -- all of them on the 002 `MatchStore` port.

    Narrower than `MatchStore` on purpose: detection is a pure read over two published
    methods, so a fake in a test is two methods rather than twenty-odd, and this module
    carries no import edge to the store package (which imports *it*).
    """

    def list_active_runs(self) -> list[Run]: ...

    def list_run_events(self, run_id: RunId) -> list[RunEvent]: ...

    def get_run_configuration(self, run_id: RunId) -> RunConfiguration | None: ...


class OrphanRepairStore(OrphanScanStore, Protocol):
    """:class:`OrphanScanStore` plus the two writes :func:`pause_orphan` performs."""

    def get_run(self, run_id: RunId) -> Run | None: ...

    def write_run_event(self, event: RunEvent) -> EventId: ...

    def update_run(self, run_id: RunId, **fields: Any) -> None: ...


class LockProbe(Protocol):
    """The non-mutating lock read detection depends on.

    :meth:`civsim_harness.run.identity_lock.RunIdentityLock.inspect` satisfies it. A
    protocol rather than the class itself so a test can hand in a lock whose PID is
    alive without having to own a live process.
    """

    def inspect(self, run_id: RunId) -> LockInspection: ...


def _last_activity(store: OrphanScanStore, run: Run) -> tuple[Timestamp | None, str]:
    """The most recent moment this run is *known* to have done something, and whence.

    The run's own `started_at` and the `occurred_at` of its latest `RunEvent`, whichever
    is later. Events are the right proxy for "last step": every turn's FR-007 quicksave
    is a `save_taken` event and every lifecycle move is a `lifecycle_transition`, so a
    run that was mid-play has an event from within that turn.
    """
    latest: Timestamp | None = run.started_at
    source = "run.started_at" if latest is not None else "none"
    try:
        events = store.list_run_events(RunId(str(run.run_id)))
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort; see below
        # A read that fails must not turn a diagnosis into a crash: the finding simply
        # rests on the weaker evidence it already has, and says which.
        log_event(
            get_harness_logger(),
            logging.WARNING,
            "run/orphans: a run's events could not be read while dating its last activity",
            extra={"run_id": str(run.run_id), "error_type": type(exc).__name__},
        )
        return latest, source
    for event in events:
        if latest is None or event.occurred_at > latest:
            latest = event.occurred_at
            source = f"run_event.{event.event_type.value}"
    if latest is None:
        # A run that has not started and has recorded nothing is still *dated*: its
        # configuration was created when preparation began (`create_run` takes both).
        # That timestamp is what lets the grace window cover a run created seconds ago --
        # a live preparation between `create_run` and `acquire`, or a test composition
        # that never locks -- without leaving one created hours ago mis-stated.
        try:
            config = store.get_run_configuration(RunId(str(run.run_id)))
        except Exception as exc:  # noqa: BLE001 - same rule as the events read above
            log_event(
                get_harness_logger(),
                logging.WARNING,
                "run/orphans: a run's configuration could not be read while dating its "
                "last activity",
                extra={"run_id": str(run.run_id), "error_type": type(exc).__name__},
            )
            return latest, source
        if config is not None:
            latest = config.created_at
            source = "run_configuration.created_at"
    return latest, source


def scan_orphans(
    store: OrphanScanStore,
    *,
    lock: LockProbe,
    now: Timestamp,
    grace_seconds: float = DEFAULT_ORPHAN_GRACE_SECONDS,
    states: frozenset[LifecycleState] = ORPHAN_CANDIDATE_STATES,
    exclude: frozenset[RunId] = frozenset(),
) -> list[OrphanFinding]:
    """Every run in *states* that no live process is holding. Writes nothing.

    A run whose lock is held by a live PID is never returned, and a run idle for less
    than *grace_seconds* (where the store knows how long it has been idle at all) is
    never returned -- see this module's docstring on the preparation window. Runs in
    *exclude* are never candidates: the runner passes its own in-flight runs, which it
    is driving in this very process and whose lock it may not have claimed yet.
    """
    grace = timedelta(seconds=max(0.0, grace_seconds))
    findings: list[OrphanFinding] = []
    for run in store.list_active_runs():
        if run.lifecycle_state not in states:
            continue
        if RunId(str(run.run_id)) in exclude:
            continue  # this process is driving it; never a candidate
        inspection = lock.inspect(RunId(str(run.run_id)))
        if inspection.held_by_live_process:
            continue  # someone is playing this run; it is not ours to touch
        last_activity_at, source = _last_activity(store, run)
        idle_seconds: float | None = None
        if last_activity_at is not None:
            idle = now - last_activity_at
            if idle < grace:
                continue
            idle_seconds = idle.total_seconds()
        findings.append(
            OrphanFinding(
                run_id=RunId(str(run.run_id)),
                lifecycle_state=run.lifecycle_state,
                lock=inspection,
                last_activity_at=last_activity_at,
                last_activity_source=source,
                idle_seconds=idle_seconds,
                checked_at=now,
            )
        )
    return findings


def pause_orphan(
    store: OrphanRepairStore, finding: OrphanFinding, *, now: Timestamp
) -> RunEvent | None:
    """Transition *finding*'s run to `paused`, recording why. Returns the event written.

    Returns ``None`` when the run has moved on since it was scanned -- it reached a
    terminal state, or its own driver came back and advanced it -- because the state
    re-read here, inside the write, is the authority and the scan is only a candidate
    list. Nothing is written in that case.

    The event is written **before** the run row is updated, matching
    `run/runner.py`'s own `_advance`: a state change this codebase cannot explain must
    not be reachable, so if only one of the two writes lands it is the explanation.
    """
    run = store.get_run(finding.run_id)
    if run is None or run.lifecycle_state not in ORPHAN_CANDIDATE_STATES:
        return None
    updated_run, event = transition(
        run,
        LifecycleState.PAUSED,
        occurred_at=now,
        detail=finding.as_detail(),
    )
    store.write_run_event(event)
    store.update_run(finding.run_id, lifecycle_state=updated_run.lifecycle_state)
    _refresh_completeness(store, finding.run_id)
    log_event(
        get_harness_logger(),
        logging.WARNING,
        "run/orphans: an orphaned run was paused -- its driver is gone and nothing "
        "would have transitioned it",
        extra={
            "run_id": str(finding.run_id),
            "from": finding.lifecycle_state.value,
            "to": LifecycleState.PAUSED.value,
            **finding.as_detail(),
        },
    )
    return event


def _refresh_completeness(store: OrphanRepairStore, run_id: RunId) -> None:
    """Re-derive the run's `record_completeness_status` now that it is no longer `playing`.

    `store/sqlite_reads.py`'s `turn_gaps` forgives a trailing quicksave with no `TurnCycle`
    behind it *while a run is playing* (FR-007's normal in-flight shape). The whole point
    of pausing an orphan is that it is not playing, so the gap that rule was hiding must
    be re-derived -- the same moment `run/runner.py`'s `_finish` re-derives it (T239).

    Function-local import: `store/sqlite_adapter.py` imports this module, and
    `store/completeness.py` lives behind the same package `__init__`, so a top-level
    import here would be a cycle. Best-effort: a store narrower than `MatchStore` (a
    two-method fake) cannot be refreshed, and that is logged, not raised -- the pause is
    already recorded and the completeness field is re-derived on the next write anyway.
    """
    try:
        from civsim_harness.store.completeness import refresh_run_completeness

        refresh_run_completeness(store, run_id)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - see docstring
        log_event(
            get_harness_logger(),
            logging.WARNING,
            "run/orphans: record_completeness_status could not be re-derived after the pause",
            extra={"run_id": str(run_id), "error_type": type(exc).__name__},
        )


def repair_orphans(
    store: OrphanRepairStore,
    findings: Sequence[OrphanFinding],
    *,
    now: Timestamp,
) -> list[OrphanFinding]:
    """Apply :func:`pause_orphan` to each of *findings*; return those actually paused."""
    applied: list[OrphanFinding] = []
    for finding in findings:
        if pause_orphan(store, finding, now=now) is not None:
            applied.append(finding)
    return applied


def sweep_orphans(
    store: OrphanRepairStore,
    *,
    lock: LockProbe | None = None,
    now: Timestamp,
    grace_seconds: float = DEFAULT_ORPHAN_GRACE_SECONDS,
    exclude: frozenset[RunId] = frozenset(),
) -> list[OrphanFinding]:
    """Scan and repair in one call -- the automatic path, used on a write-mode store
    open and in the runner's start path. *exclude* is forwarded to :func:`scan_orphans`.

    *lock* defaults to a :class:`~civsim_harness.run.identity_lock.RunIdentityLock` over
    its own default directory, which is the same directory the harness locks in.

    This never raises. Neither caller is doing orphan repair as its purpose -- one is
    opening a store, the other is starting a run -- and a repair that cannot be
    performed must not become the error the operator sees instead of the thing they
    asked for. It is logged at `ERROR` and the caller proceeds, which is not silence:
    the unrepaired run is exactly as visible afterwards as it was before, and
    `civsim store repair` will still name it.
    """
    probe: LockProbe = lock if lock is not None else RunIdentityLock()
    try:
        findings = scan_orphans(
            store, lock=probe, now=now, grace_seconds=grace_seconds, exclude=exclude
        )
        return repair_orphans(store, findings, now=now)
    except Exception as exc:  # noqa: BLE001 - see docstring
        log_event(
            get_harness_logger(),
            logging.ERROR,
            "run/orphans: the orphaned-run sweep failed; any orphaned run is unchanged "
            "and still reported by `civsim store repair`",
            extra={"error_type": type(exc).__name__},
            exc_info=True,
        )
        return []


# --------------------------------------------------------------------------
# The lock side: a lock whose run never reached a store (2026-09-22)
# --------------------------------------------------------------------------

#: The lock directory's file-name convention, mirrored from
#: :meth:`~civsim_harness.run.identity_lock.RunIdentityLock._path_for`. Mirrored rather
#: than imported because it is private there and this module must not reach into it --
#: so `test_the_lock_file_naming_this_module_mirrors_is_the_one_the_lock_writes` pins the
#: two together and fails if either moves. A drift here would not raise: it would make
#: the scan silently see nothing, which is the failure mode this whole section exists to
#: end.
LOCK_FILE_SUFFIX = ".lock.json"


class StrayLockDisposition(StrEnum):
    """What a lock with no live run behind it is, and therefore what may be done to it.

    The three are ordered by how much they prove, and only the first two are enough to
    delete a file on:

    ``TERMINAL_RUN``
        The store has this run's row and it is `finished` or `failed`. Decisive whatever
        the PID says: a terminal run is over, nothing will drive it again, and its lock
        can only block the next run on that client.

    ``DEAD_HOLDER``
        The lock names a process that no longer exists (or is malformed and names none).
        Also decisive: a run cannot be in flight against a client process that is gone.
        PID reuse cannot turn this into a false positive -- a recycled PID reads as
        *alive*, which lands in the third case, not this one.

    ``UNRECORDED``
        The store has no row for this run id, and the lock names a live PID. This is
        today's wreck -- and it is **not** decisive, which is why it is reported and not
        deleted by default. The live PID is the *game client*, which outlives any number
        of drivers, so it says nothing about whether a driver is running. And `create_run`
        writes the run row before `acquire` takes the lock, so "no row" means the row went
        to a *different store* or was never written at all -- and this module cannot see
        the difference from here. `civsim store repair --clean-unrecorded-locks` is the
        operator saying they can.
    """

    TERMINAL_RUN = "run_already_terminal"
    DEAD_HOLDER = "lock_holder_dead"
    UNRECORDED = "run_not_in_store"


#: The dispositions :func:`clean_stray_locks` will act on without being asked twice.
CLEANABLE_DISPOSITIONS: frozenset[StrayLockDisposition] = frozenset(
    {StrayLockDisposition.TERMINAL_RUN, StrayLockDisposition.DEAD_HOLDER}
)

#: The `reason` every stray-lock log line carries, matching :data:`ORPHAN_REASON`'s role.
STRAY_LOCK_REASON = "stray_lock"


class LockDirectoryProbe(LockProbe, Protocol):
    """:class:`LockProbe` plus the directory those locks live in.

    Both members are already public on
    :class:`~civsim_harness.run.identity_lock.RunIdentityLock`. A probe that cannot name
    its directory (a two-line test fake) simply is not scannable from this side, and
    :func:`sweep_stray_locks` treats that as "nothing to report" rather than an error.
    """

    @property
    def lock_dir(self) -> Path: ...


class LockCleanupProbe(LockDirectoryProbe, Protocol):
    """:class:`LockDirectoryProbe` plus the one write :func:`clean_stray_locks` performs."""

    def release(self, run_id: RunId) -> None: ...


class StrayLockStore(Protocol):
    """The single read the lock-side scan needs, on the published `MatchStore` port.

    A read-only store satisfies it, which is what lets `--dry-run` and the web interface
    ask the question without a write-mode open.
    """

    def get_run(self, run_id: RunId) -> Run | None: ...


@dataclass(frozen=True)
class StrayLockFinding:
    """One lock file with no live run behind it, and everything checked to say so.

    Evidence, not an act -- the same contract as :class:`OrphanFinding`. The difference
    is where the evidence can be *recorded*: an orphaned run has a row to hang a
    `lifecycle_transition` event on, and a stray lock, by definition, does not. So this
    finding's record is :meth:`as_detail` on a `WARNING` through the harness log plus the
    `civsim store repair` listing. That is not a shortcut; it is the defect's own shape.
    Inventing a run row to explain the lock would assert a run this harness never saw.
    """

    run_id: RunId
    lock: LockInspection
    disposition: StrayLockDisposition
    run_state: LifecycleState | None
    age_seconds: float | None
    age_source: str
    checked_at: Timestamp

    @property
    def cleanable(self) -> bool:
        """Whether this lock may be removed without an operator saying so explicitly."""
        return self.disposition in CLEANABLE_DISPOSITIONS

    @property
    def why(self) -> str:
        """The one-phrase cause, for a listing line and for the log detail."""
        if self.disposition is StrayLockDisposition.TERMINAL_RUN:
            state = self.run_state.value if self.run_state else "terminal"
            return f"the run it names is recorded as {state}"
        if self.disposition is StrayLockDisposition.DEAD_HOLDER:
            if not self.lock.readable:
                return "the lock file is malformed and names no holder"
            return f"lock holder pid {self.lock.client_pid} is not alive"
        return "no run with this id is recorded in this store"

    def as_detail(self) -> dict[str, Any]:
        """The finding as a flat JSON payload for the log line that records it."""
        detail: dict[str, Any] = {
            "reason": STRAY_LOCK_REASON,
            "run_id": str(self.run_id),
            "stray_lock_disposition": self.disposition.value,
            "stray_lock_cause": self.why,
            "stray_lock_cleanable": self.cleanable,
            "run_state": self.run_state.value if self.run_state else None,
            "checked_at": self.checked_at.isoformat(),
            "lock_age_seconds": (
                round(self.age_seconds, 3) if self.age_seconds is not None else None
            ),
            "lock_age_source": self.age_source,
        }
        detail.update(self.lock.as_detail())
        return detail

    def render(self) -> str:
        """One operator-facing line: the lock, what it stands for, and what will happen."""
        age = "unknown" if self.age_seconds is None else f"{self.age_seconds:.0f}s"
        act = "remove" if self.cleanable else "report"
        return (
            f"{self.run_id:<38} {self.disposition.value:<22} -> {act}  "
            f"{self.why}; lock={self.lock.lock_path} "
            f"pid={self.lock.client_pid if self.lock.client_pid is not None else '-'} "
            f"alive={str(self.lock.pid_alive).lower()} age={age} ({self.age_source})"
        )


def _lock_age(inspection: LockInspection, *, now: Timestamp) -> tuple[float | None, str]:
    """How long this lock has existed, and on whose word.

    The lock's own `acquired_at` first; the file's mtime when the lock is malformed or its
    timestamp will not parse, so that a half-written file is still *dated* and the grace
    window still covers it. `None` when neither can be had -- and an undated lock is never
    cleanable, because the grace window is the only thing standing between this scan and a
    run that started half a second ago.
    """
    if inspection.acquired_at is not None:
        try:
            acquired = datetime.fromisoformat(inspection.acquired_at)
        except ValueError:
            acquired = None
        if acquired is not None:
            if acquired.tzinfo is None:
                acquired = acquired.replace(tzinfo=UTC)
            return (now - acquired).total_seconds(), "lock.acquired_at"
    try:
        mtime = inspection.lock_path.stat().st_mtime
    except OSError:
        return None, "none"
    return now.timestamp() - mtime, "lock_file.mtime"


def _recorded_run(store: StrayLockStore, run_id: RunId) -> tuple[Run | None, bool]:
    """This run's row and whether the store could be asked at all.

    The second value matters: a read that *failed* must never be read as "no such run",
    which is the one inference that would let this module delete a live run's lock.
    """
    try:
        return store.get_run(run_id), True
    except Exception as exc:  # noqa: BLE001 - a failed read weakens the finding, never widens it
        log_event(
            get_harness_logger(),
            logging.WARNING,
            "run/orphans: a stray lock's run could not be looked up; the lock is left alone",
            extra={"run_id": str(run_id), "error_type": type(exc).__name__},
        )
        return None, False


def _classify(
    inspection: LockInspection, run: Run | None, store_readable: bool
) -> StrayLockDisposition | None:
    """Which :class:`StrayLockDisposition` this lock is, or `None` for a healthy one.

    `None` covers the two cases that are not strays at all: a live claim on a run this
    store has in flight, and any lock at all when the store could not be read.
    """
    if run is not None and run.lifecycle_state in TERMINAL_STATES:
        return StrayLockDisposition.TERMINAL_RUN
    if not inspection.held_by_live_process:
        return StrayLockDisposition.DEAD_HOLDER
    if not store_readable:
        return None  # cannot distinguish "no row" from "could not look"; leave it be
    if run is None:
        return StrayLockDisposition.UNRECORDED
    return None  # a live process holding a run this store has in flight: healthy


def scan_stray_locks(
    store: StrayLockStore,
    *,
    lock: LockDirectoryProbe,
    now: Timestamp,
    grace_seconds: float = DEFAULT_ORPHAN_GRACE_SECONDS,
) -> list[StrayLockFinding]:
    """Every lock file in *lock*'s directory that no live run stands behind. Writes nothing.

    The mirror of :func:`scan_orphans`: that one walks run rows and asks about their locks,
    this one walks lock files and asks about their runs. A lock younger than
    *grace_seconds* is never returned on any evidence -- the window that covers a run
    between `create_run` and `acquire` covers a lock between `acquire` and its first
    recorded step just as well.
    """
    grace = max(0.0, grace_seconds)
    directory = lock.lock_dir
    try:
        paths = sorted(directory.glob(f"*{LOCK_FILE_SUFFIX}"))
    except OSError:
        return []
    findings: list[StrayLockFinding] = []
    for path in paths:
        run_id = RunId(path.name[: -len(LOCK_FILE_SUFFIX)])
        inspection = lock.inspect(run_id)
        if not inspection.present:
            continue  # released between the listing and the read; nothing to report
        age_seconds, age_source = _lock_age(inspection, now=now)
        if age_seconds is None or age_seconds < grace:
            continue
        run, store_readable = _recorded_run(store, run_id)
        disposition = _classify(inspection, run, store_readable)
        if disposition is None:
            continue
        findings.append(
            StrayLockFinding(
                run_id=run_id,
                lock=inspection,
                disposition=disposition,
                run_state=run.lifecycle_state if run is not None else None,
                age_seconds=age_seconds,
                age_source=age_source,
                checked_at=now,
            )
        )
    return findings


def clean_stray_locks(
    findings: Sequence[StrayLockFinding],
    *,
    lock: LockCleanupProbe,
    include_unrecorded: bool = False,
) -> list[StrayLockFinding]:
    """Remove the lock files in *findings* that may be removed; return those removed.

    `cleanable` findings always; `UNRECORDED` ones only when *include_unrecorded* -- see
    :class:`StrayLockDisposition` for why that one needs an operator behind it.

    Each file is re-inspected here, inside the removal, and skipped if it has changed
    since the scan: a lock released and re-acquired in between is a *new* claim, and the
    scan's verdict was about the old one. Same rule as :func:`pause_orphan`'s re-read --
    the scan produces candidates, the act re-establishes the facts.
    """
    removed: list[StrayLockFinding] = []
    for finding in findings:
        if not (finding.cleanable or include_unrecorded):
            continue
        current = lock.inspect(finding.run_id)
        if (
            not current.present
            or current.client_pid != finding.lock.client_pid
            or current.acquired_at != finding.lock.acquired_at
        ):
            log_event(
                get_harness_logger(),
                logging.INFO,
                "run/orphans: a stray lock changed between the scan and the removal and was "
                "left alone",
                extra={"run_id": str(finding.run_id), **finding.as_detail()},
            )
            continue
        lock.release(finding.run_id)
        log_event(
            get_harness_logger(),
            logging.WARNING,
            "run/orphans: a run-identity lock with no live run behind it was removed",
            extra=finding.as_detail(),
        )
        removed.append(finding)
    return removed


def sweep_stray_locks(
    store: StrayLockStore,
    *,
    lock: LockProbe | None = None,
    now: Timestamp,
    grace_seconds: float = DEFAULT_ORPHAN_GRACE_SECONDS,
) -> list[StrayLockFinding]:
    """Scan for stray locks and *record* them. Removes nothing, and never raises.

    The automatic counterpart to :func:`sweep_orphans`, and deliberately not its equal:
    this one reports and stops. A store open is one process's intention to use one store,
    while the lock directory is shared by every store on the host -- so an open is
    entitled to say "a lock here stands for no run I can see" and is not entitled to act
    on it. `civsim store repair` is where acting happens, with an operator behind it.

    What it buys is the thing that was missing on 2026-09-21/22: the next run's driver log
    names the lock that is about to refuse it, by path and PID, instead of the run simply
    failing to start with no diagnosis.

    *lock* defaults to a :class:`~civsim_harness.run.identity_lock.RunIdentityLock` over
    the harness's own directory. A probe that cannot name its directory (a test fake) is
    not scannable and yields an empty list.
    """
    probe: LockProbe = lock if lock is not None else RunIdentityLock()
    if not hasattr(probe, "lock_dir"):
        return []
    try:
        findings = scan_stray_locks(
            store, lock=probe, now=now, grace_seconds=grace_seconds  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        log_event(
            get_harness_logger(),
            logging.ERROR,
            "run/orphans: the stray run-identity lock scan failed; `civsim store repair "
            "--dry-run` will still list them",
            extra={"error_type": type(exc).__name__},
            exc_info=True,
        )
        return []
    for finding in findings:
        log_event(
            get_harness_logger(),
            logging.WARNING,
            "run/orphans: a run-identity lock stands for no run this store can see -- it will "
            "refuse the next run on that client pid; `civsim store repair --dry-run`",
            extra=finding.as_detail(),
        )
    return findings


__all__ = [
    "CLEANABLE_DISPOSITIONS",
    "DEFAULT_ORPHAN_GRACE_SECONDS",
    "LOCK_FILE_SUFFIX",
    "ORPHAN_CANDIDATE_STATES",
    "ORPHAN_REASON",
    "STRAY_LOCK_REASON",
    "LockCleanupProbe",
    "LockDirectoryProbe",
    "LockProbe",
    "OrphanFinding",
    "OrphanRepairStore",
    "OrphanScanStore",
    "StrayLockDisposition",
    "StrayLockFinding",
    "StrayLockStore",
    "clean_stray_locks",
    "pause_orphan",
    "repair_orphans",
    "scan_orphans",
    "scan_stray_locks",
    "sweep_orphans",
    "sweep_stray_locks",
]
