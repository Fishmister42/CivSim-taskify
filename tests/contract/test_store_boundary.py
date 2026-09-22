"""Principle I, as an import-graph fact (003 FR-029; contract B1; research R11) -- plus, below,
FR-006's structural boundary: the store exposes no operation that deletes or edits a turn, step,
capture, event or model call.

Nothing in the store may ever be read into the playing agent's context. The only agent-facing
path in the system is observation assembly, and it does not read the store. This test makes
that a property of the import graph rather than of reviewer diligence, the same way
``tests/unit/test_platform_neutrality.py`` does for OS libraries:

1. **Statically**: no module under the agent-facing packages names ``civsim_harness.store`` in
   an import statement.
2. **At runtime**: importing every one of those modules in a fresh interpreter never pulls a
   ``civsim_harness.store`` module into ``sys.modules`` transitively.

The package list is the one the contract names. ``run/``, ``operator/`` and ``saves/`` are
deliberately *not* on it: they are the harness's own write and control paths, and they must
import the store to persist through it.
"""

from __future__ import annotations

import ast
import json
import pkgutil
import re
import subprocess
import sys
from pathlib import Path

import pytest

import civsim_harness
from civsim_harness.store.contract import MUTATING_OPERATIONS, MatchTrackingStore
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

AGENT_FACING_PACKAGES: tuple[str, ...] = (
    "civsim_harness.observe",
    "civsim_harness.parity",
    "civsim_harness.agent",
    "civsim_harness.capability",
    "civsim_harness.act",
    "civsim_harness.provider",
)

FORBIDDEN_PREFIX = "civsim_harness.store"


def _modules_under(package_name: str) -> list[str]:
    package = __import__(package_name, fromlist=["_"])
    names = [package_name]
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{package_name}."):
        names.append(info.name)
    return names


def _source_path(module_name: str) -> Path:
    root = Path(civsim_harness.__file__).resolve().parent.parent
    relative = Path(*module_name.split("."))
    candidate = root / relative
    if candidate.is_dir():
        return candidate / "__init__.py"
    return candidate.with_suffix(".py")


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_agent_facing_packages_never_name_the_store_statically() -> None:
    offenders: dict[str, list[str]] = {}
    for package_name in AGENT_FACING_PACKAGES:
        for module_name in _modules_under(package_name):
            hits = sorted(
                name
                for name in _imports_in(_source_path(module_name))
                if name == FORBIDDEN_PREFIX or name.startswith(f"{FORBIDDEN_PREFIX}.")
            )
            if hits:
                offenders[module_name] = hits
    assert not offenders, f"agent-facing modules import the store: {offenders}"


def test_agent_facing_packages_never_load_the_store_at_runtime() -> None:
    modules = [name for package in AGENT_FACING_PACKAGES for name in _modules_under(package)]
    script = (
        "import importlib, json, sys\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        f"loaded = sorted(m for m in sys.modules if m.startswith({FORBIDDEN_PREFIX!r}))\n"
        "print(json.dumps(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=300
    )
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert loaded == [], f"importing agent-facing packages loaded the store: {loaded}"


# --------------------------------------------------------------------------
# FR-006: the store exposes no operation that deletes or edits a record
# --------------------------------------------------------------------------
#
# spec.md FR-006's first clause (abandoned/superseded attempts retained and marked, never
# deleted) is exercised elsewhere in this suite. This is the second clause, checked
# structurally rather than by omission: the store's mutating surface is the closed,
# enumerated ``MUTATING_OPERATIONS`` (store/contract.py), and no public operation name -- on
# the ``MatchTrackingStore`` Protocol or on the concrete ``SqliteMatchStore`` -- matches
# delete/edit vocabulary, regardless of whether it happens to already be sorted into that list.

#: Word/prefix vocabulary FR-006 rules out outright. Matched case-insensitively, as a substring,
#: so both a whole word (``clear``) and a prefix (``deleteRow`` / ``delete_turn_cycle``) are
#: caught.
_FORBIDDEN_VOCABULARY: tuple[str, ...] = (
    "delete",
    "remove",
    "drop",
    "purge",
    "erase",
    "truncate",
    "destroy",
    "edit",
    "rewrite",
    "overwrite",
    "clear",
    "unlink",
)

_VOCAB_PATTERN = re.compile(
    "(" + "|".join(re.escape(word) for word in _FORBIDDEN_VOCABULARY) + ")", re.IGNORECASE
)


def _public_callables(cls: type) -> set[str]:
    """Every public (non-underscore) name on *cls*, including inherited ones, that is callable.

    ``dir()`` walks the whole MRO -- unlike ``vars(cls)``, which sees only names declared
    directly on *cls* -- so this reaches a Protocol's inherited floor (``MatchTrackingStore``
    extends ``MatchStore``) and a concrete adapter's mixed-in reads (``SqliteMatchStore``
    extends ``SqliteReadBase``) alike: exactly the surface a caller can actually reach through
    an instance of *cls*. A ``@property`` is excluded automatically: ``getattr(cls, name)`` on
    a *class* returns the descriptor object itself, which is not callable, so a property can
    never masquerade as an operation here.
    """
    names: set[str] = set()
    for name in dir(cls):
        if name.startswith("_"):
            continue
        try:
            value = getattr(cls, name)
        except AttributeError:
            continue
        if callable(value):
            names.add(name)
    return names


def _forbidden_vocabulary_hits(names: set[str]) -> dict[str, list[str]]:
    """``{name: [matched words]}`` for every name containing delete/edit vocabulary."""
    hits: dict[str, list[str]] = {}
    for name in names:
        matches = sorted({m.lower() for m in _VOCAB_PATTERN.findall(name)})
        if matches:
            hits[name] = matches
    return hits


#: Every read this store publishes, by name: the 002 ``MatchStore`` floor (11 reads + ``ping``)
#: plus this deliverable's own additions (``store/contract.py``, ``MatchTrackingStore``). Pinned
#: here, literally, rather than derived as "whatever ``MUTATING_OPERATIONS`` doesn't cover" --
#: that derivation would make the partition check below a tautology (see
#: ``test_protocol_public_surface_partitions_into_mutating_and_reads``). A method the Protocol
#: gains later that is absent from *both* this set and ``MUTATING_OPERATIONS`` fails that test,
#: which is the point: it forces a deliberate choice, not a silent pass-through.
_KNOWN_READ_OPERATIONS: frozenset[str] = frozenset(
    {
        # MatchStore's floor (store/port.py) -- eleven reads plus ping
        "get_run",
        "get_run_configuration",
        "get_turn_cycle",
        "list_save_points",
        "get_last_known_good",
        "list_active_runs",
        "turn_gaps",
        "step_gaps",
        "list_eligible_save_points",
        "get_capture",
        "list_run_events",
        "ping",
        # deliverable 3's own reads (store/contract.py, MatchTrackingStore)
        "list_runs",
        "get_capture_blob",
        "get_turn_cycle_attempt",
        "query_runs",
        "highest_recorded_turn",
        "list_turn_attempts",
        "get_capture_image",
        "list_captures",
        "list_model_calls",
        "model_call_totals",
        "record_completeness",
        "trend_exclusion",
        "metric_series",
        "divergence",
        "store_info",
        "export_run",
    }
)

#: Public names the concrete adapter carries that name no store *data* operation at all --
#: neither a read nor a write of any tracked record -- so they are exempt from the read/mutating
#: partition rather than silently swallowed by it. Kept to one explicit, reviewed member: adding
#: a name here is exactly as deliberate as adding one to ``MUTATING_OPERATIONS`` or to
#: ``_KNOWN_READ_OPERATIONS``, just for the "neither" bucket.
_ADAPTER_LIFECYCLE_EXTRAS: frozenset[str] = frozenset({"close"})


def test_mutating_operations_is_exactly_the_derived_write_surface() -> None:
    """`MUTATING_OPERATIONS` is exactly the nine `MatchStore` writes plus `import_run` (W4) --
    the list the task description derived by hand from port.py/contract.py/sqlite_adapter.py,
    confirmed here against the published tuple rather than trusted."""
    assert set(MUTATING_OPERATIONS) == {
        "create_run",
        "update_run",
        "write_turn_cycle",
        "write_run_event",
        "write_model_call",
        "write_save_point",
        "write_capture",
        "mark_turn_superseded",
        "archive_run",
        "import_run",
    }
    assert len(MUTATING_OPERATIONS) == len(set(MUTATING_OPERATIONS)), (
        "MUTATING_OPERATIONS must name each operation once"
    )


def test_protocol_public_surface_partitions_into_mutating_and_reads() -> None:
    """Every public callable on `MatchTrackingStore` is either in `MUTATING_OPERATIONS` or is
    one of this store's published reads -- the two sets partition the Protocol's whole public
    surface, so a method added to the Protocol without being sorted into one bucket or the
    other is caught here (FR-006)."""
    public = _public_callables(MatchTrackingStore)
    mutating = set(MUTATING_OPERATIONS)
    assert mutating.isdisjoint(_KNOWN_READ_OPERATIONS), (
        "MUTATING_OPERATIONS and the known reads must not overlap -- an operation cannot be "
        "both (FR-006)"
    )
    known = _KNOWN_READ_OPERATIONS | mutating
    assert public == known, (
        "MatchTrackingStore's public surface has drifted from the read/mutating partition this "
        f"test pins (FR-006): unaccounted-for={sorted(public - known)}, "
        f"pinned-but-gone={sorted(known - public)}. A new public method must be filed as a "
        "read in this test's _KNOWN_READ_OPERATIONS or added to MUTATING_OPERATIONS "
        "deliberately -- reviewed against FR-006 either way."
    )


def test_sqlite_match_store_public_surface_partitions_into_mutating_and_reads() -> None:
    """Same partition, over the concrete adapter. This is what would catch a mutating method
    landed on `SqliteMatchStore` or a class it inherits from (the SQLite read base included)
    *without* a matching `MUTATING_OPERATIONS` entry: such a method is neither a Protocol read
    nor a declared lifecycle extra, so it shows up as drift here even though nothing about it
    is delete/edit-shaped by name (FR-006)."""
    protocol_public = _public_callables(MatchTrackingStore)
    adapter_public = _public_callables(SqliteMatchStore)
    expected = protocol_public | _ADAPTER_LIFECYCLE_EXTRAS
    assert adapter_public == expected, (
        "SqliteMatchStore's public surface has drifted from MatchTrackingStore's read/mutating "
        f"partition (FR-006): extra={sorted(adapter_public - expected)}, "
        f"missing={sorted(expected - adapter_public)}. A new public method on the adapter (or "
        "on a base class it inherits, such as the SQLite read base) must be added to "
        "MUTATING_OPERATIONS, to this test's _KNOWN_READ_OPERATIONS, or to "
        "_ADAPTER_LIFECYCLE_EXTRAS -- deliberately, not silently."
    )


@pytest.mark.parametrize("cls", [MatchTrackingStore, SqliteMatchStore], ids=lambda c: c.__name__)
def test_no_public_operation_matches_delete_or_edit_vocabulary(cls: type) -> None:
    """FR-006's second clause, literally: no public operation name may contain delete/edit
    vocabulary, independent of whether it happens to already be catalogued in
    `MUTATING_OPERATIONS` -- the store is supposed to have *no such operation*, not merely one
    that is tracked as dangerous."""
    hits = _forbidden_vocabulary_hits(_public_callables(cls))
    assert not hits, (
        f"{cls.__name__} exposes an operation whose name matches delete/edit vocabulary FR-006 "
        "rules out ('the store MUST expose no operation that deletes or edits a turn, step, "
        f"capture, event or model call'): {hits}"
    )


# --- negative control: prove the vocabulary scan can actually fail (mandatory, T047) ---


def test_vocabulary_scan_negative_control_detects_a_fake_delete_method() -> None:
    """A stand-in type carrying a delete-shaped method must be caught by the exact scan the two
    tests above run, or that scan is not actually checking anything. This is the cheap half of
    the negative control; the other half -- adding a real method to `SqliteMatchStore` itself,
    on the working copy, and watching the suite fail -- is recorded in the task report rather
    than left in the tree (a real delete method must never survive as a merged test fixture)."""

    class _FakeStoreWithADeleteMethod:
        def delete_turn_cycle(self) -> None: ...

        def get_run(self) -> None: ...

    hits = _forbidden_vocabulary_hits(_public_callables(_FakeStoreWithADeleteMethod))
    assert hits == {"delete_turn_cycle": ["delete"]}, hits
