"""Save point naming and record writing (T080, FR-032).

``civsim__<run_id>__t<turn:04d>`` is a naming convention, not the address
(data-model.md §13): callers address a save by run, turn, and lineage, never
by path, and this module is the one place that convention is spelled out.
"""

from __future__ import annotations

import uuid
from typing import Any

from civsim_harness.models.common import RunId, SavePointId, Timestamp
from civsim_harness.models.records import RetentionStatus, SavePoint
from civsim_harness.saves.verify import VerifiedSave
from civsim_harness.store.port import MatchStore

#: ``t<turn:04d>`` -- matches ``models.records._SAVE_NAME_PATTERN``
#: (``^civsim__.+__t\\d{4,}$``); four digits is the floor, not a cap, so a
#: run past turn 9999 still produces a valid (if wider) name.
_TURN_WIDTH = 4


def save_name_for(run_id: RunId, turn_number: int) -> str:
    """``civsim__<run_id>__t<turn:04d>`` (FR-032, research R5)."""
    return f"civsim__{run_id}__t{turn_number:0{_TURN_WIDTH}d}"


def build_save_point(
    *,
    run_id: RunId,
    turn_number: int,
    save_name: str,
    verified_save: VerifiedSave | None,
    taken_at: Timestamp,
    lineage: dict[str, Any] | None = None,
    save_point_id: SavePointId | None = None,
) -> SavePoint:
    """Build the ``SavePoint`` record for one turn's quicksave.

    ``verified`` is set from *verified_save* -- i.e. from T079's filesystem
    check -- and never asserted independently of it: passing ``None`` (an
    unverifiable quicksave, ``saves.verify.SaveVerificationError`` already
    raised upstream and the turn already failed) records ``verified=False``
    rather than omitting the save point outright, so a failed verification
    still leaves an auditable trace of the attempt.
    """
    return SavePoint(
        save_point_id=(
            save_point_id if save_point_id is not None else SavePointId(uuid.uuid4().hex)
        ),
        run_id=run_id,
        turn_number=turn_number,
        save_name=save_name,
        taken_at=taken_at,
        verified=verified_save is not None,
        lineage=lineage or {},
        retention_status=RetentionStatus.RETAINED,
        missing=False,
    )


def write_save_point(store: MatchStore, save_point: SavePoint) -> SavePointId:
    """Write *save_point* through the store (FR-032). Durability and failure
    behaviour belong entirely to the ``MatchStore`` port
    (``write_save_point``, D5) -- this is a thin, explicit wrapper kept here
    as the one place this task's naming/record-writing responsibility lives,
    rather than inlined at every call site.
    """
    return store.write_save_point(save_point)
