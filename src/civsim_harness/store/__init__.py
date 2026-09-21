"""The match-tracking store: deliverable 3's contract and its SQLite + blob reference adapter.

Since `specs/003-match-tracking-store` this package **is** the store, not an interim stand-in:

- :mod:`civsim_harness.store.port` -- 002's `MatchStore` Protocol, its `TurnCycleRecord` /
  `DecisionStepBundle` write-bundle types, and `StoreHealth`. **Unchanged** by 003: the harness
  keeps binding to this and nothing wider (003 FR-016).
- :mod:`civsim_harness.store.contract` -- `MatchTrackingStore`, the published deliverable-3
  contract (`specs/003-match-tracking-store/contracts/match-tracking-store.md`) extending
  `MatchStore` with the reads the web interface probes for (E1), paged listing, attempt
  addressing, tagged capture images, model-call rows and totals, store-owned completeness,
  trends and divergence, store info, and `export_run` / `import_run`; plus every read value
  type those return.
- :mod:`civsim_harness.store.schema` -- the store **file**'s schema version (1.1), version
  detection, and the copy-first migration from the 1.0 file 002 wrote (V1-V3).
- :mod:`civsim_harness.store.sqlite_reads` -- `SqliteReadBase`: connection, transactions, and
  every published read (R1-R8, T1-T4, V4). Opened `read_only=True`, this is the whole store a
  reader such as the web interface gets.
- :mod:`civsim_harness.store.sqlite_adapter` -- `SqliteMatchStore`: the writes (D1-D6, A1-A4,
  W1-W5) on top of `SqliteReadBase`.
- :mod:`civsim_harness.store.trends` -- pure metric / fingerprint / exclusion derivations.
- :mod:`civsim_harness.store.bundle` -- `RunRecordSet` to and from a bundle directory or
  `.tar.gz` (V5, V6), over the contract only.
- :mod:`civsim_harness.store.completeness` -- the completeness rules (002 T158); the adapter
  applies the same rules inside its own write transactions (W3).
- :mod:`civsim_harness.store.guard` -- the write-before-advance guard (002 T046, FR-013).

`civsim_web` is deliberately **not** modified by deliverable 3: its reads keep working
unchanged against this store, and the three capabilities it still probes for by name are now
real implementations under exactly those names (E1, E2). :func:`open_read_only` is the one
convenience this package offers that consumer -- `civsim-web --store
civsim_harness.store:open_read_only` -- so no code in `civsim_web` has to learn this adapter.
"""

from __future__ import annotations

import os
from pathlib import Path

from civsim_harness.store.contract import MatchTrackingStore
from civsim_harness.store.guard import (
    TurnPersistedToken,
    advance_turn,
    persist_turn_before_advance,
    write_then_advance,
)
from civsim_harness.store.port import DecisionStepBundle, MatchStore, StoreHealth, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

#: Mirrors ``operator/cli.py``'s ``DEFAULT_STORE_PATH_ENV_VAR`` / ``DEFAULT_STORE_PATH`` --
#: spelled here too so a reader process that never imports the operator CLI resolves the same
#: file the harness writes.
STORE_PATH_ENV_VAR = "CIVSIM_STORE_PATH"
DEFAULT_STORE_FILE = "civsim-match-store.db"


def open_read_only(path: str | os.PathLike[str] | None = None) -> SqliteMatchStore:
    """Open the configured store for readers (003 T039; research R10).

    *path* defaults to ``$CIVSIM_STORE_PATH``, then ``./civsim-match-store.db``. The store never
    migrates in this mode: a 1.0 file is refused with the command that will migrate it.
    """
    resolved = (
        Path(path)
        if path is not None
        else Path(os.environ.get(STORE_PATH_ENV_VAR, DEFAULT_STORE_FILE))
    )
    return SqliteMatchStore(resolved, read_only=True)


__all__ = [
    "DEFAULT_STORE_FILE",
    "STORE_PATH_ENV_VAR",
    "DecisionStepBundle",
    "MatchStore",
    "MatchTrackingStore",
    "StoreHealth",
    "TurnCycleRecord",
    "SqliteMatchStore",
    "TurnPersistedToken",
    "advance_turn",
    "open_read_only",
    "persist_turn_before_advance",
    "write_then_advance",
]
