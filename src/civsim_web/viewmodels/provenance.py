"""Response provenance -- invariant V10, on the responses that need it.

*"Every top-level response names both the run's game-side catalog version and
this feature's Panel Registry version"* (data-model.md V10, SS13). Two versions,
because a rendered panel has two upstream authorities: 002's catalog decided
what data existed to record, and this feature's registry decided whether to show
it. Tracing a panel after the fact needs both.

data-model.md SS13 names the responses this belongs on: ``RunDetailView``,
``TurnCycleView``, ``ComparisonView``, and catalog listings. It is deliberately
four short strings rather than the full ``PanelRegistryVersion`` (which carries
every ``panel_id`` in force): a turn with three hundred steps should not repeat
a few hundred panel ids per nested view. ``RunDetailView`` -- one object per
response -- carries the full ``PanelRegistryVersion`` as well.
"""

from __future__ import annotations

from typing import Any

from civsim_web.registry.loader import PanelRegistry
from civsim_web.viewmodels.base import ViewModel

__all__ = ["Provenance", "build_provenance"]


class Provenance(ViewModel):
    """Which registry rendered this, and which game-side catalogs produced it."""

    panel_registry_version: str
    panel_registry_content_hash: str
    observation_catalog_version: str | None = None
    action_catalog_version: str | None = None


def build_provenance(registry: PanelRegistry, run: Any | None = None) -> Provenance:
    """Stamp a response with its registry version and the run's catalog versions.

    The catalog versions are read straight off the ``Run`` record and are
    ungated on purpose: they are this feature's own audit trail, not game
    information, and withholding them would make the boundary *less* auditable
    rather than more (plan.md Constitution Check, "boundary is auditable after
    the fact").
    """
    return Provenance(
        panel_registry_version=registry.version,
        panel_registry_content_hash=registry.content_hash,
        observation_catalog_version=_version(run, "observation_catalog_version"),
        action_catalog_version=_version(run, "action_catalog_version"),
    )


def _version(run: Any | None, name: str) -> str | None:
    if run is None:
        return None
    value = getattr(run, name, None)
    if value is None:
        return None
    return str(getattr(value, "value", value))
