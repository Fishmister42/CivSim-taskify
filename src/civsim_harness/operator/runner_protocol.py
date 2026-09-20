"""`RunnerProtocol` -- the narrow seam T118's CLI and T119's HTTP API need from a
run orchestrator.

`src/civsim_harness/run/runner.py` (T116) does not exist yet in this wave -- it
is a later wave's task, owned by another agent. Rather than block the operator
surface on it, or invent a stand-in implementation this wave has no authority
to design, this module states the **contract** the CLI and the HTTP API are
written against: whatever T116 produces must satisfy this `Protocol` (structural
typing -- `@runtime_checkable` means an `isinstance()` check works too, but nothing
here requires inheriting from it explicitly).

**Design decisions the runner author should read before implementing:**

1. **Synchronous, thread-safe control surface.** Every method here is a plain
   (non-async) call. The actual play loop is necessarily asyncio-heavy (Nexus is
   an asyncio client), but a Typer CLI invocation and a FastAPI request handler
   both need to reach a *running* asyncio loop from outside it -- the natural
   shape is a runner that owns its own event loop (in a background thread or
   process) and exposes a synchronous, thread-safe control interface for
   start/pause/resume/stop/status, matching this Protocol. That is an
   implementation detail of whatever concretely satisfies `RunnerProtocol`; this
   module only pins the seam's shape.

2. **`start` blocks through preparation, not through play.** Per
   contracts/operator-surface.md, `run start` "validate, preflight, prepare,
   begin playing" -- the CLI/HTTP call is expected to return once the run exists
   and has begun playing (turn 1 underway or already failed at preflight), not
   once the run reaches its stop condition. Play then continues in the
   background; `status`/`pause`/etc. are how a caller interacts with it from
   then on.

3. **`start`'s two distinct failure shapes, per the contract's error table:**
   - *Configuration invalid* ("rejected; nothing prepared") -- no run was ever
     created, so there is no `RunId` to return: implementations must raise a
     `civsim_harness.errors.HarnessError` (or a fitting subtype, e.g.
     `PreflightError`) in this case, and callers (this module's CLI/API) treat
     any raised `HarnessError` as "nothing started."
   - *Preflight mismatch, model chain failure, undeclared capability, disk
     headroom, host tier unsupported*, etc. ("run created in `failed` state ...
     no turn 1") -- a `Run` record *does* exist here, so implementations return
     its `RunId` normally; the caller discovers the failure by calling
     `get_status` and finding `lifecycle_state == LifecycleState.FAILED`.

4. **`request_pause` / `request_resume` / `request_stop` do not block until the
   command lands.** Pause and stop take effect at a turn boundary, never
   mid-turn (FR-004, FR-008, SC-022) -- a turn has no time bound, so a pause
   requested during a long turn may wait a long time to land, and the harness
   must never cut the turn short to honour it sooner. These three methods are
   the "request" half only: they return once the request has been accepted (and,
   by the time `operator/commands.py` calls them, already durably recorded as a
   `lifecycle_command_received` event -- see that module), not once the
   requested transition has actually happened. `get_status(run_id)
   .requested_state` is how a caller observes that a request is still pending.
   Implementations should raise `HarnessError` for a request that makes no
   sense given the run's current lifecycle state (e.g. `resume` on a run that
   is not paused, or any command naming an unknown `run_id`).

5. **`get_status` returns the exact `RunStatusView` both surfaces serve.** One
   method, one shape, shared verbatim by the CLI's `run status` and the HTTP
   API's `GET /runs/{id}/status` -- this is what keeps the two forms of the
   contract from drifting into two different presentations of the same run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from civsim_harness.models.common import RunId
from civsim_harness.operator.schemas import RunStatusView

__all__ = ["RunnerProtocol"]


@runtime_checkable
class RunnerProtocol(Protocol):
    """What `operator/commands.py`, `operator/cli.py`, and `operator/api.py`
    need from a run orchestrator. See the module docstring for the semantics
    each method must honour -- this is a structural shape, not a full spec.
    """

    def start(self, config_path: Path) -> RunId:
        """Validate, preflight, prepare, and begin playing *config_path*.

        Returns the new run's id once it exists and play has begun (even a run
        that immediately landed in `failed` at preflight, per D2 above).
        Raises `civsim_harness.errors.HarnessError` when no run could be
        created at all (invalid configuration -- nothing prepared).
        """
        ...

    def request_pause(self, run_id: RunId) -> None:
        """Request that *run_id* pause at its next turn boundary.

        Non-blocking: returns once the request is accepted, not once the run
        has actually paused. Raises `HarnessError` for an unknown `run_id` or
        a run for which pausing is not a legal request right now.
        """
        ...

    def request_resume(self, run_id: RunId) -> None:
        """Request that a paused *run_id* resume playing.

        Non-blocking in the same sense as `request_pause`. Raises
        `HarnessError` for an unknown `run_id` or a run that is not paused.
        """
        ...

    def request_stop(self, run_id: RunId) -> None:
        """Request that *run_id* stop at its next turn boundary, recording
        `stop_resolution = operator_stop`.

        Non-blocking in the same sense as `request_pause`. Raises
        `HarnessError` for an unknown `run_id` or a run already in a terminal
        lifecycle state.
        """
        ...

    def get_status(self, run_id: RunId) -> RunStatusView:
        """Return *run_id*'s current lifecycle/diagnostic snapshot.

        Raises `HarnessError` for an unknown `run_id`. Never returns anything
        beyond the closed `RunStatusView` shape (FR-053) -- see
        `operator/schemas.py`.
        """
        ...
