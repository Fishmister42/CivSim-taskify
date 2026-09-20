"""The read-only boundary, checked from the import graph (plan.md Constraints).

plan.md commits to enforcing "no route, handler, or dependency in this codebase
may import or call a `MatchStore` write method" with "a lint/import-boundary
check over the store client module, not by convention alone" (FR-023, FR-024,
FR-026). This is that check.

It reads the AST rather than the text, which matters: `store_client/port.py`
and `store_client/fake.py` both *name* the nine write operations in prose, to
document what they exclude. A text scan would flag exactly the two files whose
job is to be explicit about the boundary. An AST scan sees attribute accesses,
calls, and definitions -- what a violation would actually look like -- and is
blind to the docstrings that describe them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from civsim_web.store_client.fake import FakeMatchStore
from civsim_web.store_client.port import READ_OPERATIONS, WRITE_OPERATIONS, MatchStore

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "civsim_web"


def _modules() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_the_package_exists_and_is_being_scanned():
    """Guard against this suite silently passing over an empty directory."""
    modules = _modules()
    assert len(modules) >= 10, f"only found {len(modules)} modules under {PACKAGE_ROOT}"


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_no_module_references_a_store_write_operation(module):
    """No write operation is defined, called, or accessed anywhere in the package.

    This is the structural half of Principle III's check for this feature: the
    workflow section's "no new game-state write path bypasses turn-by-turn
    persistence" is satisfied by absence rather than by a guard, and absence is
    only credible if something asserts it.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in WRITE_OPERATIONS:
            offenders.append(f"attribute access .{node.attr} (line {node.lineno})")
        elif isinstance(node, ast.Name) and node.id in WRITE_OPERATIONS:
            offenders.append(f"name {node.id} (line {node.lineno})")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in WRITE_OPERATIONS:
                offenders.append(f"definition of {node.name} (line {node.lineno})")

    assert not offenders, (
        f"{module.relative_to(PACKAGE_ROOT)} reaches for a MatchStore write "
        f"operation: {offenders}. This feature has no write path at all "
        f"(FR-023, FR-024, FR-026)"
    )


def test_the_match_store_protocol_declares_reads_only():
    """The Protocol and the documented read list agree, with no third source."""
    declared = {
        name
        for name, value in vars(MatchStore).items()
        if callable(value) and not name.startswith("_")
    }
    assert declared == set(READ_OPERATIONS)
    assert declared.isdisjoint(WRITE_OPERATIONS)


@pytest.mark.parametrize("operation", WRITE_OPERATIONS)
def test_the_fake_omits_every_write_operation(operation):
    """T006: omitted entirely, not stubbed as a no-op.

    The difference is the whole point. A no-op stub makes a mistaken write
    *succeed silently*; an omission makes it an AttributeError at the call
    site, which is a bug report instead of a corrupted record.
    """
    assert not hasattr(FakeMatchStore, operation)


@pytest.mark.parametrize("operation", READ_OPERATIONS)
def test_the_fake_implements_every_read_operation(operation):
    assert callable(getattr(FakeMatchStore, operation, None))


def test_the_fake_satisfies_the_protocol_structurally():
    """quickstart Scenario 6: swapping stores must be configuration, not code."""
    store: MatchStore = FakeMatchStore()
    assert store.ping().ok is True
    assert store.list_active_runs() == []
    assert store.get_run("no-such-run") is None


def test_only_the_store_client_package_names_the_protocol():
    """`store_client/` is the seam; the rest of the package sees records, not a store.

    `app.py` is allowed one reference: it annotates the configured store on the
    application state, which is how the seam is handed to routes without any of
    them importing the port themselves.
    """
    allowed = {
        PACKAGE_ROOT / "store_client" / "port.py",
        PACKAGE_ROOT / "store_client" / "fake.py",
        PACKAGE_ROOT / "app.py",
        PACKAGE_ROOT / "health" / "derive.py",  # imports record protocols only
    }
    for module in _modules():
        if module in allowed:
            continue
        source = module.read_text(encoding="utf-8")
        assert "store_client.port" not in source, (
            f"{module.relative_to(PACKAGE_ROOT)} imports the MatchStore seam directly; "
            f"store_client/ is the only module permitted to touch the port (T005)"
        )
