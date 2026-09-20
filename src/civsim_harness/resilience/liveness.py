"""Process liveness monitor (T147).

research R12's first signal: `psutil` against the game client's own PID,
catching a hard crash or process exit. Deliberately the simplest of the
four signals -- it answers exactly one question, "is this PID still a live
process right now", and nothing about how long it has been running or how
long the current turn has taken. There is no duration anywhere in this
module: liveness is a point-in-time check, re-evaluated fresh every time a
caller asks (research R12, FR-014).

A zombie process (exited but not yet reaped by its parent) is treated as
*not* alive: it is no longer doing anything on behalf of the game, even
though `psutil.pid_exists` still reports it present.
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil


def is_process_alive(pid: int) -> bool:
    """Whether *pid* is currently a live, non-zombie process.

    A point-in-time check only -- callers re-invoke it whenever they need a
    fresh answer. Never raises: a PID that no longer exists, or that
    briefly races between `pid_exists` and `Process()` (e.g. it exits in
    between the two calls), is reported as not-alive rather than as an
    error, matching the `host.port` pattern of reporting an outcome rather
    than propagating a transient OS race as an exception.
    """
    if not psutil.pid_exists(pid):
        return False
    try:
        process = psutil.Process(pid)
        return bool(process.status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


@dataclass(frozen=True)
class ProcessLivenessMonitor:
    """Binds `is_process_alive` to one client PID for repeated checks.

    A thin, stateless wrapper (no cached result, no last-checked timestamp)
    so that nothing here can accidentally start measuring elapsed time
    between checks -- every call to :meth:`check` is an independent,
    fresh answer (research R12).
    """

    pid: int

    def check(self) -> bool:
        """Whether the bound PID is currently a live, non-zombie process."""
        return is_process_alive(self.pid)
