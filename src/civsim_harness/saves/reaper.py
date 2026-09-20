"""The operator-invoked reaper (T171, FR-036, research R17).

The *only* input this module ever reads to decide what may be deleted is
``MatchStore.list_eligible_save_points()`` -- the store's own guarantee
(contracts/match-store-port.md "Archival and retention") that a save point
appears there for exactly one reason: an operator already called
``archive_run`` on its run (``saves/archival.py``, T170). This module trusts
that guarantee and adds no eligibility rule of its own: no age check, no
quota, no size threshold, nothing.

**Never a background job, never run unattended.** There is no scheduling
primitive anywhere in this file -- no loop, no interval, no "run me every N
hours" -- and none should be added: the only way this code executes is a
human (or ``operator/cli.py``'s ``saves reap`` command, T173, itself
requiring an explicit invocation) calling :func:`reap` directly. It
defaults to ``dry_run=True``: an explicit, deliberate ``dry_run=False`` is
required before a single byte is deleted.

**Only save *files* are deleted here -- never the ``SavePoint`` record.**
The port has no delete operation on any record (contracts/match-store-port.md
"Immutability"), and this module does not invent one: after a file is
removed, the record is updated (via the ordinary, always-permitted
``write_save_point`` -- only ``retention_status=eligible`` is reserved to
``archive_run``) to ``retention_status=removed``, so the record survives the
file. A later branch attempt then fails explainably, through
``saves/addressing.py``'s :func:`~civsim_harness.saves.addressing.require_available_save_point`,
rather than mysteriously.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from civsim_harness.host.port import HostPlatform
from civsim_harness.models.records import RetentionStatus, SavePoint
from civsim_harness.saves.verify import SAVE_FILE_SUFFIX, resolve_saves_dir
from civsim_harness.store.port import MatchStore


def _default_delete(path: Path) -> None:
    path.unlink()


@dataclass(frozen=True)
class ReapPlanItem:
    """One save point the reaper considered, and what it found on disk."""

    save_point: SavePoint
    path: Path
    exists: bool


@dataclass(frozen=True)
class ReapReport:
    """What :func:`reap` did (or, under ``dry_run``, would have done)."""

    items: tuple[ReapPlanItem, ...]
    dry_run: bool
    deleted_paths: tuple[Path, ...]

    @property
    def would_delete_paths(self) -> tuple[Path, ...]:
        """Paths that exist on disk for an eligible save point, whether or
        not this call actually deleted them -- what a ``--dry-run`` report
        shows the operator.
        """
        return tuple(item.path for item in self.items if item.exists)


def plan_reap(
    store: MatchStore, host: HostPlatform, *, home: Path | None = None
) -> tuple[ReapPlanItem, ...]:
    """List every candidate file for the save points
    ``list_eligible_save_points()`` returns -- and only those (FR-036, R17)
    -- without deleting anything. Each save's expected path is resolved
    from its ``save_name`` (``saves/save_point.py``'s naming convention)
    through the ``HostPlatform`` port, exactly as ``saves/verify.py`` does
    for a fresh quicksave. A save point whose file cannot be found is
    reported as such (``exists=False``), not skipped silently -- its record
    may still need marking ``removed`` if that has not already happened.
    """
    saves_dir = resolve_saves_dir(host, home=home)
    items: list[ReapPlanItem] = []
    for save_point in store.list_eligible_save_points():
        path = saves_dir / f"{save_point.save_name}{SAVE_FILE_SUFFIX}"
        items.append(ReapPlanItem(save_point=save_point, path=path, exists=path.is_file()))
    return tuple(items)


def reap(
    store: MatchStore,
    host: HostPlatform,
    *,
    dry_run: bool = True,
    home: Path | None = None,
    delete: Callable[[Path], None] = _default_delete,
) -> ReapReport:
    """Delete save **files** for every save point ``list_eligible_save_points()``
    returns, and only those (FR-036, R17). Defaults to ``dry_run=True``, in
    which case nothing is deleted and no record is changed -- the returned
    :class:`ReapReport` is a preview.

    With ``dry_run=False``: for each eligible save point whose file exists,
    *delete* removes it (`Path.unlink` by default); regardless of whether
    the file existed, the ``SavePoint`` record is updated to
    ``retention_status=removed`` via ``write_save_point`` (never deleted --
    the port has no such operation, and this module does not add one).
    """
    items = plan_reap(store, host, home=home)
    deleted: list[Path] = []
    if not dry_run:
        for item in items:
            if item.exists:
                delete(item.path)
                deleted.append(item.path)
            removed = item.save_point.model_copy(
                update={"retention_status": RetentionStatus.REMOVED}
            )
            store.write_save_point(removed)
    return ReapReport(items=items, dry_run=dry_run, deleted_paths=tuple(deleted))
