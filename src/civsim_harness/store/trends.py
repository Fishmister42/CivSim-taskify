"""Pure derivations behind the trend and divergence reads (003 US3; contract T1-T4).

No SQLite here: every function takes records the adapter already loaded and returns value types
from ``store/contract.py``, so the rules are testable without a file and cannot drift between the
series read and the divergence read.

**The store never invents a metric** (research R4). A turn's metrics are the numeric, non-boolean
keys of its recorded ``TurnCycle.yields`` -- the same set the web interface's metrics page renders
(``civsim_web.viewmodels.metrics.numeric_yields``) -- plus two the store derives from the
attempt's final observation, ``city_count`` and ``unit_count`` (the lengths of ``cities.state``'s
``cities`` and ``units.state``'s ``units``). A metric absent from a turn is absent from the series;
an absent count is ``None``, never zero.

**The exclusion rule is the store's** (FR-019). :func:`exclusion_for` is the one definition both
reads apply.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from civsim_harness.models.run import ComparabilityStatus, RecordCompletenessStatus, Run
from civsim_harness.models.turn import Observation
from civsim_harness.store.contract import (
    DivergenceDetail,
    ExcludedRun,
    ExclusionReason,
    TurnFingerprint,
)
from civsim_harness.store.port import TurnCycleRecord

CITY_COUNT = "city_count"
UNIT_COUNT = "unit_count"
DERIVED_METRICS: tuple[str, ...] = (CITY_COUNT, UNIT_COUNT)

_CITIES_DECLARATION = "cities.state"
_UNITS_DECLARATION = "units.state"


def _count_from_observation(
    observation: Observation | None, declaration_id: str, key: str
) -> int | None:
    """The length of ``<declaration>.value[key]`` in *observation* (the attempt's **last** step)."""
    if observation is None:
        return None
    for entry in observation.entries:
        if entry.declaration_id != declaration_id:
            continue
        value = entry.value
        if isinstance(value, dict) and isinstance(value.get(key), list):
            return len(value[key])
        return None
    return None


def city_count_from(observation: Observation | None) -> int | None:
    return _count_from_observation(observation, _CITIES_DECLARATION, "cities")


def unit_count_from(observation: Observation | None) -> int | None:
    return _count_from_observation(observation, _UNITS_DECLARATION, "units")


def city_count(record: TurnCycleRecord) -> int | None:
    return city_count_from(record.steps[-1].observation)


def unit_count(record: TurnCycleRecord) -> int | None:
    return unit_count_from(record.steps[-1].observation)


def turn_metrics_from(
    yields: Mapping[str, Any], last_observation: Observation | None
) -> dict[str, float]:
    """Every metric a turn's ``yields`` and final observation carry (T2). Booleans are excluded:
    ``True`` is an ``int`` in Python and a flag plotted as ``1.0`` would be a fabricated
    measurement. This is the light form the series read uses -- the adapter loads a turn's
    ``yields`` and its last step only, never every step, to build a series."""
    metrics: dict[str, float] = {}
    for name, value in yields.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            metrics[str(name)] = float(value)
    cities = city_count_from(last_observation)
    if cities is not None:
        metrics[CITY_COUNT] = float(cities)
    units = unit_count_from(last_observation)
    if units is not None:
        metrics[UNIT_COUNT] = float(units)
    return metrics


def turn_metrics(record: TurnCycleRecord) -> dict[str, float]:
    """:func:`turn_metrics_from` over a full turn record."""
    return turn_metrics_from(record.turn_cycle.yields, record.steps[-1].observation)


def canonical_parameters(parameters: dict[str, Any]) -> str:
    """Parameters rendered so that key order cannot make two equal decisions look different."""
    return json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)


def turn_fingerprint(record: TurnCycleRecord) -> TurnFingerprint:
    """What this turn did and recorded (research R5)."""
    return TurnFingerprint(
        turn=record.turn_cycle.turn_number,
        actions=tuple(
            (
                str(bundle.decision.action_declaration_id),
                canonical_parameters(bundle.decision.parameters),
            )
            for bundle in record.steps
        ),
        metrics=turn_metrics(record),
        city_count=city_count(record),
        unit_count=unit_count(record),
    )


def _render(value: Any) -> str:
    if value is None:
        return "absent"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def fingerprint_differences(a: TurnFingerprint, b: TurnFingerprint) -> tuple[DivergenceDetail, ...]:
    """Every dimension on which two same-turn fingerprints differ, in a stable order."""
    differences: list[DivergenceDetail] = []
    if a.actions != b.actions:
        differences.append(
            DivergenceDetail(
                dimension="actions",
                value_a="; ".join(f"{name}{params}" for name, params in a.actions) or "none",
                value_b="; ".join(f"{name}{params}" for name, params in b.actions) or "none",
            )
        )
    if a.city_count != b.city_count:
        differences.append(
            DivergenceDetail(CITY_COUNT, _render(a.city_count), _render(b.city_count))
        )
    if a.unit_count != b.unit_count:
        differences.append(
            DivergenceDetail(UNIT_COUNT, _render(a.unit_count), _render(b.unit_count))
        )
    for name in sorted(set(a.metrics) | set(b.metrics)):
        if name in DERIVED_METRICS:
            continue
        left, right = a.metrics.get(name), b.metrics.get(name)
        if left != right:
            differences.append(DivergenceDetail(name, _render(left), _render(right)))
    return tuple(differences)


def first_divergence(
    a: Sequence[TurnFingerprint], b: Sequence[TurnFingerprint]
) -> tuple[int | None, tuple[DivergenceDetail, ...]]:
    """The first turn present in **both** sequences whose fingerprints differ (T4).

    Turns only one side holds are not compared -- absence is not a difference in play, it is a
    shorter record, and the report states the compared range separately.
    """
    by_turn_b = {fingerprint.turn: fingerprint for fingerprint in b}
    for fingerprint in sorted(a, key=lambda item: item.turn):
        other = by_turn_b.get(fingerprint.turn)
        if other is None:
            continue
        differences = fingerprint_differences(fingerprint, other)
        if differences:
            return fingerprint.turn, differences
    return None, ()


def exclusion_for(
    run: Run,
    completeness: RecordCompletenessStatus,
    gaps: Sequence[int],
    *,
    include_visually_degraded: bool,
) -> ExcludedRun | None:
    """The store's exclusion rule (T1, FR-019): ``None`` when the run may contribute."""
    if completeness is RecordCompletenessStatus.HAS_GAPS:
        return ExcludedRun(
            run_id=run.run_id,
            reason=ExclusionReason.HAS_GAPS,
            detail=f"record has gaps at turns {list(gaps)}" if gaps else "record has step gaps",
            gaps=tuple(gaps),
        )
    if completeness is RecordCompletenessStatus.UNKNOWN:
        return ExcludedRun(
            run_id=run.run_id,
            reason=ExclusionReason.COMPLETENESS_UNKNOWN,
            detail="no turn has been attempted, so completeness cannot be judged",
        )
    if run.comparability_status is ComparabilityStatus.NOT_COMPARABLE:
        return ExcludedRun(
            run_id=run.run_id,
            reason=ExclusionReason.NOT_COMPARABLE,
            detail="comparability_status is not_comparable",
        )
    if (
        run.comparability_status is ComparabilityStatus.VISUALLY_DEGRADED
        and not include_visually_degraded
    ):
        return ExcludedRun(
            run_id=run.run_id,
            reason=ExclusionReason.VISUALLY_DEGRADED,
            detail=(
                "comparability_status is visually_degraded; pass include_visually_degraded=True "
                "to admit it, which the response will record"
            ),
        )
    return None


__all__ = [
    "CITY_COUNT",
    "DERIVED_METRICS",
    "UNIT_COUNT",
    "canonical_parameters",
    "city_count",
    "city_count_from",
    "exclusion_for",
    "fingerprint_differences",
    "first_divergence",
    "turn_fingerprint",
    "turn_metrics",
    "turn_metrics_from",
    "unit_count",
    "unit_count_from",
]
