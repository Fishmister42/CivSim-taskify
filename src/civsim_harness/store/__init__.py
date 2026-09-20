"""MatchStore port, SQLite+blob reference adapter, write-before-advance guard.

- :mod:`civsim_harness.store.port` -- the `MatchStore` Protocol, its
  `TurnCycleRecord` / `DecisionStepBundle` write-bundle types, and
  `StoreHealth` (T038).
- :mod:`civsim_harness.store.sqlite_adapter` -- `SqliteMatchStore`, the
  interim SQLite + content-addressed blob reference implementation (T039,
  T040).
- :mod:`civsim_harness.store.guard` -- the write-before-advance guard: a
  turn cannot be ended without first presenting proof of a durably
  acknowledged `write_turn_cycle` (T046, FR-013, invariant I3).

Later waves should bind to the names re-exported here (or import the
submodules directly) rather than reaching into `sqlite_adapter` for
anything beyond `SqliteMatchStore` itself -- everything else is an
implementation detail of the reference adapter, not part of the port.
"""

from __future__ import annotations

from civsim_harness.store.guard import (
    TurnPersistedToken,
    advance_turn,
    persist_turn_before_advance,
    write_then_advance,
)
from civsim_harness.store.port import DecisionStepBundle, MatchStore, StoreHealth, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

__all__ = [
    "DecisionStepBundle",
    "MatchStore",
    "StoreHealth",
    "TurnCycleRecord",
    "SqliteMatchStore",
    "TurnPersistedToken",
    "advance_turn",
    "persist_turn_before_advance",
    "write_then_advance",
]
