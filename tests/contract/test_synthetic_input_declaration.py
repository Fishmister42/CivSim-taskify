"""Contract test: a synthetic-input path cannot ship declared ``path: firetuner``.

**The hole this closes.** ``IntegrationCapability``'s load-time validator
(``src/civsim_harness/models/catalog.py``) enforces FR-028/SC-020 by requiring a
non-empty ``firetuner_gap`` when ``path == bespoke``. That check only ever fires
on entries that already say ``bespoke``, so it is vacuous against the one failure
that actually matters: a capability whose implementation drives real synthetic
input while declaring itself ``firetuner``. Nothing in the catalog can see Python,
so nothing in the catalog could notice.

That is not hypothetical. ``prompts.orders`` shipped as ``path: firetuner`` with
``firetuner_gap: null`` while ``capability/executor.py`` issued a real
``mouse_move`` + ``mouse_click`` through ``HostPlatform.send_input`` for every
prompt answer that had no direct Lua call -- a documented, *measured* Firetuner
gap (2026-09-21: no Lua API reachable from ``InGame`` fires a control's registered
callback) recorded only in a Python comment. Constitution Principle II permits the
bespoke path; what it forbids is taking it without declaring it.

**How this test works.** It does not try to prove reachability by analysis. It
pins, as data, every in-harness call site of ``HostPlatform.send_input`` together
with the capability each one serves, and then:

1. re-derives the call sites from the source with :mod:`ast` and fails if the set
   has changed -- a new synthetic-input path anywhere in ``src/civsim_harness``
   must be added to :data:`SYNTHETIC_INPUT_CALL_SITES` before it can ship, which
   is the moment its declaration gets decided;
2. asserts every capability named there is declared ``path: bespoke`` with a
   non-empty ``firetuner_gap`` in the real ``catalogs/`` tree;
3. requires any call site that serves *no* catalog capability to say, in the data,
   why it is outside the catalog -- so an undeclared path is at least named rather
   than invisible.

``src/civsim_harness/host/`` is excluded from the scan: the port declares
``send_input`` and the three adapters implement it. Those are the mechanism, not a
use of it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from civsim_harness.capability.loader import load_catalog
from civsim_harness.models.catalog import CapabilityId, CapabilityPath

_REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGS_ROOT = _REPO_ROOT / "catalogs"
SRC_ROOT = _REPO_ROOT / "src" / "civsim_harness"

# The host package *is* the synthetic-input mechanism -- `port.py` declares the
# method and `linux/`, `macos/`, `windows/` implement it. Scanning it would pin
# the definition rather than any use of it.
EXCLUDED_FROM_SCAN = ("host",)


@dataclass(frozen=True)
class SyntheticInputCallSite:
    """One place in the harness that drives real synthetic input, and what it serves."""

    module: str
    """Path relative to ``src/civsim_harness``."""

    capability_ids: tuple[str, ...]
    """Catalog capabilities this call site implements. Each MUST be `path: bespoke`."""

    what_it_does: str
    """Principle II's first obligation: what the bespoke path does."""

    outside_catalog_reason: str | None = None
    """Required when `capability_ids` is empty: why no catalog capability covers this."""


SYNTHETIC_INPUT_CALL_SITES: tuple[SyntheticInputCallSite, ...] = (
    SyntheticInputCallSite(
        module="capability/executor.py",
        capability_ids=("prompts.orders",),
        what_it_does=(
            "Completes a Lua result of the shape {ok=false, reason='requires_host_click', "
            "click={x,y,w,h}} with one mouse_move + one left mouse_click at the named control's "
            "centre, scaled from the UI's own screen space onto the game window. Measured "
            "Firetuner gap, 2026-09-21: no Lua API reachable from InGame fires a control's "
            "registered callback."
        ),
    ),
    SyntheticInputCallSite(
        module="saves/load_game.py",
        capability_ids=(),
        what_it_does=(
            "Presses Escape once, aimed at the focused game window, to dismiss the leader-intro "
            "screen that a load can stop on. Runs only while the tuner port is closed by the load "
            "itself, and stops the moment the port answers."
        ),
        outside_catalog_reason=(
            "Save loading is operator/branch lifecycle (FR-033, FR-045/FR-046), not an agent-"
            "facing action: there is no load declaration in catalogs/actions/ and no capability "
            "for it, so Principle II's capability-path rule has nothing to bind here. The gap is "
            "nonetheless measured and recorded in the module docstring -- the intro screen cannot "
            "be observed OR dismissed through the tuner, because the tuner is what the load is "
            "holding closed. Flagged for a ruling on whether lifecycle paths outside the catalog "
            "should carry a declaration of their own; this test pins it so it cannot go quiet."
        ),
    ),
)


def _scanned_call_sites() -> set[str]:
    """Every module under `src/civsim_harness` (bar `host/`) that calls `.send_input(`."""
    found: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT)
        if relative.parts[0] in EXCLUDED_FROM_SCAN:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_input"
            ):
                found.add(relative.as_posix())
                break
    return found


def test_every_synthetic_input_call_site_is_pinned_as_data() -> None:
    """A new `send_input` caller must be declared here before it can ship.

    This is the tripwire: the assertion below is what forces the author of a new
    synthetic-input path to say which capability it serves, which in turn is what
    the next test checks against the catalog.
    """
    assert _scanned_call_sites() == {site.module for site in SYNTHETIC_INPUT_CALL_SITES}


def test_every_capability_that_drives_synthetic_input_is_declared_bespoke() -> None:
    """Constitution Principle II, made checkable: declare the bespoke path as bespoke.

    The catalog's own validator cannot reach this conclusion -- it sees YAML, and
    the synthetic input is in Python. This test is the join.
    """
    catalog = load_catalog(CATALOGS_ROOT)

    for site in SYNTHETIC_INPUT_CALL_SITES:
        for capability_id in site.capability_ids:
            capability = catalog.capabilities.get(CapabilityId(capability_id))
            assert capability is not None, (
                f"{site.module} names capability {capability_id!r}, which is not in the catalog"
            )
            assert capability.path is CapabilityPath.BESPOKE, (
                f"{capability_id} reaches HostPlatform.send_input in {site.module} but declares "
                f"path: {capability.path.value}. Principle II permits the bespoke path only when "
                f"it is declared and its Firetuner gap recorded; declaring it firetuner makes the "
                f"load-time firetuner_gap rule pass over it vacuously."
            )
            assert (capability.firetuner_gap or "").strip(), (
                f"{capability_id} is bespoke but carries no firetuner_gap (FR-028, SC-020)"
            )


def test_a_synthetic_input_path_outside_the_catalog_says_why() -> None:
    """A call site serving no capability must name its reason, not simply have none."""
    for site in SYNTHETIC_INPUT_CALL_SITES:
        if site.capability_ids:
            continue
        assert (site.outside_catalog_reason or "").strip(), (
            f"{site.module} drives synthetic input and serves no catalog capability, so it must "
            f"record why it sits outside the catalog"
        )


def test_every_pinned_call_site_records_what_it_does() -> None:
    """Principle II's other half: each bespoke path records *what it does*, not only the gap."""
    for site in SYNTHETIC_INPUT_CALL_SITES:
        assert (site.what_it_does or "").strip(), f"{site.module} records no behaviour statement"
