"""Typer CLI entry point for the civsim operator surface (T118).

This is the ``civsim`` console script (see ``pyproject.toml``
``[project.scripts]``). Implements the lifecycle commands
(``run start|pause|resume|stop|status``, ``doctor``) and the record-only
audits (``audit parity|prompts|decisions|steps|loop|capabilities``) named in
``contracts/operator-surface.md`` for this wave. ``run resume-from``,
``run branch``, ``run archive``, ``seedset accept-build``, and ``saves reap``
are later waves' additions (not this task's -- see T118's own scope) and are
deliberately left unregistered rather than stubbed, so `--help` reflects only
what is actually implemented; the ``run_app``/``audit_app`` sub-``Typer``
objects below are exactly where those commands attach later without touching
anything already here.

The CLI never presents turn records, decisions, metrics, or captures (FR-053,
Principle VI) -- ``run status`` returns exactly ``operator.schemas.RunStatusView``,
the same closed shape ``GET /runs/{id}/status`` (``operator/api.py``) returns.

**Runner wiring.** ``src/civsim_harness/run/runner.py`` (T116) does not exist
yet -- see ``operator/runner_protocol.py`` for the exact seam it must satisfy.
Until it does, this module resolves a runner through an overridable factory:
call :func:`configure_runner_factory` once at process start-up (e.g. from a
small bootstrap ``__main__`` the eventual integration owns) to wire a real
one; tests call it to inject a fake. Invoking a ``run`` subcommand with no
factory configured fails with a clear, actionable message rather than an
import error or a stack trace naming a module that does not exist.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import typer

from civsim_harness.capability.loader import Catalog, load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError, HarnessError
from civsim_harness.host.factory import get_host_platform
from civsim_harness.models.common import RunId
from civsim_harness.models.run import LifecycleState
from civsim_harness.operator import audit as audit_module
from civsim_harness.operator import commands
from civsim_harness.operator.doctor import format_doctor_report, run_doctor
from civsim_harness.operator.runner_protocol import RunnerProtocol
from civsim_harness.operator.schemas import RunStatusView
from civsim_harness.store.port import MatchStore
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

app = typer.Typer(
    name="civsim",
    help="Operator CLI for the Civilization-Playing Harness (lifecycle and diagnostics only).",
    no_args_is_help=True,
)
run_app = typer.Typer(name="run", help="Lifecycle control for one run.", no_args_is_help=True)
audit_app = typer.Typer(
    name="audit", help="Verify a run's record from the store alone.", no_args_is_help=True
)
app.add_typer(run_app, name="run")
app.add_typer(audit_app, name="audit")


@app.command()
def version() -> None:
    """Print the harness package version."""
    from civsim_harness import __version__

    typer.echo(f"civsim-harness {__version__}")


# --------------------------------------------------------------------------
# Runner wiring -- see the module docstring
# --------------------------------------------------------------------------

_runner_factory: Callable[[], RunnerProtocol] | None = None


def configure_runner_factory(factory: Callable[[], RunnerProtocol] | None) -> None:
    """Wire (or clear, with `None`) the factory `run` subcommands use to reach
    a `RunnerProtocol` implementation. See the module docstring.
    """
    global _runner_factory
    _runner_factory = factory


def _get_runner() -> RunnerProtocol:
    if _runner_factory is None:
        typer.echo(
            "no runner is configured -- civsim_harness.run.runner (T116) is a later wave's "
            "addition; call civsim_harness.operator.cli.configure_runner_factory(...) to wire "
            "one before invoking a `run` subcommand (see operator/runner_protocol.py).",
            err=True,
        )
        raise typer.Exit(code=1)
    return _runner_factory()


# --------------------------------------------------------------------------
# Store / catalog resolution -- shared by `doctor` and every `audit` command
# --------------------------------------------------------------------------

DEFAULT_STORE_PATH_ENV_VAR = "CIVSIM_STORE_PATH"
DEFAULT_STORE_PATH = Path("civsim-match-store.db")
DEFAULT_CATALOG_ROOT = Path("catalogs")

# Typer/click option and argument objects, module-level singletons so they are
# never constructed inside a function's default-argument expression (ruff
# B008) and so `--catalog-root`/`--store-path` stay identical, byte for byte,
# across every command that accepts them.
_CatalogRootOption = typer.Option(DEFAULT_CATALOG_ROOT, "--catalog-root")
_StorePathOption = typer.Option(None, "--store-path")
_ConfigPathArgument = typer.Argument(..., exists=True, dir_okay=False, readable=True)

_store_factory: Callable[[Path], MatchStore] | None = None


def configure_store_factory(factory: Callable[[Path], MatchStore] | None) -> None:
    """Wire (or clear, with `None`) how `doctor`/`audit` open a `MatchStore`
    from a path. Defaults to `SqliteMatchStore` -- override for tests or for a
    non-default reference adapter.
    """
    global _store_factory
    _store_factory = factory


def _resolve_store_path(store_path: Path | None) -> Path:
    if store_path is not None:
        return store_path
    return Path(os.environ.get(DEFAULT_STORE_PATH_ENV_VAR, str(DEFAULT_STORE_PATH)))


def _open_store(store_path: Path | None) -> MatchStore:
    path = _resolve_store_path(store_path)
    if _store_factory is not None:
        return _store_factory(path)
    return SqliteMatchStore(path)


def _load_catalog_or_exit(catalog_root: Path) -> Catalog:
    try:
        return load_catalog(catalog_root)
    except CatalogError as exc:
        typer.echo(f"catalog failed to load from {catalog_root}: {exc.message}", err=True)
        raise typer.Exit(code=2) from exc


# --------------------------------------------------------------------------
# run start | pause | resume | stop | status
# --------------------------------------------------------------------------


def _print_status(status: RunStatusView) -> None:
    typer.echo(f"run_id               : {status.run_id}")
    typer.echo(f"lifecycle_state      : {status.lifecycle_state.value}")
    requested = status.requested_state.value if status.requested_state else "-"
    typer.echo(f"requested_state      : {requested}")
    current_turn = status.current_turn if status.current_turn is not None else "-"
    typer.echo(f"current_turn         : {current_turn}")
    current_step = status.current_step if status.current_step is not None else "-"
    typer.echo(f"current_step         : {current_step}")
    if status.last_known_good_save is not None:
        typer.echo(
            "last_known_good_save : turn "
            f"{status.last_known_good_save.turn} ({status.last_known_good_save.save_point_id})"
        )
    else:
        typer.echo("last_known_good_save : -")
    if status.last_error is not None:
        typer.echo(f"last_error           : {status.last_error.type} at {status.last_error.at}")
    else:
        typer.echo("last_error           : -")
    typer.echo(
        "connection_health    : tuner="
        f"{status.connection_health.tuner} client={status.connection_health.client} "
        f"store={status.connection_health.store}"
    )
    typer.echo(f"record_completeness  : {status.record_completeness_status.value}")
    typer.echo(f"comparability_status : {status.comparability_status.value}")
    typer.echo(f"archived             : {status.archived}")
    typer.echo(f"disk_headroom_gb     : {status.disk_headroom_gb}")


@run_app.command("start")
def run_start(config_path: Path = _ConfigPathArgument) -> None:
    """Validate, preflight, prepare, and begin playing CONFIG_PATH."""
    runner = _get_runner()
    try:
        run_id = commands.start(runner, config_path)
    except HarnessError as exc:
        typer.echo(f"run start failed: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc

    status = runner.get_status(run_id)
    typer.echo(f"run_id: {run_id}")
    _print_status(status)
    if status.lifecycle_state == LifecycleState.FAILED:
        # Preflight-mismatch-shaped failure: a Run exists (per the contract's
        # error table) but never reached turn 1. Non-zero so scripting/CI
        # treats it as the failure it is.
        raise typer.Exit(code=1)


def _run_command_and_print(
    action: Callable[[MatchStore, RunnerProtocol, RunId], RunStatusView], run_id: str, verb: str
) -> None:
    runner = _get_runner()
    store = _open_store(None)
    try:
        status = action(store, runner, RunId(run_id))
    except HarnessError as exc:
        typer.echo(f"run {verb} failed: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    _print_status(status)


@run_app.command("pause")
def run_pause(run_id: str) -> None:
    """Pause RUN_ID at its next turn boundary (not mid-turn -- FR-004, SC-022)."""
    _run_command_and_print(commands.pause, run_id, "pause")


@run_app.command("resume")
def run_resume(run_id: str) -> None:
    """Resume a paused RUN_ID."""
    _run_command_and_print(commands.resume, run_id, "resume")


@run_app.command("stop")
def run_stop(run_id: str) -> None:
    """Stop RUN_ID at its next turn boundary, recording `operator_stop`."""
    _run_command_and_print(commands.stop, run_id, "stop")


@run_app.command("status")
def run_status(run_id: str) -> None:
    """Print RUN_ID's lifecycle and diagnostic status -- records live in the store."""
    runner = _get_runner()
    try:
        status = commands.status(runner, RunId(run_id))
    except HarnessError as exc:
        typer.echo(f"run status failed: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    _print_status(status)


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


@app.command()
def doctor(
    catalog_root: Path = _CatalogRootOption,
    store_path: Path | None = _StorePathOption,
) -> None:
    """Preflight the environment: tuner, client, store, catalog, capture path,
    disk headroom, provider key presence (quickstart.md Setup, FR-043).
    """
    host = get_host_platform()
    store = _open_store(store_path)
    try:
        report = asyncio.run(run_doctor(host=host, store=store, catalog_root=catalog_root))
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    typer.echo(format_doctor_report(report))


# --------------------------------------------------------------------------
# audit parity | prompts | decisions | steps | loop | capabilities
# --------------------------------------------------------------------------

_TurnOption = typer.Option(..., "--turn")


def _run_registry_audit(
    audit_fn: Callable[[MatchStore, CapabilityRegistry, RunId], audit_module.AuditReport],
    name: str,
    run_id: str,
    catalog_root: Path,
    store_path: Path | None,
) -> None:
    catalog = _load_catalog_or_exit(catalog_root)
    registry = CapabilityRegistry(catalog)
    store = _open_store(store_path)
    report = audit_fn(store, registry, RunId(run_id))
    typer.echo(audit_module.format_audit_report(name, report))
    raise typer.Exit(code=audit_module.exit_code_for(report))


def _run_store_only_audit(
    audit_fn: Callable[[MatchStore, RunId], audit_module.AuditReport],
    name: str,
    run_id: str,
    store_path: Path | None,
) -> None:
    store = _open_store(store_path)
    report = audit_fn(store, RunId(run_id))
    typer.echo(audit_module.format_audit_report(name, report))
    raise typer.Exit(code=audit_module.exit_code_for(report))


@audit_app.command("parity")
def audit_parity(
    run_id: str,
    catalog_root: Path = _CatalogRootOption,
    store_path: Path | None = _StorePathOption,
) -> None:
    """Every observation and action RUN_ID used resolves to a parity
    declaration and catalog version (SC-006, SC-007)."""
    _run_registry_audit(audit_module.audit_parity, "parity", run_id, catalog_root, store_path)


@audit_app.command("prompts")
def audit_prompts(run_id: str, store_path: Path | None = _StorePathOption) -> None:
    """Every prompt RUN_ID encountered is a recorded decision or stall (SC-005)."""
    _run_store_only_audit(audit_module.audit_prompts, "prompts", run_id, store_path)


@audit_app.command("decisions")
def audit_decisions(run_id: str, store_path: Path | None = _StorePathOption) -> None:
    """Zero decisions in RUN_ID exist without a model_call_id (SC-012, I5)."""
    _run_store_only_audit(audit_module.audit_decisions, "decisions", run_id, store_path)


@audit_app.command("steps")
def audit_steps(run_id: str, store_path: Path | None = _StorePathOption) -> None:
    """One decision, one model call, one observation per step; contiguous
    step_index; no reused observation or capture (SC-003, I13, I14)."""
    _run_store_only_audit(audit_module.audit_steps, "steps", run_id, store_path)


@audit_app.command("loop")
def audit_loop(
    run_id: str,
    turn: int = _TurnOption,
    store_path: Path | None = _StorePathOption,
) -> None:
    """Within TURN, each observation was assembled after the prior step's
    verification, with a distinct capture per step (I14, I15)."""
    store = _open_store(store_path)
    report = audit_module.audit_loop(store, RunId(run_id), turn)
    typer.echo(audit_module.format_audit_report("loop", report))
    raise typer.Exit(code=audit_module.exit_code_for(report))


@audit_app.command("capabilities")
def audit_capabilities(
    run_id: str,
    catalog_root: Path = _CatalogRootOption,
    store_path: Path | None = _StorePathOption,
) -> None:
    """Every capability RUN_ID used is firetuner-path or carries a non-empty
    firetuner_gap (FR-028, SC-020)."""
    _run_registry_audit(
        audit_module.audit_capabilities, "capabilities", run_id, catalog_root, store_path
    )


if __name__ == "__main__":
    app()
