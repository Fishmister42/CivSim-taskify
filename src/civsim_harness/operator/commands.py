"""Command handling: record-before-effect, and the boundary semantics for
pause/stop (T120; contracts/operator-surface.md, FR-004, FR-008, SC-022).

Every command that targets an existing run is durably recorded as a
`lifecycle_command_received` `RunEvent` **before** it is forwarded to the
runner -- so the timeline shows what was *asked* as well as what happened, even
if the runner rejects the request or takes a long time to honour it. The order
is the whole point: write the event, then call the runner, never the reverse.

**Why `start` is not recorded here.** A `lifecycle_command_received` event
requires a `run_id` (see `models.records.RunEvent`), and `start` is the one
command that does not have one until the runner's own preparation path creates
the `Run` it names. Recording "a start was requested" before a run exists would
mean inventing a placeholder id or a different event shape neither
`RunEvent` nor this module's scope covers. `start` is therefore a thin pass
through to `RunnerProtocol.start` -- whatever preparation-time instrumentation
`run/preparation.py` (another wave) writes is that module's concern, not this
one's.

**Pause and stop land on a turn boundary, never mid-turn.** This module never
tries to make that happen faster or more directly than the runner allows: it
records the request, forwards it via `RunnerProtocol.request_pause` /
`request_stop` (both explicitly non-blocking -- see `runner_protocol.py`), and
returns. `RunStatusView.requested_state` is how a caller sees that the request
is accepted but not yet landed; nothing in this module -- or anywhere in the
operator surface -- may shorten a turn to honour a pending command sooner
(FR-008, SC-022).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from civsim_harness.models.common import EventId, RunId, Timestamp
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.operator.runner_protocol import RunnerProtocol
from civsim_harness.operator.schemas import RunStatusView
from civsim_harness.store.port import MatchStore

__all__ = ["pause", "resume", "start", "status", "stop"]


def _new_event_id() -> EventId:
    return EventId(f"cmd-{uuid.uuid4().hex}")


def _record_command(
    store: MatchStore,
    run_id: RunId,
    command: str,
    *,
    now: Timestamp | None = None,
) -> None:
    """Durably record one `lifecycle_command_received` event before its command
    takes effect (contracts/operator-surface.md: "every command is recorded ...
    before it takes effect").
    """
    event = RunEvent.model_validate(
        {
            "event_id": _new_event_id(),
            "run_id": run_id,
            "event_type": RunEventType.LIFECYCLE_COMMAND_RECEIVED,
            "occurred_at": now if now is not None else datetime.now(UTC),
            "detail": {"command": command},
        }
    )
    store.write_run_event(event)


def start(runner: RunnerProtocol, config_path: Path) -> RunId:
    """`civsim run start <config.yaml>` / `POST /runs`.

    No event is recorded here -- see the module docstring for why `start` is
    the one command with no `run_id` to attach a `lifecycle_command_received`
    event to before it exists.
    """
    return runner.start(config_path)


def pause(store: MatchStore, runner: RunnerProtocol, run_id: RunId) -> RunStatusView:
    """`civsim run pause <run_id>` / `POST /runs/{id}/pause`.

    Records the command, forwards the (non-blocking) request, and returns the
    run's status immediately after -- which will typically still show
    `requested_state == paused` and `lifecycle_state` unchanged, since pause
    lands at the next turn boundary, not on this call (FR-004, SC-022).
    """
    _record_command(store, run_id, "pause")
    runner.request_pause(run_id)
    return runner.get_status(run_id)


def resume(store: MatchStore, runner: RunnerProtocol, run_id: RunId) -> RunStatusView:
    """`civsim run resume <run_id>` / `POST /runs/{id}/resume`."""
    _record_command(store, run_id, "resume")
    runner.request_resume(run_id)
    return runner.get_status(run_id)


def stop(store: MatchStore, runner: RunnerProtocol, run_id: RunId) -> RunStatusView:
    """`civsim run stop <run_id>` / `POST /runs/{id}/stop`.

    Like `pause`, this lands on a turn boundary rather than mid-turn -- `stop`
    additionally records a terminal condition (`stop_resolution =
    operator_stop`) once it lands, which is the runner's concern, not this
    module's.
    """
    _record_command(store, run_id, "stop")
    runner.request_stop(run_id)
    return runner.get_status(run_id)


def status(runner: RunnerProtocol, run_id: RunId) -> RunStatusView:
    """`civsim run status <run_id>` / `GET /runs/{id}/status`.

    A read, not a command: no `lifecycle_command_received` event is recorded
    for a status query.
    """
    return runner.get_status(run_id)
