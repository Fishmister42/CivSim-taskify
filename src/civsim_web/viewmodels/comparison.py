"""``ComparisonView`` and ``DivergencePoint`` (T052, data-model.md SS11).

Three rules govern this model and all three are enforced by the *type* or by a
validator rather than by the route that builds it, because a route is a place a
future contributor can forget.

**1. No capture, anywhere, ever (FR-035, SC-016, invariant V7).** data-model.md
SS11, verbatim:

    this entire model is constructed with ``CaptureView`` nowhere in its type --
    not merely unused, but structurally absent, so a future addition to this
    view cannot accidentally introduce a capture dependency into a comparison
    question without changing the type

This module therefore does not import ``viewmodels.capture`` at all, and
``tests/contract/test_web_parity_boundary.py`` walks the annotated type graph of
``ComparisonView`` transitively to assert no ``CaptureView`` is reachable from
it. Comparison must stay answerable with every capture in every compared run
withheld.

**2. Quarantine, not omission (FR-021).** data-model.md SS11, verbatim:

    A run in ``quarantined_run_ids`` still appears in ``runs`` (so the user can
    see *why* it's excluded) but is excluded from ``series`` and
    ``divergence_points`` computation

for any run whose ``record_completeness_status != complete``. The quarantine
decision itself is ``RunSummaryView.trend_eligibility`` (``viewmodels/base.py``,
T050), which reads that status verbatim *and* additionally refuses a run the
store lists turn gaps for -- Principle III's "MUST NOT be used for trending ...
if its turn-by-turn record has gaps", applied where the trending actually
happens. The validator below re-checks the outcome: a quarantined run that has
acquired a series, a divergence-point reference, or a leadership claim fails
construction. Removing the filter in ``build_comparison_view`` does not produce
a quietly-averaged trend line; it produces a ``ValidationError``.

**3. The comparison basis is stated, not assumed (Principle IV).** The
constitution requires comparison work to run "against a fixed set of initial
seeds under a consistent civilization and ruleset". Nothing stops a user from
selecting five runs that share none of those, and a chart that draws them on one
axis without comment invites exactly the conclusion the principle forbids. So
``ComparisonBasis`` reports which of seed / civilization / leader / ruleset /
model actually agree across the selected set, which diverge, and which cannot be
checked at all because the published port cannot reach ``RunConfiguration``
(plan.md C1). An unverifiable basis is reported as unverifiable, never as
uniform.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import model_validator

from civsim_web.refs.reference import ViewReference
from civsim_web.viewmodels.base import (
    PanelRegistryVersion,
    RunSummaryView,
    UnavailableField,
    ViewModel,
)
from civsim_web.viewmodels.metrics import MetricAxis, MetricSeriesView, axis_for
from civsim_web.viewmodels.provenance import Provenance

__all__ = [
    "BASIS_DIMENSIONS",
    "ComparisonBasis",
    "ComparisonView",
    "DivergenceKind",
    "DivergencePoint",
    "MetricAxis",
    "NOTHING_SELECTED",
    "SEPARATION_RATIO",
    "build_comparison_basis",
    "build_comparison_view",
    "build_divergence_points",
]

#: Relative spread at which a metric's compared values count as having
#: "separated beyond a threshold" (data-model.md SS11). Expressed as a ratio of
#: the leading value rather than an absolute number because metric units are
#: open-ended -- spec Assumptions say the comparison view handles "any per-turn
#: metric the store records", and an absolute threshold would mean something
#: different for science output than for, say, a city count.
SEPARATION_RATIO = 0.25

#: The dimensions Principle IV names as the basis of a meaningful comparison,
#: plus the model, which FR-018 lists as a catalog column and which is the one
#: thing most likely to differ between two otherwise-identical runs.
BASIS_DIMENSIONS: tuple[str, ...] = ("seed", "civilization", "leader", "ruleset", "model_primary")

NOTHING_SELECTED = (
    "No runs were selected for comparison. Choose runs from the catalog; the "
    "comparison view never picks a set on your behalf, because which runs are "
    "comparable is a question about seeds and rulesets, not a default."
)


class DivergenceKind(StrEnum):
    """Why a turn is a divergence point (data-model.md SS11).

    The two are kept distinct rather than merged into "interesting turn": a
    change of leader and a widening gap are different observations, and a user
    navigating to one wants to know which they are looking at.
    """

    LEADER_CHANGE = "leader_change"
    SEPARATION = "separation"


class DivergencePoint(ViewModel):
    """A turn where the compared runs separated (data-model.md SS11)."""

    turn: int
    metric_name: str
    kind: DivergenceKind
    leader_run_id: str
    leader_value: float
    trailing_run_id: str | None = None
    trailing_value: float | None = None
    spread: float = 0.0
    previous_leader_run_id: str | None = None
    refs: dict[str, str] = {}
    """One ready-to-navigate reference per *compared, non-quarantined* run at
    this turn, so a client can jump to "this turn, in each compared run" with no
    extra round trip (FR-022). The values are canonical view-reference paths --
    the reference *is* the URL (data-model.md SS12)."""


class ComparisonBasis(ViewModel):
    """Whether the selected runs are actually comparable (Principle IV).

    ``is_uniform`` is true only when every checkable dimension holds exactly one
    distinct value across the selection **and** no dimension is unverifiable.
    A selection whose seeds cannot be read is not "uniform by default".
    """

    values: dict[str, list[str]] = {}
    """Dimension -> the distinct values present across the selection, sorted."""
    uniform_dimensions: tuple[str, ...] = ()
    divergent_dimensions: tuple[str, ...] = ()
    unverifiable_dimensions: tuple[str, ...] = ()
    is_uniform: bool = False
    note: str = ""


class ComparisonView(ViewModel):
    """Several runs' metric series on common axes (data-model.md SS11).

    No field on this model, or on anything reachable from it, is a
    ``CaptureView`` -- see the module docstring. FR-035 and SC-016 require every
    comparison and trend question to be answerable with every capture withheld,
    and the structural way to guarantee that is for captures to have no type
    path into this response at all.
    """

    runs: tuple[RunSummaryView, ...] = ()
    series: dict[str, list[MetricSeriesView]] = {}
    quarantined_run_ids: tuple[str, ...] = ()
    divergence_points: tuple[DivergencePoint, ...] = ()
    axes: dict[str, MetricAxis] = {}
    basis: ComparisonBasis
    metrics: tuple[str, ...] = ()
    requested_run_ids: tuple[str, ...] = ()
    empty_state_reason: str | None = None
    unavailable: tuple[UnavailableField, ...] = ()
    provenance: Provenance
    panel_registry: PanelRegistryVersion

    @model_validator(mode="after")
    def _quarantined_runs_are_excluded_from_every_computation(self) -> ComparisonView:
        """FR-021's second half, checked rather than trusted.

        A quarantined run *must* still be in ``runs`` -- hiding it would lose the
        explanation of why it was excluded -- and *must not* appear anywhere a
        value of its was used. Those are opposite requirements about the same id,
        which is exactly the pair a future edit is likely to get half right.
        """
        quarantined = set(self.quarantined_run_ids)
        if not quarantined:
            return self

        listed = {summary.run_id for summary in self.runs}
        missing = sorted(quarantined - listed)
        if missing:
            raise ValueError(
                f"quarantined runs {missing} are absent from `runs`. FR-016 requires "
                f"marking, not hiding: a run excluded from the trend must still be "
                f"visible with the reason it was excluded"
            )

        offenders: list[str] = []
        for metric_name, series_list in self.series.items():
            for series in series_list:
                if series.run_id in quarantined:
                    offenders.append(f"series[{metric_name}] carries run {series.run_id}")
        for point in self.divergence_points:
            if point.leader_run_id in quarantined:
                offenders.append(
                    f"divergence point turn {point.turn} names quarantined run "
                    f"{point.leader_run_id} as leader"
                )
            for run_id in point.refs:
                if run_id in quarantined:
                    offenders.append(
                        f"divergence point turn {point.turn} carries a ref for "
                        f"quarantined run {run_id}"
                    )
        for metric_name, axis in self.axes.items():
            for run_id in axis.run_ids:
                if run_id in quarantined:
                    offenders.append(f"axis[{metric_name}] was scaled to run {run_id}")

        if offenders:
            raise ValueError(
                "a run whose turn-by-turn record has gaps was used as trending input "
                "(Principle III, FR-021): " + "; ".join(sorted(offenders))
            )
        return self


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def build_comparison_basis(runs: Sequence[RunSummaryView]) -> ComparisonBasis:
    """Report whether the selected runs share a comparison basis (Principle IV).

    "Optimization, branching, backtracking, and ablation work MUST run against a
    fixed set of initial seeds under a consistent civilization and ruleset."
    This does not *refuse* a mixed selection -- the user may well be asking a
    deliberate question about two rulesets -- it refuses to let a mixed
    selection look like a clean one.
    """
    if not runs:
        return ComparisonBasis(note=NOTHING_SELECTED)

    values: dict[str, list[str]] = {}
    uniform: list[str] = []
    divergent: list[str] = []
    unverifiable: list[str] = []

    for dimension in BASIS_DIMENSIONS:
        found = [getattr(summary, dimension, None) for summary in runs]
        present = sorted({str(value) for value in found if value is not None})
        values[dimension] = present
        if any(value is None for value in found):
            unverifiable.append(dimension)
        elif len(present) > 1:
            divergent.append(dimension)
        else:
            uniform.append(dimension)

    is_uniform = not divergent and not unverifiable
    return ComparisonBasis(
        values=values,
        uniform_dimensions=tuple(uniform),
        divergent_dimensions=tuple(divergent),
        unverifiable_dimensions=tuple(unverifiable),
        is_uniform=is_uniform,
        note=_basis_note(is_uniform, divergent, unverifiable),
    )


def _basis_note(
    is_uniform: bool, divergent: Sequence[str], unverifiable: Sequence[str]
) -> str:
    if is_uniform:
        return (
            "These runs share a seed, civilization, leader, ruleset, and model, so "
            "differences between them are attributable to what the agent did "
            "(Principle IV)."
        )
    parts: list[str] = []
    if divergent:
        parts.append(
            "These runs differ in " + ", ".join(divergent) + ". Differences between "
            "their trajectories are not attributable to agent behaviour alone: a "
            "comparison is only meaningful across a fixed seed set under a "
            "consistent civilization and ruleset (Principle IV)."
        )
    if unverifiable:
        parts.append(
            "The comparison basis could not be verified for "
            + ", ".join(unverifiable)
            + ": the published MatchStore port exposes no read that reaches a run's "
            "configuration (plan.md Complexity Tracking C1). Treat these runs as "
            "possibly incomparable rather than as verified comparable."
        )
    return " ".join(parts)


def build_divergence_points(
    metric_name: str,
    series_by_run: Mapping[str, MetricSeriesView],
) -> tuple[DivergencePoint, ...]:
    """Turns where the leading run changes, or where values separate (SS11).

    Only turns for which **every** compared run has a real recorded value are
    considered. Comparing a turn where one run simply has no number would make
    the absence look like a low value, and "the leader changed" would then be an
    artefact of the hole rather than of the game -- the same mistake in leader
    form that interpolating a gapped point would be in chart form.
    """
    if len(series_by_run) < 2:
        return ()

    by_run = {run_id: series.values_by_turn for run_id, series in series_by_run.items()}
    shared = sorted(set.intersection(*(set(values) for values in by_run.values())))
    if not shared:
        return ()

    points: list[DivergencePoint] = []
    previous_leader: str | None = None
    separation_reported = False

    for turn in shared:
        ranked = sorted(
            ((run_id, values[turn]) for run_id, values in by_run.items()),
            key=lambda pair: (-pair[1], pair[0]),
        )
        leader_run_id, leader_value = ranked[0]
        trailing_run_id, trailing_value = ranked[-1]
        spread = leader_value - trailing_value
        refs = {
            run_id: ViewReference(run_id=run_id, turn_number=turn).path for run_id in sorted(by_run)
        }

        if previous_leader is not None and leader_run_id != previous_leader:
            points.append(
                DivergencePoint(
                    turn=turn,
                    metric_name=metric_name,
                    kind=DivergenceKind.LEADER_CHANGE,
                    leader_run_id=leader_run_id,
                    leader_value=leader_value,
                    trailing_run_id=trailing_run_id,
                    trailing_value=trailing_value,
                    spread=spread,
                    previous_leader_run_id=previous_leader,
                    refs=refs,
                )
            )
        previous_leader = leader_run_id

        if separation_reported or leader_value <= 0:
            continue
        if spread / abs(leader_value) >= SEPARATION_RATIO:
            separation_reported = True
            points.append(
                DivergencePoint(
                    turn=turn,
                    metric_name=metric_name,
                    kind=DivergenceKind.SEPARATION,
                    leader_run_id=leader_run_id,
                    leader_value=leader_value,
                    trailing_run_id=trailing_run_id,
                    trailing_value=trailing_value,
                    spread=spread,
                    refs=refs,
                )
            )

    return tuple(sorted(points, key=lambda point: (point.metric_name, point.turn, point.kind)))


def build_comparison_view(
    *,
    runs: Sequence[RunSummaryView],
    series_by_run: Mapping[str, Mapping[str, MetricSeriesView]],
    metrics: Sequence[str],
    requested_run_ids: Sequence[str],
    provenance: Provenance,
    panel_registry: PanelRegistryVersion,
    unavailable: Sequence[UnavailableField] = (),
    empty_state_reason: str | None = None,
) -> ComparisonView:
    """Assemble the comparison, quarantining every run that may not be trended.

    ``series_by_run`` is ``{run_id: {metric_name: MetricSeriesView}}`` for every
    selected run, quarantined ones included -- the caller does not have to know
    the quarantine rule, and cannot get it wrong by filtering earlier. The filter
    happens here, once, and the model validator re-checks its outcome.
    """
    quarantined = tuple(
        summary.run_id for summary in runs if not summary.trend_eligibility.eligible
    )
    quarantined_set = set(quarantined)
    eligible = [summary.run_id for summary in runs if summary.run_id not in quarantined_set]

    series: dict[str, list[MetricSeriesView]] = {}
    axes: dict[str, MetricAxis] = {}
    divergence: list[DivergencePoint] = []

    for metric_name in metrics:
        collected = [
            series_by_run[run_id][metric_name]
            for run_id in eligible
            if run_id in series_by_run and metric_name in series_by_run[run_id]
        ]
        series[metric_name] = collected
        axes[metric_name] = axis_for(metric_name, collected)
        divergence.extend(
            build_divergence_points(
                metric_name, {found.run_id: found for found in collected}
            )
        )

    return ComparisonView(
        runs=tuple(runs),
        series=series,
        quarantined_run_ids=quarantined,
        divergence_points=tuple(divergence),
        axes=axes,
        basis=build_comparison_basis(runs),
        metrics=tuple(metrics),
        requested_run_ids=tuple(requested_run_ids),
        empty_state_reason=empty_state_reason,
        unavailable=tuple(unavailable),
        provenance=provenance,
        panel_registry=panel_registry,
    )
