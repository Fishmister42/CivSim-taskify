"""``civsim store …`` -- the operator's window onto the match-tracking store itself (003 T036).

Eight commands, all over the published contract and nothing else (FR-017). The roster below is
the whole of it -- ``test_the_docstring_roster_is_the_registered_command_set`` fails if a command
is added, removed or renamed without this list moving with it:

- ``info``      schema version, identity, counts, migrations, dangling parents
- ``migrate``   bring a 1.0 file to 1.1 (copy first); ``--dry-run`` says what would change
- ``runs``      the catalog, paged and filtered in the store (FR-011)
- ``model-calls`` a run's model-call rows and totals (FR-015)
- ``coverage``  the claimed harness surface versus what the store says was demonstrated live
- ``export``    a run as a bundle directory, ``--archive`` for the ``.tar.gz`` (FR-027)
- ``import``    a bundle directory or archive into this store (FR-027)
- ``repair``    pause runs whose driver is gone -- ``playing``/``preparing`` with no live lock
                holder -- with the evidence on the event, and remove run-identity locks that
                stand for no live run; ``--dry-run`` lists and changes nothing

``info``, ``runs``, ``model-calls``, ``coverage`` and ``export`` open the store **read-only** --
an operator inspecting a store never migrates it by accident. ``migrate``, ``import`` and
``repair`` are the three writers (``repair --dry-run`` is a read).
Exit code 2 carries a `StoreSchemaError` / `StoreReadError` / `BundleError` message, so a script
can tell "the store refused" from "the command was misused" (exit 1, Typer's own).

The store path resolves exactly as the rest of the CLI resolves it (``--store``, then
``$CIVSIM_STORE_PATH``, then ``./civsim-match-store.db``); the resolver is imported lazily from
``operator/cli.py`` because that module registers this sub-app at import time.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import typer

from civsim_harness.errors import (
    BundleError,
    CatalogError,
    HarnessError,
    StoreReadError,
    StoreSchemaError,
)
from civsim_harness.models.common import RunId
from civsim_harness.models.run import ComparabilityStatus, LifecycleState, RecordCompletenessStatus
from civsim_harness.run.identity_lock import RunIdentityLock
from civsim_harness.run.orphans import (
    DEFAULT_ORPHAN_GRACE_SECONDS,
    OrphanFinding,
    StrayLockDisposition,
    StrayLockFinding,
    clean_stray_locks,
    repair_orphans,
    scan_orphans,
    scan_stray_locks,
)
from civsim_harness.store import schema as store_schema
from civsim_harness.store.bundle import read_bundle, write_bundle
from civsim_harness.store.contract import RunQuery, RunSort
from civsim_harness.store.coverage import (
    compute_coverage,
    load_claimed_surface,
    render_json,
    render_markdown,
)
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

store_app = typer.Typer(
    name="store",
    help="The match-tracking store itself: version, migration, listing, model calls, bundles.",
    no_args_is_help=True,
)

_StoreOption = typer.Option(
    None,
    "--store-path",
    "--store",
    help="Store file (default: $CIVSIM_STORE_PATH, then ./civsim-match-store.db).",
)
_DryRunOption = typer.Option(False, "--dry-run", help="Report what would change; change nothing.")
_SeedSetOption = typer.Option(None, "--seed-set", help="Only runs of this seed set.")
_StateOption = typer.Option(None, "--state", help="Only these lifecycle states (repeatable).")
_CompletenessOption = typer.Option(
    None, "--completeness", help="Only these completeness statuses (repeatable)."
)
_ComparabilityOption = typer.Option(
    None, "--comparability", help="Only these comparability statuses (repeatable)."
)
_ArchivedOption = typer.Option(
    None, "--archived/--not-archived", help="Only archived / only unarchived runs."
)
_PageOption = typer.Option(1, "--page", min=1)
_PageSizeOption = typer.Option(25, "--page-size", min=1, max=500)
_SortOption = typer.Option(
    RunSort.STARTED_AT_DESC.value, "--sort", help="One of the RunSort values."
)
_TurnOption = typer.Option(None, "--turn", help="Only calls at this turn.")
_StepOption = typer.Option(None, "--step", help="Only calls at this step index.")
_OutOption = typer.Option(..., "--out", help="Directory to write the bundle into.")
_ArchiveOption = typer.Option(False, "--archive", help="Also write the .tar.gz of the bundle.")
_RunOption = typer.Option(None, "--run", help="Only this run (repeatable).")
_SinceOption = typer.Option(
    None, "--since", help="This run and every run started at or after it (a block's delta)."
)
_CatalogRootOption = typer.Option(
    Path("catalogs"), "--catalog-root", help="The catalog the claimed surface is loaded from."
)
_ScreensLuaOption = typer.Option(
    None,
    "--screens-lua",
    help="lua/ingame/screens.lua (default: a sibling of the catalog root).",
)
_FormatOption = typer.Option("md", "--format", help="md or json.")


def _resolve(store_path: Path | None) -> Path:
    from civsim_harness.operator.cli import _resolve_store_path  # lazy: cli registers this app

    return _resolve_store_path(store_path)


def _refuse(exc: HarnessError, *, what: str) -> typer.Exit:
    typer.echo(f"{what} refused: {exc.message}", err=True)
    for key, value in exc.detail.items():
        typer.echo(f"  {key}: {value}", err=True)
    return typer.Exit(code=2)


def _open(path: Path, *, read_only: bool) -> SqliteMatchStore:
    try:
        return SqliteMatchStore(path, read_only=read_only)
    except (StoreSchemaError, StoreReadError) as exc:
        raise _refuse(exc, what="store open") from exc


def _peek_version(path: Path) -> str | None:
    """The file's version without opening it as a store (so `info` can describe a 1.0 file)."""
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        detected = store_schema.detect_version(conn)
    finally:
        conn.close()
    return None if detected is None else str(detected)


# --------------------------------------------------------------------------
# info
# --------------------------------------------------------------------------


@store_app.command("info")
def store_info(store_path: Path | None = _StoreOption) -> None:
    """Schema version, identity, record counts and migrations of the store."""
    path = _resolve(store_path)
    version = _peek_version(path)
    typer.echo(f"store            : {path}")
    if version is None:
        typer.echo("schema_version   : (no store file, or an empty one)")
        return
    if version == str(store_schema.LEGACY_SCHEMA_VERSION):
        typer.echo(f"schema_version   : {version} (pre-feature file, 002 layout)")
        typer.echo(
            f"action           : run `{store_schema.MIGRATE_COMMAND}` to bring it to "
            f"{store_schema.STORE_SCHEMA_VERSION} (a copy is taken first)"
        )
        return
    store = _open(path, read_only=True)
    try:
        info = store.store_info()
    finally:
        store.close()
    typer.echo(f"schema_version   : {info.schema_version}")
    typer.echo(f"store_id         : {info.store_id}")
    typer.echo(f"host_platform    : {info.host_platform}")
    for table, count in info.counts.items():
        typer.echo(f"{table:<17}: {count}")
    if info.migrations:
        for record in info.migrations:
            typer.echo(
                f"migration        : {record.migration_id} at {record.applied_at.isoformat()} "
                f"backup={record.backup_path} {dict(record.detail)}"
            )
    else:
        typer.echo("migrations       : none")
    if info.runs_with_absent_parent:
        typer.echo(f"absent parents   : {', '.join(info.runs_with_absent_parent)}")


# --------------------------------------------------------------------------
# migrate
# --------------------------------------------------------------------------


@store_app.command("migrate")
def store_migrate(store_path: Path | None = _StoreOption, dry_run: bool = _DryRunOption) -> None:
    """Migrate a 1.0 store file to the current schema, taking a copy first (FR-024, FR-025)."""
    path = _resolve(store_path)
    if not path.exists():
        typer.echo(f"store migrate: no store file at {path}", err=True)
        raise typer.Exit(code=2)
    if dry_run:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            plan = store_schema.plan_migration(conn)
        finally:
            conn.close()
        typer.echo(f"detected         : {plan['detected']}")
        typer.echo(f"action           : {plan['action']}")
        if plan["changes"]:
            from datetime import UTC, datetime

            typer.echo(
                f"backup would be  : {store_schema.backup_path_for(path, now=datetime.now(UTC))}"
            )
            for key, value in plan["changes"].items():
                typer.echo(f"{key:<17}: {value}")
        return
    store = _open(path, read_only=False)
    try:
        applied = store.migrations_applied_on_open
        version = store.schema_version
    finally:
        store.close()
    if not applied:
        typer.echo(f"already at {version}; nothing to migrate")
        return
    for record in applied:
        typer.echo(f"migrated         : {record.from_version} -> {record.to_version}")
        typer.echo(f"backup           : {record.backup_path}")
        for key, value in record.detail.items():
            typer.echo(f"{key:<17}: {value}")


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


@store_app.command("runs")
def store_runs(
    store_path: Path | None = _StoreOption,
    seed_set: str | None = _SeedSetOption,
    state: list[str] | None = _StateOption,
    completeness: list[str] | None = _CompletenessOption,
    comparability: list[str] | None = _ComparabilityOption,
    archived: bool | None = _ArchivedOption,
    page: int = _PageOption,
    page_size: int = _PageSizeOption,
    sort: str = _SortOption,
) -> None:
    """List runs -- every lifecycle state, archived included -- paged and filtered (FR-011)."""
    try:
        query = RunQuery(
            page=page,
            page_size=page_size,
            sort=RunSort(sort),
            seed_set_id=seed_set,
            lifecycle_states=frozenset(LifecycleState(s) for s in state) if state else None,
            completeness=(
                frozenset(RecordCompletenessStatus(s) for s in completeness)
                if completeness
                else None
            ),
            comparability=(
                frozenset(ComparabilityStatus(s) for s in comparability) if comparability else None
            ),
            archived=archived,
        )
    except ValueError as exc:
        typer.echo(f"store runs: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    store = _open(_resolve(store_path), read_only=True)
    try:
        result = store.query_runs(query)
        # Principle III: a listing that shows completeness but not trend eligibility invites the
        # reader to assume the two are the same question. They are not -- a run can have a
        # gap-free record and still be ineligible because its game turns never advanced
        # (research R14, 2026-09-21). Computed per listed run, over the published read.
        exclusions = {
            run.run_id: store.trend_exclusion(RunId(str(run.run_id))) for run in result.runs
        }
    finally:
        store.close()
    pages = max(1, -(-result.total // result.page_size))
    typer.echo(f"page {result.page} of {pages} (total {result.total}, sort {result.sort.value})")
    typer.echo(
        f"{'run_id':<24} {'state':<16} {'completeness':<12} {'comparability':<18} "
        f"{'started_at':<27} {'archived':<9} trend"
    )
    for run in result.runs:
        exclusion = exclusions[run.run_id]
        typer.echo(
            f"{run.run_id:<24} {run.lifecycle_state.value:<16} "
            f"{run.record_completeness_status.value:<12} {run.comparability_status.value:<18} "
            f"{(run.started_at.isoformat() if run.started_at else '-'):<27} "
            f"{('yes' if run.archived_at else 'no'):<9} "
            f"{'eligible' if exclusion is None else f'not eligible ({exclusion.reason.value})'}"
        )


# --------------------------------------------------------------------------
# model-calls
# --------------------------------------------------------------------------


@store_app.command("model-calls")
def store_model_calls(
    run_id: str,
    store_path: Path | None = _StoreOption,
    turn: int | None = _TurnOption,
    step: int | None = _StepOption,
) -> None:
    """A run's model calls as rows, and their totals, without opening a decision step (FR-015)."""
    store = _open(_resolve(store_path), read_only=True)
    try:
        if store.get_run(RunId(run_id)) is None:
            typer.echo(f"store model-calls: no such run {run_id}", err=True)
            raise typer.Exit(code=2)
        rows = store.list_model_calls(RunId(run_id), turn=turn, step=step)
        totals = store.model_call_totals(RunId(run_id))
    finally:
        store.close()
    typer.echo(
        f"{'turn':>4} {'step':>4} {'outcome':<18} {'served':<28} {'ms':>7} {'usd':>10} fallback"
    )
    for row in rows:
        cost = row.call.cost.amount_usd
        typer.echo(
            f"{(row.turn_number if row.turn_number is not None else '-'):>4} "
            f"{(row.step_index if row.step_index is not None else '-'):>4} "
            f"{row.call.outcome.value:<18} "
            f"{row.call.model_served.provider + '/' + row.call.model_served.model:<28} "
            f"{row.call.latency_ms:>7} {(f'{cost:.6f}' if cost is not None else '-'):>10} "
            f"{'yes' if row.call.fallback_occurred else 'no'}"
        )
    usd = f"{totals.cost_usd:.6f}" if totals.cost_usd is not None else "unpriced"
    typer.echo(
        f"totals           : calls={totals.call_count} priced={totals.priced_call_count} "
        f"cost_usd={usd} input_tokens={totals.input_tokens} output_tokens={totals.output_tokens} "
        f"total_tokens={totals.total_tokens} latency_ms={totals.latency_ms_total} "
        f"fallbacks={totals.fallback_count} retries={totals.retry_count}"
    )


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------


@store_app.command("coverage")
def store_coverage(
    store_path: Path | None = _StoreOption,
    run: list[str] | None = _RunOption,
    since: str | None = _SinceOption,
    catalog_root: Path = _CatalogRootOption,
    screens_lua: Path | None = _ScreensLuaOption,
    output_format: str = _FormatOption,
) -> None:
    """Claimed harness surface versus what the store says was demonstrated on a live client.

    The claimed surface comes from the catalog (through its own loader) and from
    ``lua/ingame/screens.lua``; the demonstrated surface comes from this store's records and
    nothing else. Markdown by default, ready to paste into an issue; ``--format json`` carries
    the same content. ``--run`` and ``--since`` narrow the window to one block of live work.
    """
    if output_format not in {"md", "json"}:
        typer.echo("store coverage: --format must be md or json", err=True)
        raise typer.Exit(code=1)
    try:
        claimed = load_claimed_surface(catalog_root, screens_lua=screens_lua)
    except CatalogError as exc:
        raise _refuse(exc, what="store coverage") from exc

    path = _resolve(store_path)
    store = _open(path, read_only=True)
    try:
        try:
            scorecard = compute_coverage(
                store,
                claimed,
                run_ids=tuple(run or ()),
                since_run_id=since,
                store_path=path,
            )
        except StoreReadError as exc:
            raise _refuse(exc, what="store coverage") from exc
    finally:
        store.close()

    if output_format == "json":
        typer.echo(json.dumps(render_json(scorecard), indent=2, sort_keys=False))
    else:
        typer.echo(render_markdown(scorecard))


# --------------------------------------------------------------------------
# export / import
# --------------------------------------------------------------------------


@store_app.command("export")
def store_export(
    run_id: str,
    out: Path = _OutOption,
    archive: bool = _ArchiveOption,
    store_path: Path | None = _StoreOption,
) -> None:
    """Export RUN_ID as a bundle directory under --out, and with --archive its .tar.gz (FR-027)."""
    store = _open(_resolve(store_path), read_only=True)
    try:
        info = store.store_info()
        try:
            records = store.export_run(RunId(run_id))
        except StoreReadError as exc:
            raise _refuse(exc, what="store export") from exc
    finally:
        store.close()
    try:
        written = write_bundle(
            records,
            out,
            store_schema_version=str(info.schema_version),
            source_store_id=info.store_id,
            archive=archive,
        )
    except BundleError as exc:
        raise _refuse(exc, what="store export") from exc
    typer.echo(f"exported         : {run_id}")
    typer.echo(f"bundle           : {written}")
    for kind, count in records.counts().items():
        typer.echo(f"{kind:<17}: {count}")


@store_app.command("import")
def store_import(path: Path, store_path: Path | None = _StoreOption) -> None:
    """Import a bundle directory or .tar.gz into this store; a run id that exists is refused."""
    try:
        manifest, records = read_bundle(path)
    except BundleError as exc:
        raise _refuse(exc, what="store import") from exc
    store = _open(_resolve(store_path), read_only=False)
    try:
        try:
            imported = store.import_run(records)
        except BundleError as exc:
            raise _refuse(exc, what="store import") from exc
    finally:
        store.close()
    typer.echo(f"imported         : {imported}")
    typer.echo(f"source_host      : {manifest.source_host}")
    typer.echo(f"source_store_id  : {manifest.source_store_id}")
    for kind, count in records.counts().items():
        typer.echo(f"{kind:<17}: {count}")


__all__ = ["store_app"]


# --------------------------------------------------------------------------
# repair
# --------------------------------------------------------------------------

_LockDirOption = typer.Option(
    None,
    "--lock-dir",
    help="Run-identity lock directory (default: the harness's own, under the temp dir).",
)
_GraceOption = typer.Option(
    DEFAULT_ORPHAN_GRACE_SECONDS,
    "--grace-seconds",
    min=0.0,
    help="Leave alone a run whose last recorded activity is younger than this.",
)


_CleanUnrecordedOption = typer.Option(
    False,
    "--clean-unrecorded-locks",
    help=(
        "Also remove locks for run ids this store has no row for. Reported without this; "
        "see `run/orphans.py`'s StrayLockDisposition on why that one needs you."
    ),
)


def _echo_findings(findings: list[OrphanFinding]) -> None:
    if not findings:
        typer.echo("orphaned runs: none")
        return
    typer.echo(f"orphaned runs: {len(findings)}")
    for finding in findings:
        typer.echo("  " + finding.render())


def _echo_unrecorded_hint(findings: list[StrayLockFinding], cleaning: bool) -> None:
    """Name the way out, on the one class this command will not decide by itself.

    An operator who has just been refused a run needs the next command, not a category.
    """
    if cleaning:
        return
    kept = [f for f in findings if f.disposition is StrayLockDisposition.UNRECORDED]
    if not kept:
        return
    typer.echo(
        f"{len(kept)} lock(s) name a run this store has no row for and were kept: a run "
        "recorded in another store looks the same from here. If this is the only store, "
        "`--clean-unrecorded-locks` removes them."
    )


def _echo_stray_locks(findings: list[StrayLockFinding]) -> None:
    if not findings:
        typer.echo("stray run-identity locks: none")
        return
    typer.echo(f"stray run-identity locks: {len(findings)}")
    for finding in findings:
        typer.echo("  " + finding.render())


@store_app.command("repair")
def store_repair(
    store_path: Path | None = _StoreOption,
    dry_run: bool = _DryRunOption,
    lock_dir: Path | None = _LockDirOption,
    grace_seconds: float = _GraceOption,
    clean_unrecorded_locks: bool = _CleanUnrecordedOption,
) -> None:
    """Pause runs whose driver is gone (`run/orphans.py`, 2026-09-21).

    A run left in ``playing`` or ``preparing`` whose run-identity lock is absent, unreadable,
    or names a dead process is nobody's: its driver was killed, crashed, or lost with the
    host, and nothing will ever transition it. Left as is, it reads as *live* to every
    downstream consumer and hides the gap at its last, unfinished turn (Principle III).
    ``repair`` moves each such run to ``paused`` -- never ``finished`` or ``failed`` -- and
    records a ``lifecycle_transition`` event with ``reason: orphaned`` and everything it
    checked (lock path, PID and liveness, last activity and how stale it was). A run whose
    lock holder is alive is never touched. ``--dry-run`` opens the store read-only and lists
    the same evidence without writing a byte.

    It also repairs the other direction (2026-09-22): a **lock** standing for no live run.
    A run that finishes without ever being written to a store leaves a lock file behind,
    and because the Civ VI client outlives its driver that lock goes on naming a live PID
    -- so ``acquire`` refuses the next run on that client and nothing anywhere says why.
    Every lock file older than the grace window is listed with its evidence. Two kinds are
    removed: one whose run this store records as ``finished``/``failed``, and one whose
    holder process is gone. A lock for a run id this store has **no row for** is reported
    and kept, because a run recorded in a *different* store looks identical from here --
    ``--clean-unrecorded-locks`` is the operator saying it does not.
    """
    path = _resolve(store_path)
    if not path.exists():
        # A write-mode open would create an empty store here; repairing nothing into a new
        # file is not what an operator pointing at the wrong path wants.
        typer.echo(f"store repair refused: no store file at {path}", err=True)
        raise typer.Exit(code=2)
    lock = RunIdentityLock(lock_dir) if lock_dir is not None else RunIdentityLock()
    now = datetime.now(UTC)
    typer.echo(f"store            : {path}")
    typer.echo(f"lock directory   : {lock.lock_dir}")
    typer.echo(f"grace seconds    : {grace_seconds:g}")
    if dry_run:
        store = _open(path, read_only=True)
        try:
            findings = scan_orphans(store, lock=lock, now=now, grace_seconds=grace_seconds)
            strays = scan_stray_locks(store, lock=lock, now=now, grace_seconds=grace_seconds)
        finally:
            store.close()
        _echo_findings(findings)
        _echo_stray_locks(strays)
        would_remove = [s for s in strays if s.cleanable or clean_unrecorded_locks]
        typer.echo(
            f"dry run: {len(findings)} run(s) would be paused, "
            f"{len(would_remove)} lock(s) removed; nothing changed"
        )
        _echo_unrecorded_hint(strays, clean_unrecorded_locks)
        return
    try:
        # orphan_sweep=False: the open must not pre-empt the command, or this listing
        # would always read "none" for the runs the open just repaired.
        store = SqliteMatchStore(path, orphan_sweep=False)
    except (StoreSchemaError, StoreReadError) as exc:
        raise _refuse(exc, what="store open") from exc
    try:
        findings = scan_orphans(store, lock=lock, now=now, grace_seconds=grace_seconds)
        _echo_findings(findings)
        applied = repair_orphans(store, findings, now=now)
        # After the pauses: a run just moved to `paused` is no longer in flight, and its
        # lock -- if its holder is dead -- is now removable on the same evidence.
        strays = scan_stray_locks(store, lock=lock, now=now, grace_seconds=grace_seconds)
    finally:
        store.close()
    _echo_stray_locks(strays)
    removed = clean_stray_locks(strays, lock=lock, include_unrecorded=clean_unrecorded_locks)
    typer.echo(f"paused {len(applied)} orphaned run(s)")
    typer.echo(f"removed {len(removed)} stray run-identity lock(s)")
    _echo_unrecorded_hint(strays, clean_unrecorded_locks)
