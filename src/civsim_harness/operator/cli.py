"""Typer CLI entry point for the civsim operator surface (T118, T173, T174).

This is the ``civsim`` console script (see ``pyproject.toml``
``[project.scripts]``). Implements the lifecycle commands (``run
start|pause|resume|stop|status|resume-from|branch|abandon|archive``, ``saves reap``,
``seedset accept-build``, ``doctor``) and the record-only audits (``audit
parity|prompts|decisions|steps|loop|capabilities|recovery|completeness|
lineage|immutability|builds|models|secrets``) named in
``contracts/operator-surface.md``.

The CLI never presents turn records, decisions, metrics, or captures (FR-053,
Principle VI) -- ``run status`` returns exactly ``operator.schemas.RunStatusView``,
the same closed shape ``GET /runs/{id}/status`` (``operator/api.py``) returns.

**Runner wiring.** This module resolves a runner through an overridable
factory, wired at import to :func:`default_runner_factory` -- which composes
the real thing (``run/composition.py``'s ``build_runner_dependencies`` around
``run/runner.py``'s ``Runner``, T209). Assigning the factory constructs
nothing; the catalog is loaded and the tuner socket opened only when a ``run``
subcommand actually asks for a runner, so ``--help``, ``doctor`` and every
``audit`` command stay as cheap as they were. Tests (and embedders wanting a
different composition) override it via :func:`configure_runner_factory`, and
passing ``None`` clears it entirely -- which is the only way a ``run``
subcommand now reports "no runner is configured".

**``run branch`` needs no new seam.** A branch document is an ordinary run
configuration plus a ``branch_from`` block (``config/run_config.py``); this
command pre-checks the one thing it can check without a live client -- the
parent's save at the named turn is not missing (FR-036, via
``saves.addressing.require_available_save_point``) -- and otherwise routes
straight through the *existing* ``RunnerProtocol.start``, exactly as ``run
start`` does for any other configuration file. ``run resume-from`` has no
such existing counterpart (it continues the *same* run_id from an earlier
turn rather than creating a new one), so it is the one place this wave adds
a method to ``RunnerProtocol`` (``resume_from`` -- see that module).

**``run archive`` and ``saves reap`` need no runner at all.** Both are pure
``MatchStore`` operations (``saves/archival.py``, ``saves/reaper.py``) --
archival is a recorded decision that a run is no longer a branch source, not
a live-session concern.

**``seedset accept-build`` records the acceptance on the seed set's own YAML
file only.** There is no ``run_id`` yet at the point an operator accepts a
build change (quickstart.md Scenario 9 runs it *before* `run start`), and
``RunEvent`` cannot be constructed without one (``models/records.py``) --
mirroring ``operator/commands.py``'s own reasoning for why `start` records no
`lifecycle_command_received` event of its own. The ``game_build_change_
accepted`` event data-model.md describes as recorded "on every dependent
run" is exactly that: recorded once a dependent run actually exists and
relies on this acceptance, by whatever preflight code resolves it then (a
concurrent wave's `run/preparation.py`) -- not by this command, which has
no run to attach it to.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
import yaml

from civsim_harness.capability.loader import Catalog, load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.config.run_config import peek_branch_from
from civsim_harness.config.seed_set import load_seed_set_file
from civsim_harness.errors import CatalogError, HarnessError
from civsim_harness.host.detect import probe_host_support
from civsim_harness.host.factory import get_host_platform
from civsim_harness.models.common import AcceptanceId, BuildAcceptance, EventId, RunId, Timestamp
from civsim_harness.models.config import SeedSet
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.run import LifecycleState
from civsim_harness.observe.game_build import is_platform_transition
from civsim_harness.operator import audit as audit_module
from civsim_harness.operator import commands
from civsim_harness.operator.doctor import format_doctor_report, run_doctor
from civsim_harness.operator.runner_protocol import RunnerProtocol
from civsim_harness.operator.schemas import RunStatusView
from civsim_harness.saves.addressing import SaveAddressingError, require_available_save_point
from civsim_harness.saves.archival import archive_run
from civsim_harness.saves.branching import abandon_branch_run
from civsim_harness.saves.reaper import reap
from civsim_harness.store.port import MatchStore
from civsim_harness.store.sqlite_adapter import SqliteMatchStore
from civsim_harness.telemetry.logging import configure_logging

app = typer.Typer(
    name="civsim",
    help="Operator CLI for the Civilization-Playing Harness (lifecycle and diagnostics only).",
    no_args_is_help=True,
)
run_app = typer.Typer(name="run", help="Lifecycle control for one run.", no_args_is_help=True)
audit_app = typer.Typer(
    name="audit", help="Verify a run's record from the store alone.", no_args_is_help=True
)
saves_app = typer.Typer(
    name="saves", help="Operator-invoked save retention operations.", no_args_is_help=True
)
seedset_app = typer.Typer(
    name="seedset", help="Seed set maintenance (build-pin acceptance).", no_args_is_help=True
)
app.add_typer(run_app, name="run")
app.add_typer(audit_app, name="audit")
app.add_typer(saves_app, name="saves")
app.add_typer(seedset_app, name="seedset")


@app.callback()
def _configure_harness_logging() -> None:
    """T228, FR-020, T015: wire the redacting log handler before any command runs.

    ``telemetry/logging.py`` makes redaction structural -- ``configure_logging`` and
    ``attach_handler`` force the redacting formatter *and* filter onto every handler they hand
    out, so "bypassing redaction requires going around this module entirely". Nothing in ``src/``
    called either one, which meant the ``civsim_harness.*`` loggers that do emit
    (``nexus/client.py``, ``nexus/sentinels.py``) propagated to whatever root handler the host
    process happened to have -- with no redaction filter on it at all. Configuring the package
    logger here catches those by propagation, since both are its children.

    A **Typer callback**, not module import: this is the process entry point for an operator
    command, and doing it at import would reach into the logging configuration of anything that
    merely imports this module (the test suite included), which is the caller's business, not
    this module's. Stderr rather than stdout, so structured telemetry never interleaves with the
    command output an operator is reading or piping.
    """
    configure_logging(stream=sys.stderr)


@app.command()
def version() -> None:
    """Print the harness package version."""
    from civsim_harness import __version__

    typer.echo(f"civsim-harness {__version__}")


# --------------------------------------------------------------------------
# Runner wiring -- see the module docstring
# --------------------------------------------------------------------------

#: Wired to :func:`default_runner_factory` at the bottom of this module, so an ordinary `civsim`
#: process reaches a real runner with no bootstrap step of its own. Tests (and any embedder
#: wanting a different composition) override it through :func:`configure_runner_factory`; passing
#: ``None`` clears it entirely, which is what makes `_get_runner`'s "no runner is configured"
#: branch reachable and testable rather than dead code.
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
            "no runner is configured -- the default factory was explicitly cleared by calling "
            "civsim_harness.operator.cli.configure_runner_factory(None); call it again with a "
            "factory (or default_runner_factory) before invoking a `run` subcommand.",
            err=True,
        )
        raise typer.Exit(code=1)
    return _runner_factory()


def default_runner_factory() -> RunnerProtocol:
    """Build the real, fully composed :class:`~civsim_harness.run.runner.Runner` (T209).

    This is what makes ``civsim run start`` reach a runner at all. Until the composition root
    landed, ``_runner_factory`` was ``None`` in every production process and the command failed on
    its first line, before the configuration file was even parsed -- the single blocking finding of
    ``specs/002-civ-playing-harness/integration-readiness.md``.

    Resolves its collaborators from exactly the conventions the rest of this CLI already uses: the
    store from ``$CIVSIM_STORE_PATH`` (via :func:`_open_store`, the same construction ``doctor``
    and every ``audit`` subcommand use), the host adapter from
    :func:`~civsim_harness.host.factory.get_host_platform`, and the catalog from
    :data:`DEFAULT_CATALOG_ROOT`. Imported lazily so ``civsim --help`` and the ``audit``/``doctor``
    commands never pay for loading the run stack they do not use.

    **The host support probe is the real R19 per-platform one** (T212,
    :func:`~civsim_harness.host.detect.probe_host_support`, resolved inside
    ``build_runner_dependencies``). It was left at ``UNPROBED`` until that probe existed, and
    since nothing in ``src/`` ever constructed anything else,
    :func:`~civsim_harness.observe.host_gate.evaluate_host_gate` refused **every** run on
    **every** host -- including a Linux one that had already passed live validation -- with a
    message that read as a host-specific finding and was not one. A host with no recorded spike
    for its platform still refuses (FR-054), which is the correct outcome: a host claiming a
    capability nothing ever demonstrated is exactly what that gate exists to prevent. What
    changed is that the refusal now names which spike is missing on which platform.
    """
    from civsim_harness.run.composition import build_runner_dependencies
    from civsim_harness.run.runner import Runner

    return Runner(
        build_runner_dependencies(
            store=_open_store(None),
            host=get_host_platform(),
            catalog_root=DEFAULT_CATALOG_ROOT,
        )
    )


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
_TurnOption = typer.Option(..., "--turn")

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
        # The message alone is often just "run preparation failed; no run was created" -- the
        # actionable part (which gate refused, and why) lives in `detail`. Printing only the
        # message leaves an operator with nothing to act on, which is exactly the shape of
        # failure this command is most likely to produce against a real client.
        typer.echo(f"run start failed: {exc.message}", err=True)
        for key, value in (exc.detail or {}).items():
            typer.echo(f"  {key}: {value}", err=True)
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
# run resume-from | branch | archive (T173)
# --------------------------------------------------------------------------

_BranchConfigOption = typer.Option(..., "--config", exists=True, dir_okay=False, readable=True)
_ByOption = typer.Option(None, "--by", help="Operator identity to record (default: OS user)")


def _new_event_id() -> EventId:
    """Mirrors `operator/commands.py`'s own private `_new_event_id` -- that
    module is out of this task's file ownership, so the small, stable
    `lifecycle_command_received`-recording shape it already establishes is
    duplicated here rather than imported past its module boundary.
    """
    return EventId(f"cmd-{uuid.uuid4().hex}")


def _record_command(store: MatchStore, run_id: RunId, command: str) -> None:
    """Every command in contracts/operator-surface.md's table -- including
    the three below, which `operator/commands.py` does not cover -- is
    durably recorded as `lifecycle_command_received` before it takes effect.
    """
    store.write_run_event(
        RunEvent(
            event_id=_new_event_id(),
            run_id=run_id,
            event_type=RunEventType.LIFECYCLE_COMMAND_RECEIVED,
            occurred_at=datetime.now(UTC),
            detail={"command": command},
        )
    )


def _reject_missing_save(
    store: MatchStore, run_id: str, turn: int, *, verb: str
) -> None:
    """Shared FR-036 pre-check for `resume-from` and `branch`: reject with
    the missing save named, before ever reaching a runner (contracts/
    operator-surface.md's error table: "Save missing on resume-from or
    branch | Rejected, reporting the missing save").
    """
    try:
        require_available_save_point(store, RunId(run_id), turn)
    except SaveAddressingError as exc:
        typer.echo(f"run {verb} failed: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc


@run_app.command("resume-from")
def run_resume_from(
    run_id: str,
    turn: int = _TurnOption,
    store_path: Path | None = _StorePathOption,
) -> None:
    """Resume RUN_ID -- the same run -- from its recorded save at TURN
    (FR-036, contracts/operator-surface.md)."""
    store = _open_store(store_path)
    _reject_missing_save(store, run_id, turn, verb="resume-from")

    runner = _get_runner()
    _record_command(store, RunId(run_id), f"resume-from --turn {turn}")
    try:
        runner.resume_from(RunId(run_id), turn)
    except HarnessError as exc:
        typer.echo(f"run resume-from failed: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc

    status = runner.get_status(RunId(run_id))
    _print_status(status)
    if status.lifecycle_state == LifecycleState.FAILED:
        raise typer.Exit(code=1)


@run_app.command("branch")
def run_branch(
    run_id: str,
    turn: int = _TurnOption,
    config_path: Path = _BranchConfigOption,
    store_path: Path | None = _StorePathOption,
) -> None:
    """Start a new run from RUN_ID's turn TURN save, using CONFIG_PATH's
    branch configuration, recording lineage (FR-033, FR-036).

    A branch document is an ordinary run configuration plus a `branch_from`
    block naming its own `run_id`/`turn` (contracts/run-configuration.md
    "Branch configuration") -- this command only pre-checks the missing-save
    case itself (the one thing checkable without a live client) and
    otherwise forwards CONFIG_PATH to `RunnerProtocol.start`, exactly like
    `run start`; see the module docstring.
    """
    store = _open_store(store_path)
    _reject_missing_save(store, run_id, turn, verb="branch")

    # T226, FR-033, Principle IV: this command states the lineage twice -- once as its own
    # arguments, once inside the document -- and the two must agree before anything is created.
    # Checked here rather than left to the runner because this is the only layer that can see
    # both, and because a mismatch is a wrong branch, not a recoverable detail: the lineage is the
    # load-bearing claim a branch's whole record rests on.
    try:
        branch_from = peek_branch_from(config_path)
    except HarnessError as exc:
        typer.echo(f"run branch failed: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    if branch_from is None:
        typer.echo(
            "run branch failed: this configuration is not a branch document -- it carries no "
            "branch_from block naming the run and turn to branch from "
            "(contracts/run-configuration.md 'Branch configuration')",
            err=True,
        )
        raise typer.Exit(code=1)
    if branch_from.run_id != run_id or branch_from.turn != turn:
        typer.echo(
            "run branch failed: the branch document's branch_from block names "
            f"{branch_from.run_id} at turn {branch_from.turn}, but this command names {run_id} "
            f"at turn {turn}; nothing was created",
            err=True,
        )
        raise typer.Exit(code=1)

    _record_command(store, RunId(run_id), f"branch --turn {turn} --config {config_path}")
    runner = _get_runner()
    try:
        new_run_id = commands.start(runner, config_path)
    except HarnessError as exc:
        typer.echo(f"run branch failed: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc

    status = runner.get_status(new_run_id)
    typer.echo(f"run_id: {new_run_id}")
    _print_status(status)
    if status.lifecycle_state == LifecycleState.FAILED:
        raise typer.Exit(code=1)


@run_app.command("archive")
def run_archive(
    run_id: str,
    by: str | None = _ByOption,
    store_path: Path | None = _StorePathOption,
) -> None:
    """Mark RUN_ID archived -- the only thing that makes its saves eligible
    for `saves reap` (FR-004, FR-036). Rejected on a non-terminal run.

    Needs no runner: archival is a pure `MatchStore` operation
    (`saves/archival.py`) an operator invokes explicitly, never a live-run
    concern (see the module docstring).
    """
    store = _open_store(store_path)
    operator_identity = by or getpass.getuser()
    now = datetime.now(UTC)
    _record_command(store, RunId(run_id), "archive")
    try:
        archive_run(store, RunId(run_id), by=operator_identity, at=now)
    except HarnessError as exc:
        typer.echo(f"run archive failed: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"run {run_id} archived by {operator_identity} at {now.isoformat()}")


# --------------------------------------------------------------------------
# run abandon (T241)
# --------------------------------------------------------------------------

_ReasonOptionalOption = typer.Option(
    None, "--reason", help="Why this branch is being abandoned (recorded on the event)"
)


@run_app.command("abandon")
def run_abandon(
    run_id: str,
    reason: str | None = _ReasonOptionalOption,
    store_path: Path | None = _StorePathOption,
) -> None:
    """Abandon branch RUN_ID: record `branch_abandoned`, mark its own turns
    superseded -- never deleted -- and leave its lifecycle honestly terminal
    (FR-035, T241).

    Only a branch (a run with recorded lineage) can be abandoned, and only
    when nothing may still be driving it: `paused`, `finished`, or `failed`.
    A branch still playing must be paused or stopped first -- abandonment
    strips the branch's own authoritative record and must not race a live
    turn loop (FR-004/FR-008). Needs no runner: like `run archive`, this is a
    recorded `MatchStore` decision (`saves/branching.py`), not a live-session
    concern.
    """
    store = _open_store(store_path)
    command = "abandon" if reason is None else f"abandon --reason {reason}"
    _record_command(store, RunId(run_id), command)
    try:
        outcome = abandon_branch_run(
            store, RunId(run_id), reason=reason, occurred_at=datetime.now(UTC)
        )
    except HarnessError as exc:
        typer.echo(f"run abandon failed: {exc.message}", err=True)
        for key, value in (exc.detail or {}).items():
            typer.echo(f"  {key}: {value}", err=True)
        raise typer.Exit(code=1) from exc

    superseded = (
        ", ".join(str(turn) for turn in outcome.superseded_turns)
        if outcome.superseded_turns
        else "-"
    )
    typer.echo(f"run {run_id} abandoned (branch of {outcome.run.parent_run_id})")
    typer.echo(f"lifecycle_state      : {outcome.run.lifecycle_state.value}")
    typer.echo(f"superseded_turns     : {superseded}")
    typer.echo(f"record_completeness  : {outcome.run.record_completeness_status.value}")


# --------------------------------------------------------------------------
# saves reap (T173)
# --------------------------------------------------------------------------

_DryRunOption = typer.Option(True, "--dry-run/--no-dry-run", help="Preview only; the default")
_ConfirmOption = typer.Option(
    False, "--confirm", help="Actually delete eligible save files (overrides --dry-run)"
)


@saves_app.command("reap")
def saves_reap(
    dry_run: bool = _DryRunOption,
    confirm: bool = _ConfirmOption,
    store_path: Path | None = _StorePathOption,
) -> None:
    """Delete save files for already-archived runs only -- never a
    background job, never run unattended (FR-036, research R17). Defaults
    to `--dry-run`; pass `--confirm` to actually delete.
    """
    store = _open_store(store_path)
    host = get_host_platform()
    effective_dry_run = dry_run and not confirm
    report = reap(store, host, dry_run=effective_dry_run)

    typer.echo(f"saves reap: dry_run={effective_dry_run}")
    typer.echo(f"  eligible save points: {len(report.items)}")
    paths = report.deleted_paths if not effective_dry_run else report.would_delete_paths
    verb = "deleted" if not effective_dry_run else "would delete"
    typer.echo(f"  {verb} ({len(paths)}):")
    for path in paths:
        typer.echo(f"    - {path}")


# --------------------------------------------------------------------------
# seedset accept-build (T174)
# --------------------------------------------------------------------------

DEFAULT_SEEDSET_ROOT = Path("configs/seedsets")
_SeedsetRootOption = typer.Option(DEFAULT_SEEDSET_ROOT, "--seedset-root")
_ToBuildOption = typer.Option(..., "--to", help="The build to accept, e.g. win/1.0.12.11")
_ReasonOption = typer.Option(..., "--reason")

#: Resolves the recorded, **passing** R20 cross-platform save spike result
#: (T199) to reference from a platform-crossing `BuildAcceptance`, or `None`
#: when none is on record. No such result exists yet in this repo (T199 has
#: not run -- specs/002-civ-playing-harness/spikes/r20-cross-platform-saves.md
#: does not exist), so the default always returns `None`, which fails every
#: platform-crossing acceptance closed -- exactly as research R20 requires
#: until T199 records a pass. Overridable via
#: :func:`configure_r20_spike_resolver` for whenever T199 lands, or for
#: tests planting a fake passing result.
R20SpikeResolver = Callable[[], str | None]


def _default_r20_spike_resolver() -> str | None:
    return None


_r20_spike_resolver: R20SpikeResolver = _default_r20_spike_resolver


def configure_r20_spike_resolver(resolver: R20SpikeResolver | None) -> None:
    global _r20_spike_resolver
    _r20_spike_resolver = resolver if resolver is not None else _default_r20_spike_resolver


def _seed_set_path(seedset_root: Path, name: str) -> Path:
    return seedset_root / f"{name}.yaml"


def _seed_set_to_yaml_dict(seed_set: SeedSet) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": seed_set.name,
        "civilization": seed_set.civilization,
        "leader": seed_set.leader,
        "ruleset": seed_set.ruleset,
        "mod_set": [m.model_dump(mode="json") for m in seed_set.mod_set],
        "game_build": seed_set.game_build,
        "seeds": list(seed_set.seeds),
        "created_at": seed_set.created_at.isoformat(),
        "accepted_build_changes": [
            a.model_dump(mode="json") for a in seed_set.accepted_build_changes
        ],
    }


def _write_seed_set_file(path: Path, seed_set: SeedSet) -> None:
    path.write_text(
        yaml.safe_dump(_seed_set_to_yaml_dict(seed_set), sort_keys=False), encoding="utf-8"
    )


@seedset_app.command("accept-build")
def seedset_accept_build(
    name: str,
    to_build: str = _ToBuildOption,
    reason: str = _ReasonOption,
    by: str | None = _ByOption,
    seedset_root: Path = _SeedsetRootOption,
) -> None:
    """Accept exactly one `from_build -> to_build` transition for seed set
    NAME (FR-002). Scoped to this one composite transition only -- there is
    no global override, and accepting a version bump never implicitly
    accepts a platform change (research R18, R20): the acceptance this
    writes covers `NAME.game_build -> --to` and nothing else.

    A version-only transition needs no spike result. A platform-crossing
    transition is refused unless a passing R20 spike result is on record
    (T199) -- see :data:`R20SpikeResolver`.
    """
    path = _seed_set_path(seedset_root, name)
    if not path.is_file():
        typer.echo(f"seedset accept-build failed: no seed set file at {path}", err=True)
        raise typer.Exit(code=2)

    try:
        seed_set = load_seed_set_file(path)
    except HarnessError as exc:
        typer.echo(f"seedset accept-build failed: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc

    from_build = seed_set.game_build
    platform_transition = is_platform_transition(from_build, to_build)
    spike_ref: str | None = None
    if platform_transition:
        spike_ref = _r20_spike_resolver()
        if not spike_ref:
            typer.echo(
                "seedset accept-build failed: "
                f"{from_build} -> {to_build} crosses platform and no passing R20 "
                "cross-platform save spike result is on record (T199); accepting a "
                "version bump never implicitly accepts a platform change (research R20)",
                err=True,
            )
            raise typer.Exit(code=1)

    accepted_at: Timestamp = datetime.now(UTC)
    acceptance = BuildAcceptance(
        acceptance_id=AcceptanceId(f"acc_{uuid.uuid4().hex}"),
        from_build=from_build,
        to_build=to_build,
        accepted_by=by or getpass.getuser(),
        accepted_at=accepted_at,
        reason=reason,
        r20_spike_ref=spike_ref,
    )
    updated = seed_set.model_copy(
        update={"accepted_build_changes": [*seed_set.accepted_build_changes, acceptance]}
    )
    _write_seed_set_file(path, updated)

    typer.echo(f"accepted {from_build} -> {to_build} for seed set {name!r}")
    typer.echo(f"acceptance_id      : {acceptance.acceptance_id}")
    typer.echo(f"is_platform_transition : {acceptance.is_platform_transition}")
    typer.echo(f"r20_spike_ref      : {acceptance.r20_spike_ref or '-'}")
    typer.echo(f"set is now non-uniform : {not updated.is_uniform}")


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
        # T212: the same real per-platform probe `run start` now gates on. `run_doctor`'s own
        # default is still `UNPROBED` -- it is a library entry point with no host opinion of its
        # own -- but the CLI *does* know which host it is on, and R19's whole point is that
        # `doctor` prints the tier with its reason. Reporting "not yet probed" here while the
        # runner refuses for a concrete, nameable reason would make `doctor` the less useful of
        # the two commands an operator reaches for first.
        report = asyncio.run(
            run_doctor(
                host=host,
                store=store,
                catalog_root=catalog_root,
                support_probe=probe_host_support(host),
            )
        )
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    typer.echo(format_doctor_report(report))


# --------------------------------------------------------------------------
# audit parity | prompts | decisions | steps | loop | capabilities |
# recovery | completeness | lineage | immutability | builds | models | secrets
# --------------------------------------------------------------------------

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


@audit_app.command("recovery")
def audit_recovery(run_id: str, store_path: Path | None = _StorePathOption) -> None:
    """Crash/resume event pairs, and abandoned-vs-authoritative attempts per
    turn (SC-003, SC-011, SC-021)."""
    _run_store_only_audit(audit_module.audit_recovery, "recovery", run_id, store_path)


@audit_app.command("completeness")
def audit_completeness(run_id: str, store_path: Path | None = _StorePathOption) -> None:
    """Zero silently missing turns or steps in RUN_ID (SC-003, SC-011)."""
    _run_store_only_audit(audit_module.audit_completeness, "completeness", run_id, store_path)


@audit_app.command("lineage")
def audit_lineage(branch_id: str, store_path: Path | None = _StorePathOption) -> None:
    """BRANCH_ID records its parent run and turn, a matching branch_created
    event, and the parent actually has a save point there (FR-033)."""
    _run_store_only_audit(audit_module.audit_lineage, "lineage", branch_id, store_path)


@audit_app.command("immutability")
def audit_immutability(run_id: str, store_path: Path | None = _StorePathOption) -> None:
    """Every record RUN_ID owns still names RUN_ID, never a child's
    (invariant I12, FR-034)."""
    _run_store_only_audit(audit_module.audit_immutability, "immutability", run_id, store_path)


@audit_app.command("builds")
def audit_builds(
    seed_set: str,
    seedset_root: Path = _SeedsetRootOption,
    store_path: Path | None = _StorePathOption,
) -> None:
    """A set carrying any accepted build change reports as non-uniform
    (FR-031, quickstart.md Scenarios 5 and 9).

    `MatchStore` has no accessor from a seed set to the runs that used it
    (see `operator/audit.py`'s module docstring) -- this command therefore
    reports the set's own uniformity and accepted transitions honestly,
    with `run_enumeration_supported: False`, rather than fabricating a
    partition of runs it has no way to look up.
    """
    path = _seed_set_path(seedset_root, seed_set)
    if not path.is_file():
        typer.echo(f"audit builds failed: no seed set file at {path}", err=True)
        raise typer.Exit(code=2)
    try:
        loaded = load_seed_set_file(path)
    except HarnessError as exc:
        typer.echo(f"audit builds failed: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    store = _open_store(store_path)
    report = audit_module.audit_builds(store, loaded)
    typer.echo(audit_module.format_audit_report("builds", report))
    raise typer.Exit(code=audit_module.exit_code_for(report))


@audit_app.command("models")
def audit_models(run_id: str, store_path: Path | None = _StorePathOption) -> None:
    """Every call names its served model with latency, cost, and retry
    count, rolled up per turn (SC-016)."""
    _run_store_only_audit(audit_module.audit_models, "models", run_id, store_path)


@audit_app.command("secrets")
def audit_secrets(run_id: str, store_path: Path | None = _StorePathOption) -> None:
    """Zero credential-shaped values appear in RUN_ID's records or captures
    (SC-016, SC-018)."""
    _run_store_only_audit(audit_module.audit_secrets, "secrets", run_id, store_path)


# --------------------------------------------------------------------------
# Default wiring (T209) -- "call configure_runner_factory once at process
# start-up" is satisfied here, at import, rather than left to a bootstrap
# module no production entry point ever had. Assigning the factory (never
# calling it) keeps this free: nothing is constructed, no catalog is loaded
# and no socket is opened until a `run` subcommand actually asks for a runner.
# --------------------------------------------------------------------------
configure_runner_factory(default_runner_factory)


if __name__ == "__main__":
    app()
