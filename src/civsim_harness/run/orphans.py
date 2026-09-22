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
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from civsim_harness.models.common import EventId, RunId, Timestamp
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.records import RunEvent
from civsim_harness.models.run import LifecycleState, Run
from civsim_harness.run.identity_lock import LockInspection, RunIdentityLock
from civsim_harness.run.lifecycle import transition
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


__all__ = [
    "DEFAULT_ORPHAN_GRACE_SECONDS",
    "ORPHAN_CANDIDATE_STATES",
    "ORPHAN_REASON",
    "LockProbe",
    "OrphanFinding",
    "OrphanRepairStore",
    "OrphanScanStore",
    "pause_orphan",
    "repair_orphans",
    "scan_orphans",
    "sweep_orphans",
]
