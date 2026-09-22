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
