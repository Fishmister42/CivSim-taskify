"""The loopback-only HTTP form of the operator surface (T119;
contracts/operator-surface.md, FR-053, Principle VI).

Two structural guarantees, not conventions someone has to remember:

1. **The response schema is closed.** `GET /runs/{id}/status` returns exactly
   `operator.schemas.RunStatusView` -- a `HarnessModel` (`extra="forbid"`)
   naming only lifecycle and diagnostic fields. There is no code path in this
   module that can attach a turn record, an observation, a decision, or a
   capture to that response; doing so would require editing
   `operator/schemas.py` itself, which is the whole point (see that module's
   docstring).

2. **The server only ever binds `127.0.0.1`.** `run_server` below does not
   accept a `host` parameter at all -- the loopback address is hard-coded, so
   there is no configuration knob that could accidentally expose this surface
   on a LAN address. Deliverable 1 binds the LAN address for its own,
   deliberately richer surface (its FR-028); this one is unreachable from the
   devices deliverable 1 serves, by construction rather than by firewall rule.

This module holds no state of its own: every route is a thin translation from
an HTTP verb/path to one `operator.commands` function, using whatever
`RunnerProtocol` and `MatchStore` the caller constructs and hands to
`create_app`. `civsim doctor` / `GET /health` is served the same way, via a
caller-supplied zero-argument callable that returns a `doctor.DoctorReport` --
this module never decides how that report gets built.

**T173 additions (`resume-from`, `branch`, `archive`).** These three routes
follow the same "thin translation" rule as everything above, and the same
FR-036 discipline `operator/cli.py` applies: `resume-from`/`branch` reject a
missing save with it named, before ever reaching the runner
(`saves.addressing.require_available_save_point`), and `branch` forwards to
the *existing* `commands.start` -- a branch document is an ordinary run
configuration plus a `branch_from` block, needing no new runner method (see
`operator/cli.py`'s module docstring). `archive` needs no runner at all
(`saves/archival.py`). `saves reap` has no HTTP form at all (contracts/
operator-surface.md: "no HTTP form"), and neither does `seedset accept-build`
(scoped to `operator/cli.py` only, T174).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from civsim_harness.errors import HarnessError
from civsim_harness.models.common import EventId, HarnessModel, RunId
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.operator import commands
from civsim_harness.operator.doctor import DoctorReport
from civsim_harness.operator.runner_protocol import RunnerProtocol
from civsim_harness.operator.schemas import RunStatusView
from civsim_harness.saves.addressing import SaveAddressingError, require_available_save_point
from civsim_harness.saves.archival import archive_run
from civsim_harness.store.port import MatchStore

__all__ = [
    "LOOPBACK_HOST",
    "ArchiveRunRequest",
    "BranchRunRequest",
    "ResumeFromRequest",
    "StartRunRequest",
    "create_app",
    "run_server",
]

#: The one address this surface may ever bind (FR-053, Principle VI) -- see
#: the module docstring. Not a default; `run_server` has no parameter that
#: could override it.
LOOPBACK_HOST = "127.0.0.1"

DoctorProvider = Callable[[], Awaitable[DoctorReport]]


class StartRunRequest(HarnessModel):
    """`POST /runs` body: a path to an already-authored run configuration file."""

    config_path: str


class ResumeFromRequest(HarnessModel):
    """`POST /runs/{id}/resume-from` body: which recorded turn to resume from."""

    turn: int


class BranchRunRequest(HarnessModel):
    """`POST /runs/{id}/branch` body: which recorded turn to branch from, and
    the branch's own (variable-only) configuration file.
    """

    turn: int
    config_path: str


class ArchiveRunRequest(HarnessModel):
    """`POST /runs/{id}/archive` body: the operator identity to record.

    Required (no OS-user fallback the way `operator/cli.py` has, since this
    surface has no notion of "the caller's OS user" over loopback HTTP) --
    an unattributed archival would defeat the audit trail this mechanism
    exists to be (`saves/archival.py`).
    """

    by: str


def _as_http_error(exc: HarnessError, *, status_code: int) -> HTTPException:
    body = {"message": exc.message, "detail": exc.detail}
    return HTTPException(status_code=status_code, detail=body)


def _new_event_id() -> EventId:
    """Mirrors `operator/commands.py`'s own private `_new_event_id` -- see
    `operator/cli.py`'s identical duplication note (that module is out of
    this task's file ownership).
    """
    return EventId(f"cmd-{uuid.uuid4().hex}")


def _record_command(store: MatchStore, run_id: RunId, command: str) -> None:
    store.write_run_event(
        RunEvent(
            event_id=_new_event_id(),
            run_id=run_id,
            event_type=RunEventType.LIFECYCLE_COMMAND_RECEIVED,
            occurred_at=datetime.now(UTC),
            detail={"command": command},
        )
    )


def _reject_missing_save(store: MatchStore, run_id: str, turn: int) -> None:
    """FR-036 pre-check shared by `resume-from` and `branch` (contracts/
    operator-surface.md's error table: "Save missing on resume-from or
    branch | Rejected, reporting the missing save").
    """
    try:
        require_available_save_point(store, RunId(run_id), turn)
    except SaveAddressingError as exc:
        raise _as_http_error(exc, status_code=422) from exc


def create_app(
    *,
    runner: RunnerProtocol,
    store: MatchStore,
    doctor_provider: DoctorProvider | None = None,
) -> FastAPI:
    """Build the FastAPI app. Binding it to loopback only happens in
    `run_server`, not here -- this function is what `tests/unit/test_operator_api.py`
    exercises directly (via `TestClient`), independent of any real socket.
    """
    app = FastAPI(
        title="civsim operator surface",
        description="Lifecycle control and diagnostics only -- see FR-053.",
    )

    @app.post("/runs")
    def start_run(body: StartRunRequest) -> JSONResponse:
        try:
            run_id = commands.start(runner, Path(body.config_path))
        except HarnessError as exc:
            raise _as_http_error(exc, status_code=422) from exc
        return JSONResponse(status_code=201, content={"run_id": str(run_id)})

    @app.post("/runs/{run_id}/pause", response_model=RunStatusView)
    def pause_run(run_id: str) -> RunStatusView:
        try:
            return commands.pause(store, runner, RunId(run_id))
        except HarnessError as exc:
            raise _as_http_error(exc, status_code=409) from exc

    @app.post("/runs/{run_id}/resume", response_model=RunStatusView)
    def resume_run(run_id: str) -> RunStatusView:
        try:
            return commands.resume(store, runner, RunId(run_id))
        except HarnessError as exc:
            raise _as_http_error(exc, status_code=409) from exc

    @app.post("/runs/{run_id}/stop", response_model=RunStatusView)
    def stop_run(run_id: str) -> RunStatusView:
        try:
            return commands.stop(store, runner, RunId(run_id))
        except HarnessError as exc:
            raise _as_http_error(exc, status_code=409) from exc

    @app.get("/runs/{run_id}/status", response_model=RunStatusView)
    def get_status(run_id: str) -> RunStatusView:
        """The FR-053 bound lives in the response model's own type, not in this
        handler: `RunStatusView.model_config["extra"] == "forbid"` (inherited
        from `HarnessModel`), so FastAPI's own response serialization can never
        emit a field this contract does not name.
        """
        try:
            return commands.status(runner, RunId(run_id))
        except HarnessError as exc:
            raise _as_http_error(exc, status_code=404) from exc

    @app.post("/runs/{run_id}/resume-from", response_model=RunStatusView)
    def resume_from_run(run_id: str, body: ResumeFromRequest) -> RunStatusView:
        _reject_missing_save(store, run_id, body.turn)
        _record_command(store, RunId(run_id), f"resume-from --turn {body.turn}")
        try:
            runner.resume_from(RunId(run_id), body.turn)
        except HarnessError as exc:
            raise _as_http_error(exc, status_code=409) from exc
        return runner.get_status(RunId(run_id))

    @app.post("/runs/{run_id}/branch")
    def branch_run(run_id: str, body: BranchRunRequest) -> JSONResponse:
        """A branch document is an ordinary run configuration plus a
        `branch_from` block -- this forwards to the same `commands.start`
        `POST /runs` already uses, after the one FR-036 pre-check a live
        client cannot answer for it (see the module docstring).
        """
        _reject_missing_save(store, run_id, body.turn)
        _record_command(
            store, RunId(run_id), f"branch --turn {body.turn} --config {body.config_path}"
        )
        try:
            new_run_id = commands.start(runner, Path(body.config_path))
        except HarnessError as exc:
            raise _as_http_error(exc, status_code=422) from exc
        return JSONResponse(status_code=201, content={"run_id": str(new_run_id)})

    @app.post("/runs/{run_id}/archive")
    def archive_run_route(run_id: str, body: ArchiveRunRequest) -> JSONResponse:
        _record_command(store, RunId(run_id), "archive")
        try:
            archive_run(store, RunId(run_id), by=body.by, at=datetime.now(UTC))
        except HarnessError as exc:
            raise _as_http_error(exc, status_code=409) from exc
        return JSONResponse(status_code=200, content={"run_id": run_id, "archived": True})

    @app.get("/health")
    async def health() -> DoctorReport:
        if doctor_provider is None:
            raise HTTPException(status_code=503, detail="doctor is not configured on this server")
        return await doctor_provider()

    return app


def run_server(
    *,
    runner: RunnerProtocol,
    store: MatchStore,
    doctor_provider: DoctorProvider | None = None,
    port: int = 4319,
) -> None:
    """Serve the operator surface on `127.0.0.1:{port}` -- and nowhere else.

    No `host` parameter exists here on purpose (see the module docstring).
    Imports `uvicorn` lazily so that constructing/testing the FastAPI app via
    `create_app` (as `tests/unit/test_operator_api.py` does, with
    `TestClient`) never requires an actual server to be started.
    """
    import uvicorn

    app = create_app(runner=runner, store=store, doctor_provider=doctor_provider)
    uvicorn.run(app, host=LOOPBACK_HOST, port=port)
