"""Save point addressing (T165, FR-032) and missing-save reporting (T172,
the ``saves/`` half; ``resilience/recovery.py``'s half belongs to another
wave).

FR-032: every save point must be addressable **by run, turn, and lineage,
without inspecting the filesystem or the game client**. ``save_name``
(``saves/save_point.py``'s ``civsim__<run_id>__t<turn:04d>`` convention) is
a naming convention, not the address -- this module resolves a save purely
from ``MatchStore`` records, and nothing here ever opens a file or talks to
the game client. Verifying that a resolved save's *file* is actually
present is a different, filesystem-touching concern (``saves/verify.py``),
deliberately kept out of this module.

FR-036's other half lives here too: "a recovery [or branch] that finds a
required save missing MUST report it rather than resume from a different
turn." A save point already recorded absent -- ``missing=True``, or
``retention_status=removed`` after the reaper has run (``saves/reaper.py``,
T171) -- is refused outright by :func:`require_available_save_point`, never
silently retargeted to a nearby turn. :func:`report_save_missing` is the
other direction: given an absence *discovered* elsewhere (by whatever code
actually inspected the game client or filesystem -- this module never does
that itself), it durably records the finding.
"""

from __future__ import annotations

import uuid

from civsim_harness.errors import PreflightError
from civsim_harness.models.common import EventId, RunId, Timestamp
from civsim_harness.models.records import RetentionStatus, RunEvent, RunEventType, SavePoint
from civsim_harness.saves.save_point import save_name_for
from civsim_harness.store.port import MatchStore


class SaveAddressingError(PreflightError):
    """A save point could not be resolved by run, turn, and lineage, or was
    already recorded absent (FR-032, FR-036). Never worked around by
    falling back to a nearby turn -- the caller gets the exact missing save
    named instead.
    """


def find_save_point(store: MatchStore, run_id: RunId, turn: int) -> SavePoint | None:
    """Resolve the save point for (*run_id*, *turn*) purely from store
    records (FR-032) -- never the filesystem or the game client.

    Returns ``None`` when no such save point was ever recorded (a genuine
    turn gap, or a turn/run that never took one); this is not itself an
    error, since some callers (e.g. an existence probe) legitimately want to
    tell "never recorded" apart from "recorded, then found missing".
    """
    for save_point in store.list_save_points(run_id):
        if save_point.turn_number == turn:
            return save_point
    return None


def resolve_save_point(store: MatchStore, run_id: RunId, turn: int) -> SavePoint:
    """As :func:`find_save_point`, but raises :class:`SaveAddressingError`
    when no record exists at all, naming the save name a caller would have
    expected (``saves/save_point.py``'s naming convention) even though that
    name was never the address (FR-032).
    """
    save_point = find_save_point(store, run_id, turn)
    if save_point is None:
        raise SaveAddressingError(
            "no save point is recorded for this run and turn",
            detail={
                "run_id": run_id,
                "turn": turn,
                "expected_save_name": save_name_for(run_id, turn),
            },
        )
    return save_point


def require_available_save_point(store: MatchStore, run_id: RunId, turn: int) -> SavePoint:
    """:func:`resolve_save_point`, additionally rejecting a save point
    already recorded absent -- ``missing`` (recovery/branching found it
    gone) or ``retention_status=removed`` (the reaper deleted its file,
    T171) -- rather than silently proceeding or retargeting to a nearby
    turn (FR-036: "a required save found missing ... fails, never resuming
    from a different turn").

    This is the check both branch creation (``saves/branching.py``, T167)
    and a future resume path should call before attempting to actually load
    a save: it never touches the filesystem itself, it only refuses to hand
    back a save the store already knows is gone.
    """
    save_point = resolve_save_point(store, run_id, turn)
    if save_point.missing or save_point.retention_status == RetentionStatus.REMOVED:
        raise SaveAddressingError(
            "the required save point is recorded as no longer present on disk",
            detail={
                "run_id": run_id,
                "turn": turn,
                "save_point_id": save_point.save_point_id,
                "save_name": save_point.save_name,
                "missing": save_point.missing,
                "retention_status": save_point.retention_status.value,
            },
        )
    return save_point


def report_save_missing(
    store: MatchStore, save_point: SavePoint, *, occurred_at: Timestamp
) -> SavePoint:
    """Durably record that *save_point* was found absent (T172): writes a
    ``save_missing`` :class:`RunEvent` and marks the ``SavePoint`` record
    ``missing=True`` through the store.

    This module never performs the filesystem or game-client check that
    *discovers* the absence -- that is ``resilience/recovery.py``'s half of
    T172 (or, for a branch, whatever inspected the save before calling
    here). This function only makes the finding a durable, auditable fact,
    so a later attempt to resolve the same save fails explainably via
    :func:`require_available_save_point` rather than mysteriously.
    """
    updated = save_point.model_copy(update={"missing": True})
    store.write_save_point(updated)
    store.write_run_event(
        RunEvent(
            event_id=EventId(uuid.uuid4().hex),
            run_id=save_point.run_id,
            turn_number=save_point.turn_number,
            event_type=RunEventType.SAVE_MISSING,
            occurred_at=occurred_at,
            detail={
                "save_point_id": save_point.save_point_id,
                "save_name": save_point.save_name,
            },
        )
    )
    return updated
