"""Record-completeness derivation (T158; FR-052, SC-003, SC-011, invariant I11).

``Run.record_completeness_status`` must reflect gaps at *two* grains -- a missing turn
(``MatchStore.turn_gaps``) and a missing step *within* an otherwise-present turn
(``MatchStore.step_gaps``) -- so a turn that is present but internally incomplete (e.g. steps
1, 2, 4 -- step 3 silently missing) is never reported ``complete`` just because the turn number
itself has an authoritative attempt (data-model.md: "``complete`` requires a contiguous
authoritative turn sequence from 1 to the stop turn with no gap markers, **and** a contiguous step
sequence within each of those turns").

**Why this needs more than ``turn_gaps`` alone.** ``MatchStore`` (contracts/match-store-port.md)
has no "highest recorded turn" accessor of its own, and ``turn_gaps`` cannot distinguish "no turns
attempted yet" from "every attempted turn is gap-free" -- both resolve to an empty list (see
``store.sqlite_adapter.turn_gaps``: an empty *present* set of authoritative turn numbers returns
``[]`` exactly like a fully-contiguous one does). ``operator/audit.py`` already solves this the
same way this module does: FR-007 guarantees a quicksave at the start of every turn attempt, so the
distinct turn numbers across ``MatchStore.list_save_points`` are exactly the turns this run has
ever attempted, and their maximum is the range this function checks ``step_gaps`` against.
"""

from __future__ import annotations

from civsim_harness.models.common import RunId
from civsim_harness.models.run import RecordCompletenessStatus
from civsim_harness.store.port import MatchStore


def record_completeness_status(store: MatchStore, run_id: RunId) -> RecordCompletenessStatus:
    """Derive ``Run.record_completeness_status`` from ``turn_gaps`` and ``step_gaps`` (T158).

    - :attr:`~civsim_harness.models.run.RecordCompletenessStatus.UNKNOWN` -- this run has not
      produced a single turn attempt yet (no save points at all, per FR-007): there is nothing yet
      to judge complete or incomplete.
    - :attr:`~civsim_harness.models.run.RecordCompletenessStatus.HAS_GAPS` -- ``turn_gaps`` names a
      turn number with no authoritative attempt, **or** ``step_gaps`` names a missing
      ``step_index`` within some attempted turn's authoritative attempt (T145: either alone is
      sufficient, checked independently).
    - :attr:`~civsim_harness.models.run.RecordCompletenessStatus.COMPLETE` -- every turn from 1 to
      the highest attempted turn has an authoritative attempt, and every one of those attempts has
      a contiguous step sequence -- the only case data-model.md's own definition of ``complete``
      permits (SC-003).

    Derived, never asserted (data-model.md): this is the one function a caller (``run/runner.py``,
    ``operator/audit.py``) is meant to call to obtain the value at all, rather than setting it by
    hand from partial information.
    """
    attempted_turns = {save.turn_number for save in store.list_save_points(run_id)}
    if not attempted_turns:
        return RecordCompletenessStatus.UNKNOWN

    if store.turn_gaps(run_id):
        return RecordCompletenessStatus.HAS_GAPS

    highest_turn = max(attempted_turns)
    for turn in range(1, highest_turn + 1):
        if store.step_gaps(run_id, turn):
            return RecordCompletenessStatus.HAS_GAPS

    return RecordCompletenessStatus.COMPLETE
