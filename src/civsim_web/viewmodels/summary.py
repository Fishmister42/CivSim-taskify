"""Populating ``RunSummaryView`` (data-model.md SS1).

The model itself is foundational (``viewmodels/base.py``, T009); this is the
constructor US1 needs and US4's catalog (T049/T050) can reuse rather than
reimplement.

**The C1 gap, handled honestly.** FR-018's seed, civilization, leader, ruleset
and model columns all live on 002's ``RunConfiguration``, and the published
``match-store-port.md`` has *no* read that resolves one from ``Run.config_id``
(plan.md Complexity Tracking C1; restated in ``store_client/port.py``). A store
may additionally offer the optional ``RunConfigurationReader`` capability, in
which case those columns are populated; a store that does not gets them named in
``unavailable`` with the reason. Never blank, never a plausible-looking default:
an empty civilization column that might mean "no civilization" is worse than one
that says why it is missing (UP-005, FR-025's honest-state rule applied to a
port gap rather than a schema change).
"""

from __future__ import annotations

from typing import Any

from civsim_web.health.derive import derive_health
from civsim_web.registry.loader import PanelRegistry
from civsim_web.viewmodels.base import RunSummaryView, UnavailableField
from civsim_web.viewmodels.gate import GatedReader, Reason

__all__ = ["build_run_summary"]

#: The five FR-018 columns that live behind ``Run.config_id``.
_CONFIGURATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("seed", "map_seed"),
    ("civilization", "civilization"),
    ("leader", "leader"),
    ("ruleset", "ruleset"),
)


def build_run_summary(
    run: Any,
    *,
    registry: PanelRegistry,
    events: Any = (),
    configuration: Any | None = None,
    turn_count: int = 0,
    outcome_metrics: dict[str, float] | None = None,
) -> RunSummaryView:
    """Project 002's ``Run`` (+ its ``RunConfiguration``, where reachable).

    ``health`` is derived by ``health/derive.py`` and never by a timer of this
    feature's own (invariant V3). ``record_completeness_status`` and
    ``comparability_status`` are read verbatim and never re-derived (invariant
    V5) -- a second, independently computed completeness judgment could disagree
    with 002's and reproduce the exact asymmetry Principle VI forbids.
    """
    gate = GatedReader(registry, "Run", run)
    missing: list[UnavailableField] = []

    values: dict[str, str | None] = {}
    if configuration is None:
        for column, _ in _CONFIGURATION_COLUMNS:
            values[column] = None
            missing.append(
                UnavailableField(
                    field=f"RunConfiguration.{column}",
                    reason=Reason.PORT_CANNOT_REACH,
                )
            )
        values["model_primary"] = None
        missing.append(
            UnavailableField(
                field="RunConfiguration.model_config.primary", reason=Reason.PORT_CANNOT_REACH
            )
        )
    else:
        config_gate = GatedReader(registry, "RunConfiguration", configuration)
        for column, field_name in _CONFIGURATION_COLUMNS:
            values[column] = config_gate.text(field_name)
        values["model_primary"] = _model_primary(config_gate)
        missing.extend(config_gate.missing)

    return RunSummaryView(
        run_id=str(gate.get("run_id", default="") or ""),
        lifecycle_state=gate.text("lifecycle_state", default="") or "",
        health=derive_health(run, tuple(events or ())),
        record_completeness_status=gate.text("record_completeness_status", default="") or "",
        comparability_status=gate.text("comparability_status", default="") or "",
        seed=values.get("seed"),
        civilization=values.get("civilization"),
        leader=values.get("leader"),
        ruleset=values.get("ruleset"),
        model_primary=values.get("model_primary"),
        turn_count=turn_count,
        outcome_metrics=dict(outcome_metrics or {}),
        started_at=gate.get("started_at"),
        ended_at=gate.get("ended_at"),
        parent_run_id=gate.text("parent_run_id"),
        parent_turn=gate.get("parent_turn"),
        observation_catalog_version=gate.text("observation_catalog_version"),
        action_catalog_version=gate.text("action_catalog_version"),
        unavailable=tuple(gate.missing) + tuple(missing),
    )


def _model_primary(config_gate: GatedReader) -> str | None:
    """``RunConfiguration.model_config.primary`` -- identity only, never a key.

    ``ModelConfig`` carries no credential material by 002's own contract
    (FR-043) and the port's record shape narrows it to ``primary``/``fallbacks``
    as this feature's half of that guarantee (``store_client/port.py``). Only
    ``primary`` is read here, and only as a display string.
    """
    model_config = config_gate.get("model_config")
    if model_config is None:
        return None
    primary = getattr(model_config, "primary", None)
    return None if primary is None else str(getattr(primary, "value", primary))
