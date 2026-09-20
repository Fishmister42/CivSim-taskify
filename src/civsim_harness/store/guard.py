"""The write-before-advance guard (T046).

FR-013 makes a successful, durably-acknowledged `write_turn_cycle` call a
*precondition* of ending a turn -- the end-turn action is issued "on the
strength of" that return (contracts/match-store-port.md D1), and invariant
I3 states it plainly: "No turn ends before its complete record is durably
persisted."

This module makes that a structural property of the code rather than a
convention a caller has to remember to uphold. `TurnPersistedToken` is the
credential: the *only* function in this codebase that constructs one is
`persist_turn_before_advance` below, and it can only reach the line that
constructs one by first calling `store.write_turn_cycle(record)` and having
that call return normally. Per the port's own D1/D2 contract,
`write_turn_cycle` either returns after the record is durable, or raises --
there is no third outcome, and this module does not catch that exception,
so a failed write propagates straight out and no token is ever produced
(D2, FR-013, invariant I3): "a failed write raises and halts the run."

`advance_turn` is the consuming half: the only sanctioned way in this
module to invoke an end-turn callable, and its signature *requires* a
`TurnPersistedToken` -- there is no overload that accepts a bare
run/turn/attempt triple instead. A caller with no token has nothing to pass
here. The intended usage is that the harness's turn loop imports and calls
only `write_then_advance` (or the `persist_turn_before_advance` /
`advance_turn` pair, when the write and the end-turn call happen in
different places) from this module, and never calls its own end-turn
primitive directly -- at which point "was the write acknowledged before we
advanced?" stops being a question a reviewer has to trace through the run
loop by hand and becomes a question the type checker already answered:
nothing else in the run loop's signatures can produce a
`TurnPersistedToken`, so nothing else can call end-turn.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from civsim_harness.models.common import RunId, TurnCycleId
from civsim_harness.store.port import MatchStore, TurnCycleRecord


@dataclass(frozen=True)
class TurnPersistedToken:
    """Proof that `write_turn_cycle` durably acknowledged one exact turn
    attempt.

    Binds the same identity the store's own idempotency key uses
    (`run_id`, `turn_number`, `attempt_index`, D4) plus the id the store
    returned, so a downstream caller can log or assert against exactly
    which attempt it is ending.

    The only place in this codebase that constructs one is
    `persist_turn_before_advance`. Nothing prevents another module from
    calling this constructor directly -- Python has no sealed types -- but
    every consumer of a token in this module requires one as its *only* way
    to obtain the capability to advance, so doing so is a deliberate act of
    working around the guard, not an accident of a forgotten check.
    """

    run_id: RunId
    turn_number: int
    attempt_index: int
    turn_cycle_id: TurnCycleId


def persist_turn_before_advance(store: MatchStore, record: TurnCycleRecord) -> TurnPersistedToken:
    """The only sanctioned way to obtain a `TurnPersistedToken` (FR-013, I3).

    Calls `store.write_turn_cycle(record)` with no try/except around it: a
    failed write's exception (a `StoreWriteError` per D2) propagates to the
    caller unmodified, halting the run, and this function never reaches the
    line that would mint a token. A caller that does not go through this
    function has no legitimate way to obtain one.
    """
    turn_cycle_id = store.write_turn_cycle(record)
    tc = record.turn_cycle
    return TurnPersistedToken(
        run_id=tc.run_id,
        turn_number=tc.turn_number,
        attempt_index=tc.attempt_index,
        turn_cycle_id=turn_cycle_id,
    )


def advance_turn[T](token: TurnPersistedToken, end_turn: Callable[[TurnPersistedToken], T]) -> T:
    """Invoke *end_turn*, but only given a `TurnPersistedToken` (FR-013, I3).

    `end_turn` receives the token so it can assert or log exactly which
    attempt it is ending. There is no variant of this function that accepts
    a plain `(run_id, turn_number, attempt_index)` triple instead of a
    token -- the type checker rejects any call site that has not first gone
    through `persist_turn_before_advance`, which is what makes this a
    structural guard rather than a checklist item a caller could skip.
    """
    return end_turn(token)


def write_then_advance[T](
    store: MatchStore,
    record: TurnCycleRecord,
    end_turn: Callable[[TurnPersistedToken], T],
) -> T:
    """Persist the turn, then advance -- in that order, with no gap in
    between where a caller could invoke *end_turn* without having written
    anything at all (FR-013, invariant I3).

    Equivalent to `advance_turn(persist_turn_before_advance(store, record),
    end_turn)`, kept as one call so the run loop has a single entry point
    for the common case where the write and the end-turn call happen in the
    same place. If `store.write_turn_cycle` raises, this function raises
    too -- *end_turn* is never called, and the run halts (D2, FR-013).
    """
    token = persist_turn_before_advance(store, record)
    return advance_turn(token, end_turn)
