"""Contract test for catalog preflight (T123; FR-022, FR-023, SC-006, SC-007).

Two things ``run.preparation.catalog_preflight`` (T135/T136) must hold together, both asserted
against the real, checked-in ``catalogs/`` tree wherever possible, mirroring
``tests/contract/test_catalog_integration.py``'s own "load the real tree, on every test run"
discipline rather than fixture-only coverage:

1. Preflight aborts -- naming the offending ``capability_id`` -- when any capability the run could
   use has no governing parity declaration (T136).
2. The run record carries the catalog version and content hash in force, for both
   ``observation_catalog_version`` and ``action_catalog_version`` (T135), and that pair is
   accepted by ``models.run.Run`` itself, not just shaped like it would be.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from civsim_harness.capability.loader import Catalog, load_catalog
from civsim_harness.errors import PreflightError
from civsim_harness.models.catalog import (
    CapabilityPath,
    CatalogVersion,
    DeclarationKind,
    IntegrationCapability,
    ParityDeclaration,
)
from civsim_harness.models.common import CatalogVersionRef, LuaContext
from civsim_harness.models.run import HostSupportTier, LifecycleState, Run
from civsim_harness.run.preparation import (
    CatalogPreflightResult,
    _capabilities_without_declarations,
    catalog_preflight,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOGS_ROOT = _REPO_ROOT / "catalogs"


# --------------------------------------------------------------------------
# Fixture builders -- minimal, otherwise-valid declarations/capabilities.
# --------------------------------------------------------------------------


def _capability(capability_id: str) -> IntegrationCapability:
    return IntegrationCapability(
        capability_id=capability_id,  # type: ignore[arg-type]
        path=CapabilityPath.FIRETUNER,
        implementation_ref="lua/gamecore/demo.lua",
        reads=["demo state"],
        writes=[],
        firetuner_gap=None,
        parity_basis=None,
    )


def _observation(declaration_id: str, capability_id: str) -> ParityDeclaration:
    return ParityDeclaration(
        declaration_id=declaration_id,  # type: ignore[arg-type]
        kind=DeclarationKind.OBSERVATION,
        summary="Demo state.",
        parity_basis="Look at the demo panel.",
        context=LuaContext.GAME_CORE_TUNER,
        capability_id=capability_id,  # type: ignore[arg-type]
        output_schema={"type": "object"},
        introduced_in_version="2026.09.1",
    )


def _catalog(
    *,
    declarations: dict[str, ParityDeclaration],
    capabilities: dict[str, IntegrationCapability],
    content_hash: str = "deadbeef",
) -> Catalog:
    return Catalog(
        root=Path("."),
        version=CatalogVersion(
            version="test",
            content_hash=content_hash,
            declaration_ids=list(declarations.keys()),  # type: ignore[arg-type]
        ),
        declarations=declarations,  # type: ignore[arg-type]
        capabilities=capabilities,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# T136 -- capability-resolution gate
# --------------------------------------------------------------------------


def test_real_catalog_passes_capability_resolution_preflight() -> None:
    """Every capability the real, checked-in catalog implements is governed by at least one
    declaration -- the production catalog must clear this gate cleanly (SC-006)."""
    catalog = load_catalog(CATALOGS_ROOT)

    result = catalog_preflight(catalog)

    assert isinstance(result, CatalogPreflightResult)
    assert _capabilities_without_declarations(catalog) == []


def test_capability_without_any_declaration_is_named_and_aborts_the_run() -> None:
    """T136: a capability with zero governing declarations fails the run before it starts,
    naming the offending capability_id (FR-023, V5, SC-006) -- the reverse of what
    ``capability.loader.load_catalog`` already checks at load time (declaration -> capability)."""
    governed_capability = _capability("demo.governed")
    ungoverned_capability = _capability("demo.ungoverned")
    declaration = _observation("demo.state", "demo.governed")
    catalog = _catalog(
        declarations={"demo.state": declaration},
        capabilities={
            "demo.governed": governed_capability,
            "demo.ungoverned": ungoverned_capability,
        },
    )

    with pytest.raises(PreflightError) as excinfo:
        catalog_preflight(catalog)

    assert excinfo.value.detail["capability_ids"] == ["demo.ungoverned"]


def test_capability_without_a_declaration_is_never_a_permissive_pass() -> None:
    """The run truly does not start -- no CatalogPreflightResult is returned on this path."""
    catalog = _catalog(
        declarations={},
        capabilities={"demo.orphan": _capability("demo.orphan")},
    )

    with pytest.raises(PreflightError):
        catalog_preflight(catalog)


def test_every_capability_governed_passes_the_gate() -> None:
    catalog = _catalog(
        declarations={"demo.state": _observation("demo.state", "demo.cap")},
        capabilities={"demo.cap": _capability("demo.cap")},
    )

    result = catalog_preflight(catalog)

    assert isinstance(result, CatalogPreflightResult)


def test_all_offending_capabilities_are_named_at_once_not_just_the_first() -> None:
    catalog = _catalog(
        declarations={},
        capabilities={
            "demo.orphan_a": _capability("demo.orphan_a"),
            "demo.orphan_b": _capability("demo.orphan_b"),
        },
    )

    with pytest.raises(PreflightError) as excinfo:
        catalog_preflight(catalog)

    assert excinfo.value.detail["capability_ids"] == ["demo.orphan_a", "demo.orphan_b"]


# --------------------------------------------------------------------------
# T135 -- catalog version + content hash recorded on the run
# --------------------------------------------------------------------------


def test_catalog_preflight_returns_the_catalog_version_and_content_hash_for_both_fields() -> None:
    catalog = load_catalog(CATALOGS_ROOT)

    result = catalog_preflight(catalog)

    expected = CatalogVersionRef(
        version=catalog.version.version, content_hash=catalog.version.content_hash
    )
    assert result.observation_catalog_version == expected
    assert result.action_catalog_version == expected


def test_catalog_preflight_result_is_accepted_by_the_run_record() -> None:
    """T135/SC-007: the returned CatalogVersionRef pair is exactly what `Run.
    observation_catalog_version` / `Run.action_catalog_version` need -- constructing a Run with
    them proves the shape, not just the field names, line up."""
    catalog = load_catalog(CATALOGS_ROOT)
    result = catalog_preflight(catalog)

    run = Run.model_validate(
        {
            "run_id": "run-1",
            "config_id": "cfg-1",
            "lifecycle_state": LifecycleState.PREPARING,
            "record_completeness_status": "unknown",
            "comparability_status": "comparable",
            "observation_catalog_version": result.observation_catalog_version,
            "action_catalog_version": result.action_catalog_version,
            "game_build": "win/1.0.12.9",
            "host_support_tier": HostSupportTier.VALIDATED,
            "capture_path": "windows_graphics_capture",
        }
    )

    assert run.observation_catalog_version.content_hash == catalog.version.content_hash
    assert run.action_catalog_version.version == catalog.version.version


def test_a_catalog_change_produces_a_distinguishable_version_ref() -> None:
    """FR-022/FR-029: two catalogs with different content hash the same content differently, so
    runs before and after a catalog change stay distinguishable."""
    catalog_a = _catalog(
        declarations={"demo.state": _observation("demo.state", "demo.cap")},
        capabilities={"demo.cap": _capability("demo.cap")},
        content_hash="deadbeef",
    )
    catalog_b = _catalog(
        declarations={"demo.state": _observation("demo.state", "demo.cap")},
        capabilities={"demo.cap": _capability("demo.cap")},
        content_hash="somethingelse",
    )

    result_a = catalog_preflight(catalog_a)
    result_b = catalog_preflight(catalog_b)

    assert result_a.observation_catalog_version != result_b.observation_catalog_version
