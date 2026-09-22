"""`RunIdentityLock`'s `SIGTERM` backstop (T227/T289 follow-on, 2026-09-22).

`RunLockHandle.commit()` arms an `atexit` backstop so a committed lock is released even if
none of `Runner._play_run`'s several terminal paths ever call back into `composition.py`'s
own release points. That backstop does not fire for `SIGTERM`: Python's default `SIGTERM`
disposition terminates the process WITHOUT running `atexit` handlers, and `timeout` -- this
project's own standing rule for bounding every long-running command -- sends `SIGTERM` by
default. So every `timeout 580 ... goal_run` / `timeout 900 ... pytest` in the standing
rules generated exactly the uncovered kill.

`test_sigterm_releases_a_committed_lock_the_atexit_backstop_alone_cannot_reach` is the
honest shape for that: a real subprocess acquires and commits a lock, is sent a real
`SIGTERM`, and the test asserts both that the lock file is gone and that the process still
died BY the signal (not swallowed). The rest of this file tests the handler-installation
bookkeeping directly (no real signal needed): that release restores whatever `SIGTERM`
disposition preceded ours, that a pre-existing handler is chained rather than clobbered,
and that two overlapping held locks do not each try to restore a stale handler.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from civsim_harness.models.common import RunId
from civsim_harness.run import identity_lock as identity_lock_module
from civsim_harness.run.identity_lock import RunIdentityLock

# The child process: acquires and *commits* a lock (mirroring real usage -- a run's lock
# outlives the `with` scope that claimed it), then signals readiness by writing a file, then
# waits to be signalled. `sys.argv` (with `-c`, argv[0] is "-c") carries the lock directory,
# run id, and ready-file path so nothing needs string interpolation into the script itself.
_CHILD_SCRIPT = """
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from civsim_harness.models.common import RunId
from civsim_harness.run.identity_lock import RunIdentityLock

lock_dir = Path(sys.argv[1])
run_id = RunId(sys.argv[2])
ready_file = Path(sys.argv[3])

lock = RunIdentityLock(lock_dir=lock_dir)
with lock.guard(run_id=run_id, client_pid=os.getpid(), now=datetime.now(UTC)) as handle:
    handle.commit()
    ready_file.write_text("ready", encoding="utf-8")
    while True:
        time.sleep(0.05)
"""


def _wait_for(predicate, *, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for: {description}")
        time.sleep(0.02)


def test_sigterm_releases_a_committed_lock_the_atexit_backstop_alone_cannot_reach(
    tmp_path: Path,
) -> None:
    """The subprocess shape: acquire + commit, real `SIGTERM`, lock file gone, process
    actually died by the signal.

    This is the test that fails without the fix -- with no `SIGTERM` handler installed,
    Python's default disposition kills the child before `atexit` ever runs, so the lock
    file survives it. Confirmed by reverting `identity_lock.py` to its pre-fix content
    (a backup copy, not `git stash`/`reset`) and re-running this exact test: it failed on
    `assert not lock_path.exists()` -- the lock file was still there after the child died.
    """
    lock_dir = tmp_path / "locks"
    ready_file = tmp_path / "ready"
    run_id = "run-sigterm-subprocess"
    lock_path = lock_dir / f"{run_id}.lock.json"

    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT, str(lock_dir), run_id, str(ready_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for(
            ready_file.exists,
            timeout=10.0,
            description="child to acquire+commit the lock and signal readiness",
        )
        assert lock_path.exists(), "sanity: the child's lock file must exist before SIGTERM"

        proc.send_signal(signal.SIGTERM)
        try:
            returncode = proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10.0)
            raise AssertionError(
                f"child did not exit after SIGTERM; output so far:\n{proc.stdout.read()}"
            ) from None

        # -- load-bearing assertions --
        assert not lock_path.exists(), (
            "SIGTERM must not leak the lock file -- this is the exact gap the atexit-only "
            "backstop could not close, since Python's default SIGTERM disposition skips "
            "atexit handlers entirely"
        )
        assert returncode == -signal.SIGTERM, (
            "a caught SIGTERM must still end the process exactly as the caller expects "
            f"(terminated BY the signal, returncode == -{signal.SIGTERM}); got {returncode} "
            f"-- a handler that swallows SIGTERM is worse than no handler. Output:\n"
            f"{proc.stdout.read() if proc.stdout else ''}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)


def test_release_restores_the_pre_existing_sigterm_handler(tmp_path: Path) -> None:
    """`RunIdentityLock.release` must put back whatever `SIGTERM` disposition preceded the
    one it installed at `acquire` -- not the interpreter default unconditionally, and not
    left as ours forever.
    """
    lock_dir = tmp_path / "locks"
    run_id = RunId("run-restore")
    original = signal.getsignal(signal.SIGTERM)
    try:
        lock = RunIdentityLock(lock_dir=lock_dir)
        lock.acquire(run_id=run_id, client_pid=os.getpid(), now=datetime.now(UTC))

        assert signal.getsignal(signal.SIGTERM) is identity_lock_module._sigterm_handler

        lock.release(run_id)

        # -- load-bearing assertion --
        assert signal.getsignal(signal.SIGTERM) is original, (
            "release() must restore exactly the disposition that was in effect before "
            "acquire() installed ours"
        )
    finally:
        signal.signal(signal.SIGTERM, original)


def test_sigterm_handler_chains_to_a_pre_existing_handler_instead_of_clobbering_it(
    tmp_path: Path,
) -> None:
    """If a `SIGTERM` handler was already installed before this module's, our handler must
    call it after releasing -- not clobber it, and not silently drop back to default
    disposition when there was something else to hand back to.
    """
    lock_dir = tmp_path / "locks"
    run_id = RunId("run-chain")
    original = signal.getsignal(signal.SIGTERM)
    calls: list[tuple[int, object]] = []

    def previous_handler(signum: int, frame: object) -> None:
        calls.append((signum, frame))

    try:
        signal.signal(signal.SIGTERM, previous_handler)

        lock = RunIdentityLock(lock_dir=lock_dir)
        lock.acquire(run_id=run_id, client_pid=os.getpid(), now=datetime.now(UTC))
        lock_path = lock_dir / f"{run_id}.lock.json"
        assert lock_path.exists()

        # Simulate the signal arriving while held. Calling the handler function directly
        # (rather than sending a real SIGTERM) is deliberate: this handler's own contract
        # is just "a callable taking (signum, frame)", and a direct call keeps this test
        # from ever risking this pytest process's own life if chaining went wrong.
        identity_lock_module._sigterm_handler(signal.SIGTERM, None)

        # -- load-bearing assertions --
        assert not lock_path.exists(), "the handler must still release the lock before chaining"
        assert calls == [(signal.SIGTERM, None)], (
            "the pre-existing handler must be called exactly once, not skipped (clobbered) "
            "and not called more than once"
        )
        assert signal.getsignal(signal.SIGTERM) is previous_handler, (
            "disposition must be restored to the pre-existing handler, not left as ours"
        )
    finally:
        signal.signal(signal.SIGTERM, original)


def test_overlapping_locks_only_the_outermost_release_restores_the_original_handler(
    tmp_path: Path,
) -> None:
    """Two overlapping held locks share one process-wide SIGTERM disposition. Releasing the
    inner one first must not restore the pre-civsim handler while the outer lock is still
    held -- only the last release (outermost, in acquisition order here) may do that.
    """
    lock_dir = tmp_path / "locks"
    outer_id = RunId("run-outer")
    inner_id = RunId("run-inner")
    original = signal.getsignal(signal.SIGTERM)
    try:
        outer = RunIdentityLock(lock_dir=lock_dir)
        inner = RunIdentityLock(lock_dir=lock_dir)

        # `acquire` refuses two run identities on the *same* client PID (V8, research R4),
        # so this simulates two genuinely distinct clients with two different -- but real,
        # live -- PIDs, the same trick `test_stray_lock_repair.py` uses.
        outer.acquire(run_id=outer_id, client_pid=os.getpid(), now=datetime.now(UTC))
        assert signal.getsignal(signal.SIGTERM) is identity_lock_module._sigterm_handler

        inner.acquire(run_id=inner_id, client_pid=os.getppid(), now=datetime.now(UTC))
        assert signal.getsignal(signal.SIGTERM) is identity_lock_module._sigterm_handler

        inner.release(inner_id)
        # -- load-bearing assertion --
        assert signal.getsignal(signal.SIGTERM) is identity_lock_module._sigterm_handler, (
            "releasing the inner lock while the outer one is still held must not restore "
            "the stale pre-civsim handler -- our handler must stay installed"
        )

        outer.release(outer_id)
        assert signal.getsignal(signal.SIGTERM) is original, (
            "releasing the last held lock must restore the real original handler"
        )
    finally:
        signal.signal(signal.SIGTERM, original)
