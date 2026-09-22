"""Run-identity lock (T075, FR-006, V8, research R4).

Civ VI accepts exactly one FireTuner client at a time, so a second harness
instance attaching to the *same client* already fails loudly at connect time
-- that is the game's own enforcement of FR-006, and this module does not
duplicate it. What the game's limit does not cover is two harness instances
pointed at the *same run identity* against *different* clients, which would
produce two divergent records under one ``run_id``. A filesystem lock file,
keyed to both the client PID and the run ID, closes that gap -- deliberately
lightweight (R4's own "alternatives considered" rejects a coordination
service as disproportionate for a single-machine deliverable whose
parallelism story is "run more machines").

A lock whose recorded process is no longer alive is stale and is cleared
rather than left to wedge that run identity forever -- a crashed harness must
not permanently block its own run id from ever restarting, but a live one
must never be raced.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psutil

from civsim_harness.errors import PreflightError
from civsim_harness.models.common import RunId, Timestamp

DEFAULT_LOCK_DIR = Path(tempfile.gettempdir()) / "civsim_harness" / "run_locks"


class RunIdentityLockError(PreflightError):
    """A run is already active on this client PID or this run identity (FR-006, V8, R4)."""


@dataclass(frozen=True)
class ActiveRunLock:
    """One held run-identity lock: which run, attached to which client PID."""

    run_id: RunId
    client_pid: int
    acquired_at: str
    lock_path: Path


@dataclass(frozen=True)
class LockInspection:
    """What one run identity's lock file says, read **without changing anything**.

    :meth:`RunIdentityLock.acquire`'s own scan clears a stale or malformed lock as it
    goes -- correct there, because acquisition is already a write and a wedged run id is
    the failure it exists to prevent. A *diagnosis* must not: orphan detection
    (``run/orphans.py``) reports the lock as evidence for a lifecycle transition it is
    about to record, and evidence that the act of looking has already erased is not
    evidence. So this is a pure read, and every field below is a fact the caller may
    quote back into a ``RunEvent``.

    ``present`` is whether the file exists at all; ``readable`` is false for a malformed
    or half-written one (whose ``client_pid`` is then ``None``); ``pid_alive`` is
    ``psutil.pid_exists`` on the recorded PID -- the Civ VI client process the run was
    keyed to, which is the only liveness fact the lock carries.
    """

    run_id: RunId
    lock_path: Path
    present: bool
    readable: bool
    client_pid: int | None
    pid_alive: bool
    acquired_at: str | None

    @property
    def held_by_live_process(self) -> bool:
        """Whether this lock is a live claim on the run identity.

        True only when the file is there, parses, names a PID, and that PID still
        exists. Anything else -- absent, malformed, or naming a dead process -- is not
        a claim anyone is currently honouring.
        """
        return self.present and self.readable and self.client_pid is not None and self.pid_alive

    def as_detail(self) -> dict[str, object]:
        """This inspection as plain JSON for a ``RunEvent.detail`` (Principle VII)."""
        return {
            "lock_path": str(self.lock_path),
            "lock_present": self.present,
            "lock_readable": self.readable,
            "lock_client_pid": self.client_pid,
            "lock_client_pid_alive": self.pid_alive,
            "lock_acquired_at": self.acquired_at,
        }


class RunLockHandle:
    """One `with RunIdentityLock.guard(...)` scope's own handle on the lock it just acquired
    (T227/T289).

    **Why this exists.** Before this class, `acquire` and `release` were two calls a caller had
    to remember to pair up itself, with nothing enforcing it -- `run/composition.py` acquired once
    and released from three independently conditional branches, and any exit that reached none of
    them (an exception raised by a live read, a store write, or anything else between acquire and
    the run actually being handed off to play) leaked the lock file forever, silently blocking
    every later run under that run id. `RunIdentityLock.guard` closes that gap by acquiring and
    handing back one of these, whose `__exit__` (via `guard`'s own `try`/`finally`) releases
    on *every* exit from that `with` block -- normal return, an early `return`, a raised
    exception, or a cancelled task -- unless the caller has explicitly `commit()`-ed first.

    **Why `commit()` exists at all.** A run's lock is claimed once, at preparation, but is meant to
    stay held for as long as the run keeps playing turns -- long after the `with` block that
    claimed it has returned. `commit()` is how a caller says "this lock's lifetime is no longer
    this guard's to manage" -- once called, `__exit__` leaves the lock exactly as it is, and
    releasing it becomes some *other*, later call's job (`run/composition.py`'s own
    `evaluate_stop_facts`, when the run reaches a stop resolution, or the terminal-run sweep in
    `_release_terminal_run_clients` as a backstop for the paths that never reach that). A caller
    that never commits -- because preparation failed, or raised -- gets the lock released
    automatically, which is the whole point.

    **Double release is safe, by construction.** `release()` here is idempotent (tracked by
    `_released`, on top of `RunIdentityLock.release` itself already unlinking with
    ``missing_ok=True``), so a caller that explicitly releases early (e.g.
    `run/composition.py`'s `_fail_preparation`, which releases the moment a run is recorded
    `failed` so a corrected re-run is not refused) and then lets this handle's own `__exit__`
    backstop run too costs nothing: the second call sees `_released` already `True` and returns
    without touching the filesystem again. Nothing here can turn into releasing a *different*,
    later acquisition of the same run id out from under its rightful holder, because the handle
    never re-arms itself -- once released or committed, it stays that way for its whole life.
    """

    def __init__(self, lock: RunIdentityLock, run_id: RunId | None) -> None:
        # *run_id* is None exactly when this handle stands in for a "no client PID to key the
        # lock to" skip (see `RunIdentityLock.guard`'s own docstring) -- nothing was ever
        # acquired, so `commit()`/`release()` below are deliberately no-ops for it.
        self._lock = lock
        self._run_id = run_id
        self._committed = False
        self._released = False

    def commit(self) -> None:
        """Hand this lock's remaining lifetime to the run it was claimed for. After this call,
        the `with run_lock.guard(...)` scope that produced this handle will NOT release the lock
        when it exits -- call this only once the run is genuinely going to keep needing the lock
        past that scope (registered somewhere a later, real release will find it)."""
        self._committed = True

    def release(self) -> None:
        """Release the lock now. Safe to call more than once, and safe to call whether or not
        `commit()` was ever reached -- see this class's own docstring on why a second call, from
        anywhere, is always a harmless no-op."""
        if self._released or self._run_id is None:
            return
        self._lock.release(self._run_id)
        self._released = True


class RunIdentityLock:
    """Filesystem-backed lock manager: one lock file per ``run_id``, recording
    the client PID it is attached to.

    ``acquire`` refuses a second attach to either an already-active client PID
    or an already-active run identity (V8). Locks whose recorded process is no
    longer alive, or whose file is unreadable/malformed, are treated as stale
    and cleared automatically -- they cannot represent an active run.
    """

    def __init__(self, lock_dir: Path = DEFAULT_LOCK_DIR) -> None:
        self._lock_dir = lock_dir

    def acquire(self, *, run_id: RunId, client_pid: int, now: Timestamp) -> ActiveRunLock:
        """Acquire the lock for *run_id* attached to *client_pid*.

        Raises :class:`RunIdentityLockError` when a still-live lock already
        holds this run identity, or already holds this client PID under a
        *different* run identity.
        """
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        for existing in self._active_locks():
            if existing.run_id == run_id:
                raise RunIdentityLockError(
                    "a run is already active under this run identity (FR-006, V8)",
                    detail={
                        "run_id": str(run_id),
                        "existing_client_pid": existing.client_pid,
                    },
                )
            if existing.client_pid == client_pid:
                raise RunIdentityLockError(
                    "a run is already active on this client (FR-006, V8, research R4)",
                    detail={
                        "client_pid": client_pid,
                        "existing_run_id": str(existing.run_id),
                    },
                )

        lock = ActiveRunLock(
            run_id=run_id,
            client_pid=client_pid,
            acquired_at=now.isoformat(),
            lock_path=self._path_for(run_id),
        )
        self._write(lock)
        return lock

    def release(self, run_id: RunId) -> None:
        """Release *run_id*'s lock, if held. A no-op if it is not."""
        self._path_for(run_id).unlink(missing_ok=True)

    @contextmanager
    def guard(
        self, *, run_id: RunId, client_pid: int | None, now: Timestamp
    ) -> Iterator[RunLockHandle]:
        """Acquire *run_id*'s lock and guarantee it is released on every exit from the `with`
        block this opens, unless the caller `commit()`s the yielded :class:`RunLockHandle` first
        (T227/T289 -- see that class's own docstring for the full reasoning and the double-release
        story).

        *client_pid* may be `None` -- the caller had no client process to key the lock to
        (`run/composition.py`'s own documented skip: "the lock is skipped rather than keyed to a
        fabricated one"). Nothing is acquired in that case, and the yielded handle's `commit`/
        `release` are both no-ops, so a caller may use this the same way regardless of whether it
        has a PID to offer.

        This is a thin wrapper over :meth:`acquire`/:meth:`release`, not a new locking mechanism
        -- `acquire`'s own semantics (refusing a second attach to a live run identity or client
        PID) are unchanged; this only adds the missing guarantee that whatever it acquires is
        always given back.
        """
        if client_pid is None:
            yield RunLockHandle(self, None)
            return
        self.acquire(run_id=run_id, client_pid=client_pid, now=now)
        handle = RunLockHandle(self, run_id)
        try:
            yield handle
        finally:
            if not handle._committed:
                handle.release()

    def is_active(self, run_id: RunId) -> bool:
        """Whether *run_id* currently holds a live lock."""
        return any(lock.run_id == run_id for lock in self._active_locks())

    @property
    def lock_dir(self) -> Path:
        """The directory these locks live in -- quoted as evidence, never written here."""
        return self._lock_dir

    def inspect(self, run_id: RunId) -> LockInspection:
        """Read *run_id*'s lock file and report it, changing nothing (see
        :class:`LockInspection`).

        Unlike :meth:`is_active`, this never unlinks a stale or malformed file: it is the
        read orphan detection quotes into the transition it records, and it must be
        possible to ask the same question twice and get the same answer.
        """
        path = self._path_for(run_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return LockInspection(
                run_id=run_id,
                lock_path=path,
                present=False,
                readable=False,
                client_pid=None,
                pid_alive=False,
                acquired_at=None,
            )
        except OSError:
            return LockInspection(
                run_id=run_id,
                lock_path=path,
                present=True,
                readable=False,
                client_pid=None,
                pid_alive=False,
                acquired_at=None,
            )
        try:
            data = json.loads(raw)
            client_pid = int(data["client_pid"])
            acquired_at = str(data["acquired_at"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return LockInspection(
                run_id=run_id,
                lock_path=path,
                present=True,
                readable=False,
                client_pid=None,
                pid_alive=False,
                acquired_at=None,
            )
        return LockInspection(
            run_id=run_id,
            lock_path=path,
            present=True,
            readable=True,
            client_pid=client_pid,
            pid_alive=psutil.pid_exists(client_pid),
            acquired_at=acquired_at,
        )

    def _path_for(self, run_id: RunId) -> Path:
        return self._lock_dir / f"{run_id}.lock.json"

    def _active_locks(self) -> list[ActiveRunLock]:
        active: list[ActiveRunLock] = []
        if not self._lock_dir.is_dir():
            return active
        for path in sorted(self._lock_dir.glob("*.lock.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                run_id = RunId(str(data["run_id"]))
                client_pid = int(data["client_pid"])
                acquired_at = str(data["acquired_at"])
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                # A malformed or half-written lock file cannot represent an
                # active run -- remove it rather than let it wedge
                # acquisition forever.
                path.unlink(missing_ok=True)
                continue
            if not psutil.pid_exists(client_pid):
                path.unlink(missing_ok=True)
                continue
            active.append(
                ActiveRunLock(
                    run_id=run_id,
                    client_pid=client_pid,
                    acquired_at=acquired_at,
                    lock_path=path,
                )
            )
        return active

    def _write(self, lock: ActiveRunLock) -> None:
        payload = {
            "run_id": str(lock.run_id),
            "client_pid": lock.client_pid,
            "acquired_at": lock.acquired_at,
        }
        lock.lock_path.write_text(json.dumps(payload), encoding="utf-8")
