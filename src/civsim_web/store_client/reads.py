"""Read compositions, and the optional capabilities this feature probes for.

Routes ask this module for what they need; they never learn the store's shape.
Two reasons, both structural rather than stylistic:

1. **The optional capabilities live behind one door.** ``CaptureBlobReader``
   and its siblings (``store_client/port.py``) are capabilities a store *may*
   offer ahead of deliverable 3 (the amended ``match-store-port.md`` makes them
   obligations there; the interim adapter may predate them, E1). Every probe
   for one happens here, so "which store features are we depending on that the
   contract does not promise?" is answerable by reading one file -- and when
   deliverable 3 lands a read, one file changes. ``get_run_configuration`` has
   already crossed over: it is a *published* read now, keyed by ``run_id``
   (amended contract, Capability extensions E5).
2. **A read that needs several calls is composed once.** A turn needs its
   record, its gaps, and its steps' capture records; assembling that in each
   route would mean three places to get the gap check wrong.

Nothing here writes. The module names no write operation, and
``tests/contract/test_read_only_boundary.py`` scans the whole package to keep
that true.

**Note on typing**: the store arrives as ``Any`` rather than as the ``MatchStore``
Protocol. The boundary test treats naming the seam as something only a listed
set of files may do, and adding this module to that list would widen a check
that is currently exact. Every call below is still one of the port's published
reads, or an explicitly-probed optional capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "TURN_PROBE_LIMIT",
    "AttemptLookup",
    "attempt_index_of",
    "capture_blob",
    "capture_records_for_turn",
    "highest_recorded_turn",
    "last_known_good",
    "latest_authoritative_turn",
    "list_run_events",
    "run_configuration",
    "store_is_reachable",
    "turn_attempt",
    "turn_gaps",
    "turn_record",
]


def store_is_reachable(store: Any) -> tuple[bool, str | None]:
    """``ping()``, as a tagged outcome. Never raises for an ordinary failure.

    A store that raises instead of returning a health record is still simply
    "not reachable" from this feature's point of view -- there is no third
    answer a reader could act on differently.
    """
    try:
        health = store.ping()
    except Exception as exc:  # a store that raises is still "not ok"
        return (False, f"ping raised: {exc}")
    ok = bool(getattr(health, "ok", False))
    return (ok, getattr(health, "detail", None))


def run_configuration(store: Any, run: Any) -> Any | None:
    """The run's ``RunConfiguration``, through the published read.

    ``get_run_configuration`` is a published port read, **keyed by the run's
    own ``run_id``** -- never by ``Run.config_id`` (amended
    ``match-store-port.md``, Capability extensions E5: the read resolves run
    ids and nothing else, so passing a config id to a conforming store answers
    ``None`` at best and, on a config_id/run_id collision, would silently
    fetch some *other* run's configuration).

    ``None`` means this store predates the amendment and does not offer the
    read, or holds no configuration for that run. Either way the caller
    renders those columns *unavailable* with a reason -- never blank, never a
    plausible default.
    """
    reader = getattr(store, "get_run_configuration", None)
    run_id = getattr(run, "run_id", None)
    if reader is None or run_id is None:
        return None
    return reader(run_id)


def capture_blob(store: Any, capture_id: str) -> bytes | None:
    """The capture's image bytes, if this store can resolve ``blob_ref``.

    ``None`` means the store offers no ``CaptureBlobReader`` (the published port
    has no blob read at all) or holds no bytes for that id. The route turns that
    into an explicit answer naming the port gap, never a placeholder image.
    """
    reader = getattr(store, "get_capture_blob", None)
    if reader is None:
        return None
    return reader(capture_id)


def turn_gaps(store: Any, run_id: str) -> tuple[int, ...]:
    """Turn numbers with no authoritative attempt -- read, never re-derived (V5)."""
    return tuple(store.turn_gaps(run_id) or ())


def turn_record(store: Any, run_id: str, turn: int, *, authoritative_only: bool = True) -> Any:
    return store.get_turn_cycle(run_id, turn, authoritative_only=authoritative_only)


def list_run_events(store: Any, run_id: str) -> tuple[Any, ...]:
    return tuple(store.list_run_events(run_id) or ())


def last_known_good(store: Any, run_id: str) -> Any | None:
    """The save a failed or interrupted run should resume from (FR-027)."""
    return store.get_last_known_good(run_id)


def capture_records_for_turn(store: Any, steps: Any) -> dict[str, Any]:
    """``{capture_id: ScreenCapture}`` for every capture the given steps declared.

    A declared capture id whose record the store cannot produce is simply absent
    from the mapping, and ``CaptureView`` renders that as ``missing_record``
    rather than as a step with no capture at all -- a distinction FR-034 wants
    sayable.

    **Takes the steps, not the turn record** (T067). It used to take the record
    and walk ``record.steps``, which meant the turn route -- which calls this
    *before* the step window is applied -- issued one ``get_capture`` per step of
    the whole turn to render a page of fifty. FR-036 is explicit: "viewing a
    turn MUST NOT require loading captures beyond those being viewed". The
    caller now passes exactly the steps it is about to render, so the bound is
    the caller's window rather than the turn's length.
    """
    captures: dict[str, Any] = {}
    for bundle in steps or ():
        observation = getattr(bundle, "observation", None)
        for capture_id in getattr(observation, "captures", ()) or ():
            key = str(capture_id)
            if key in captures:
                continue
            found = store.get_capture(key)
            if found is not None:
                captures[key] = found
    return captures


#: Upper bound on the forward probe below. A Civ VI game ends long before this;
#: the cap exists so a misbehaving store cannot turn one page request into an
#: unbounded read loop, not because turn 4000 is expected to be meaningful.
TURN_PROBE_LIMIT = 4000


def highest_recorded_turn(store: Any, run_id: str) -> int:
    """The highest turn number this run has any record of. ``0`` when none does.

    **A third instance of the C1 gap.** ``match-store-port.md`` publishes no
    "latest turn" or "turn count" read, and the live view's very first question
    (FR-001: which turn is it on?) is exactly that. Two published reads answer
    it between them:

    - ``list_save_points`` -- Principle IV requires a quicksave at the start of
      every turn, so the highest save's turn number bounds the run. This is the
      cheap path and the one a real store takes.
    - a forward probe with ``get_turn_cycle`` -- the fallback for a run with no
      save points recorded yet, bounded by ``TURN_PROBE_LIMIT``.

    Worth raising with deliverable 3 alongside C1: a single indexed read would
    replace both.
    """
    saves = store.list_save_points(run_id) or ()
    numbers = [getattr(save, "turn_number", 0) or 0 for save in saves]
    if numbers:
        return max(numbers)

    gaps = set(turn_gaps(store, run_id))
    highest = 0
    for turn in range(1, TURN_PROBE_LIMIT + 1):
        if turn in gaps:
            continue
        if turn_record(store, run_id, turn) is None:
            break
        highest = turn
    return highest


def latest_authoritative_turn(
    store: Any, run_id: str, *, highest_turn: int | None = None
) -> Any | None:
    """The newest turn with an authoritative record, or ``None``.

    Walks back from the run's highest recorded turn. Gaps are skipped rather
    than reported as the current turn: a gap is precisely what UP-005 forbids
    rendering as confirmed current fact, and "the last turn we actually have" is
    what spec Edge Cases asks for ("the interface shows the last complete turn
    as current and marks the partial one").
    """
    bound = highest_recorded_turn(store, run_id) if highest_turn is None else highest_turn
    gaps = set(turn_gaps(store, run_id))
    for turn in range(bound, 0, -1):
        if turn in gaps:
            continue
        found = turn_record(store, run_id, turn)
        if found is not None:
            return found
    return None


# --------------------------------------------------------------------------
# Attempt addressing (US2 / T036 -- FR-009)
# --------------------------------------------------------------------------


def attempt_index_of(record: Any) -> int:
    """A turn record's ``attempt_index``, defaulting to the first attempt."""
    cycle = getattr(record, "turn_cycle", record)
    return int(getattr(cycle, "attempt_index", 0) or 0)


@dataclass(frozen=True)
class AttemptLookup:
    """The answer to "give me attempt k of this turn", with its limits stated.

    ``addressable`` and ``exhaustive`` exist so a route can tell a caller the
    difference between *this attempt was never recorded* and *this store cannot
    reach it*. Collapsing the two would make a port gap look like a fact about
    the run, which is exactly the kind of absence-rendered-as-fact UP-005
    forbids.
    """

    record: Any | None
    addressable: tuple[int, ...]
    """Attempt indices this lookup could reach. Meaningful only when
    ``exhaustive`` -- a store with the optional capability can reach any."""

    exhaustive: bool
    """``True`` when ``addressable`` is the *complete* set the published port
    can address, i.e. the fallback path was used."""


def turn_attempt(store: Any, run_id: str, turn: int, attempt: int) -> AttemptLookup:
    """One named attempt of one turn (FR-009), through whichever read can reach it.

    **A fourth instance of the C1 gap** (``store_client/port.py``
    ``TurnAttemptReader``). The published port addresses at most two attempts of
    any turn -- the authoritative one and the most recent one -- because
    ``get_turn_cycle`` takes a boolean, not an index. A store offering the
    optional ``TurnAttemptReader`` capability can address any; one that does not
    gets the two, and the route says plainly which those were rather than
    substituting the authoritative attempt for the one that was asked for.
    """
    reader = getattr(store, "get_turn_cycle_attempt", None)
    if reader is not None:
        return AttemptLookup(reader(run_id, turn, attempt), addressable=(), exhaustive=False)

    reachable: dict[int, Any] = {}
    for record in (
        turn_record(store, run_id, turn, authoritative_only=True),
        turn_record(store, run_id, turn, authoritative_only=False),
    ):
        if record is not None:
            reachable.setdefault(attempt_index_of(record), record)
    return AttemptLookup(
        reachable.get(attempt),
        addressable=tuple(sorted(reachable)),
        exhaustive=True,
    )
