"""Integration test: the real ``catalogs/`` tree loads.

``tests/unit/test_catalog_load.py`` (T037) deliberately never touches the
real ``catalogs/`` tree -- it was authored concurrently by a different task,
so that suite works entirely off small, self-contained fixtures instead
(see its module docstring). That left a gap: nothing asserted that the
*actual*, checked-in catalog -- the one every other wave loads at startup --
still validates against the ``ParityDeclaration`` / ``IntegrationCapability``
models as those models evolve.

That gap is exactly how the ``parity_basis`` defect escaped: data-model.md
SS11 always documented ``IntegrationCapability.parity_basis`` as "inherited
or restated" (i.e. optional), but the model was built requiring it while
``catalogs/capabilities.yaml`` was authored, correctly, against the
contract's schema block, which never listed the field at all. Fixture-only
coverage could not catch the mismatch because the fixtures were written
alongside the (incorrect) model, not against the real catalog data.

This module is the permanent guard against that class of drift: it loads
``catalogs/`` for real, on every test run, and pins the headline counts so a
future change that silently drops declarations or capabilities is caught
here too.
"""

from __future__ import annotations

from pathlib import Path

from civsim_harness.capability.loader import Catalog, load_catalog

_REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGS_ROOT = _REPO_ROOT / "catalogs"

# Bumped from 51/23 by T216 (game.outcome_state + the game.outcome capability, so FR-005's
# `game_outcome` stop condition can resolve at all) and T221 (camera.read_state, so a capture's
# camera state stops being empty and a view's declared camera_requirements can be satisfied).
EXPECTED_DECLARATION_COUNT = 59
EXPECTED_CAPABILITY_COUNT = 27


def test_real_catalog_tree_loads() -> None:
    """``load_catalog`` must succeed against the checked-in ``catalogs/`` tree.

    A failure here means the real catalog and the models/contracts that
    validate it have drifted apart -- the exact failure mode this test
    exists to catch (see module docstring).
    """
    catalog = load_catalog(CATALOGS_ROOT)

    assert isinstance(catalog, Catalog)
    assert catalog.root == CATALOGS_ROOT


def test_real_catalog_tree_declaration_and_capability_counts() -> None:
    """Headline counts, pinned so a silent drop is caught as a test failure."""
    catalog = load_catalog(CATALOGS_ROOT)

    assert len(catalog.declarations) == EXPECTED_DECLARATION_COUNT
    assert len(catalog.capabilities) == EXPECTED_CAPABILITY_COUNT


def test_real_catalog_every_capability_id_resolves() -> None:
    """Every declaration's ``capability_id`` resolves within the real catalog.

    ``load_catalog`` already enforces this (contract validation 2) and would
    have raised above if it did not hold; this re-asserts it explicitly
    against the loaded catalog so the guarantee is visible at this call site
    too, not just inferred from "loading did not raise".
    """
    catalog = load_catalog(CATALOGS_ROOT)

    for declaration in catalog.declarations.values():
        assert declaration.capability_id in catalog.capabilities


def test_real_catalog_version_is_recorded() -> None:
    """The loaded catalog's version carries every declaration id (FR-022)."""
    catalog = load_catalog(CATALOGS_ROOT)

    assert catalog.version.version
    assert catalog.version.content_hash
    assert set(catalog.version.declaration_ids) == set(catalog.declarations.keys())
