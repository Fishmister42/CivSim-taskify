"""Archival (T170, FR-036, invariant I17, research R17).

``archive_run`` is the **only** operation that makes a save point eligible
for removal, anywhere in this codebase (contracts/match-store-port.md
A1-A4). This module does not reimplement that transition -- it delegates
entirely to ``MatchStore.archive_run``, which ``store/sqlite_adapter.py``
(T040) already enforces in full: sets ``Run.archived_at``, writes a
``run_archived`` event, transitions that run's *retained* save points to
``eligible``, leaves every record/event/capture untouched, and is rejected
outright on a non-terminal run.

**No age, quota, retention-window, or thinning rule exists anywhere in this
module, and none should be added here -- search it.** The only "when" this
module recognises for a save becoming eligible is "an operator called this
function, naming themselves", never a duration, a count, or a run's own
terminal-ness taken on its own account. Every turn-start save is a branch
point (constitution Principle IV); an automatic rule -- however generous or
however sensible it would look in review -- discards branch points nobody
decided to give up, silently, long after the run, when nobody is watching
(research R17). If a future change to this file ever computes an age,
counts saves, or reads a duration/threshold from configuration, that change
is the exact regression this module exists to prevent.
"""

from __future__ import annotations

from civsim_harness.models.common import RunId, Timestamp
from civsim_harness.store.port import MatchStore


def archive_run(store: MatchStore, run_id: RunId, *, by: str, at: Timestamp) -> None:
    """The one and only way a save point becomes eligible for removal
    (FR-036, A1-A4).

    *by* identifies the operator invoking this -- required and non-empty,
    since an unattributed archival would defeat the audit trail this
    mechanism exists to be (research R17: "an automatic rule ... nobody
    decided to give up"). Raises whatever ``MatchStore.archive_run`` raises
    on a non-terminal run (A3) or an already-archived run (a no-op, not an
    error, per the reference adapter) -- this function adds no rule of its
    own beyond that non-empty-``by`` check.
    """
    if not by:
        raise ValueError("archive_run requires a non-empty 'by' identifying the operator")
    store.archive_run(run_id, by=by, at=at)
