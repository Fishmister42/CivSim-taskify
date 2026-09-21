"""The catalog-listing projection (T049) -- and the single place C1 is absorbed.

**plan.md Complexity Tracking C1, in one module.** FR-018 wants every recorded
run listed with its seed, civilization, ruleset, model, turn count, outcome
metrics, completeness status and start/end times, filterable and sortable by any
of them. ``specs/002-civ-playing-harness/contracts/match-store-port.md``
publishes **no catalog-listing read**: ``list_active_runs`` is documented for
*active* runs (recovery and run identity), and ``get_run`` resolves one id.
(The configuration half of the gap has since closed: the amended contract
publishes ``get_run_configuration``, keyed by ``run_id``, which is how a row's
seed / civilization / ruleset / model columns are reached.)

So this module composes the projection out of what the port does publish, and it
is deliberately the *only* place that does. Routes and view models above it take
``CatalogRow`` values and never learn how they were assembled, which means the
day deliverable 3 publishes a real indexed listing read, this file changes and
nothing above it does.

What that costs today, stated rather than hidden:

- **The listing is partial unless the store offers the optional
  ``RunCatalogReader`` capability** (``store_client/port.py``). Without it the
  catalog can only see active runs, and ``CatalogListing.listing_is_partial``
  says so on the response -- never a page that looks like the whole history
  while being a slice of it. That failure mode is the one most likely to corrupt
  a trend conclusion, because a filtered-by-accident catalog looks exactly like
  a filtered-on-purpose one.
- **Per-run reads are O(turns).** ``yields`` lives on each ``TurnCycleRecord``,
  so a metric series costs one ``get_turn_cycle`` per turn. That is fine for the
  fake and for SC-008's 50-run/300-turn target with the caching below, and it is
  precisely what a published metric-series read would replace.

Nothing here writes; the module names no write operation.

**Note on typing**: the store arrives as ``Any`` rather than as the
``MatchStore`` Protocol, for the same reason ``reads.py`` gives -- the
import-boundary test treats naming the seam as something only a listed set of
files may do.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from civsim_web.store_client import reads

__all__ = [
    "CATALOG_LISTING_IS_PARTIAL",
    "CatalogListing",
    "CatalogRow",
    "METRIC_TURN_LIMIT",
    "MetricsScope",
    "latest_yields",
    "list_catalog_rows",
    "project_run",
    "yields_by_turn",
]


class MetricsScope(StrEnum):
    """How much of a run's metric history a caller actually needs (T075).

    The port publishes no metric-series read, so a run's per-turn numbers cost
    one ``get_turn_cycle`` *per turn* (plan.md C1). That is unavoidable for a
    chart and was pure waste for a catalog row, which keeps only the final
    turn's values -- ``/runs`` was reading 320 turns per run across 55 runs to
    display 55 numbers, and SC-008 gives every view two seconds.

    Naming the scope rather than passing a bare boolean is deliberate: the
    difference between these two is a factor of the run length, and a call site
    should have to say which one it means.
    """

    SERIES = "series"
    """Every recorded turn -- what a trajectory is drawn from (``/compare``)."""

    OUTCOME = "outcome"
    """The last recorded turn only -- what a catalog row shows (``/runs``)."""

    NONE = "none"
    """No metric reads at all."""

#: Said in the words the page shows (Principle VI -- both readers get the same
#: sentence about the same limitation).
CATALOG_LISTING_IS_PARTIAL = (
    "This store offers no historical run listing, so the catalog shows only runs "
    "that are still active. Runs that have finished, failed, or been archived "
    "are not listed here -- they exist and are readable by id. The published "
    "MatchStore port has no catalog-listing read (plan.md Complexity Tracking C1)."
)

#: Upper bound on the per-run metric walk. A Civ VI game ends long before this;
#: the cap exists so a misbehaving store cannot turn one catalog request into an
#: unbounded read loop, mirroring ``reads.TURN_PROBE_LIMIT``.
METRIC_TURN_LIMIT = 1000


@dataclass(frozen=True)
class CatalogRow:
    """One run's already-read catalog facts.

    Deliberately *records and primitives*, not a view model: the view model is
    built one layer up, through the Panel Registry gate, so this projection
    cannot become a second path from the store to a response that skips the
    registry (UP-001).
    """

    run: Any
    run_id: str
    configuration: Any | None
    turn_count: int
    gapped_turns: tuple[int, ...]
    yields_by_turn: dict[int, dict[str, float]] = field(default_factory=dict)
    events: tuple[Any, ...] = ()

    @property
    def outcome_metrics(self) -> dict[str, float]:
        """Headline metric values at the run's last recorded turn (SS1).

        Sourced from the same per-turn yields the trajectory is drawn from, so a
        catalog column and a chart can never disagree about the same run --
        data-model.md SS1 requires these come from the metric series rather than
        being independently computed.
        """
        if not self.yields_by_turn:
            return {}
        last = max(self.yields_by_turn)
        return dict(self.yields_by_turn[last])


@dataclass(frozen=True)
class CatalogListing:
    """Every row the port could reach, plus an honest account of what it could not."""

    rows: tuple[CatalogRow, ...]
    listing_is_partial: bool
    partial_reason: str | None = None


def _all_runs(store: Any) -> tuple[tuple[Any, ...], bool]:
    """Every run the store can enumerate, and whether that is the whole history.

    Probes the optional ``RunCatalogReader``; falls back to ``list_active_runs``,
    which is the published read and is documented as *active* runs only.
    """
    lister = getattr(store, "list_runs", None)
    if lister is not None:
        return (tuple(lister() or ()), False)
    return (tuple(store.list_active_runs() or ()), True)


def yields_by_turn(
    store: Any,
    run_id: str,
    *,
    registry: Any,
    highest_turn: int,
    gapped_turns: Sequence[int] = (),
) -> dict[int, dict[str, float]]:
    """``{turn_number: {metric_name: value}}`` for one run's recorded turns.

    Gapped turns are not read at all -- not read and then discarded. A turn the
    store lists in ``turn_gaps()`` has no authoritative attempt, so asking for
    one and rendering whatever came back is exactly the "absence as confirmed
    fact" data-model.md V4 forbids.
    """
    from civsim_web.viewmodels.metrics import numeric_yields

    gaps = set(int(turn) for turn in gapped_turns)
    collected: dict[int, dict[str, float]] = {}
    for turn in range(1, min(highest_turn, METRIC_TURN_LIMIT) + 1):
        if turn in gaps:
            continue
        record = reads.turn_record(store, run_id, turn)
        if record is None:
            continue
        cycle = getattr(record, "turn_cycle", record)
        values = numeric_yields(cycle, registry=registry)
        if values:
            collected[turn] = values
    return collected


def latest_yields(
    store: Any,
    run_id: str,
    *,
    registry: Any,
    highest_turn: int,
    gapped_turns: Sequence[int] = (),
) -> dict[int, dict[str, float]]:
    """``{turn: values}`` for the last turn of a run that recorded any (T075).

    **The same value ``yields_by_turn`` would have produced, reached without
    reading every turn to throw all but one away.** ``CatalogRow.outcome_metrics``
    is ``yields_by_turn[max(yields_by_turn)]``: the highest turn that has a
    record and non-empty numeric yields. Walking *down* from the same upper
    bound and stopping at the first turn satisfying that same predicate selects
    the same turn by construction -- so the catalog column and the chart still
    come from one source, which is what data-model.md SS1 requires and what the
    ``outcome_metrics`` docstring exists to protect.

    The gap rule is carried over unchanged: a turn in ``turn_gaps()`` is not
    read at all, never read and then discarded (data-model.md V4).
    """
    from civsim_web.viewmodels.metrics import numeric_yields

    gaps = set(int(turn) for turn in gapped_turns)
    for turn in range(min(highest_turn, METRIC_TURN_LIMIT), 0, -1):
        if turn in gaps:
            continue
        record = reads.turn_record(store, run_id, turn)
        if record is None:
            continue
        cycle = getattr(record, "turn_cycle", record)
        values = numeric_yields(cycle, registry=registry)
        if values:
            return {turn: values}
    return {}


def project_run(
    store: Any,
    run: Any,
    *,
    registry: Any,
    metrics: MetricsScope = MetricsScope.SERIES,
    with_events: bool = False,
) -> CatalogRow:
    """Assemble one catalog row from the port's published reads.

    ``turn_gaps`` is always read, never optionally: it is one of the two facts
    ``TrendEligibility`` needs, and a row assembled without it can only ever be
    *ineligible, not assessed* (Principle III, ``viewmodels/base.py``).

    ``metrics`` says how much of the run's metric history this caller needs; see
    ``MetricsScope``. It replaced a ``with_metrics: bool`` whose ``True`` meant
    "the whole series" for every caller including the one that wanted a single
    number (T075).
    """
    run_id = str(getattr(run, "run_id", "") or "")
    gaps = reads.turn_gaps(store, run_id)
    turn_count = reads.highest_recorded_turn(store, run_id)
    read_metrics = {
        MetricsScope.SERIES: yields_by_turn,
        MetricsScope.OUTCOME: latest_yields,
    }.get(MetricsScope(metrics))
    return CatalogRow(
        run=run,
        run_id=run_id,
        configuration=reads.run_configuration(store, run),
        turn_count=turn_count,
        gapped_turns=gaps,
        yields_by_turn=(
            read_metrics(
                store,
                run_id,
                registry=registry,
                highest_turn=turn_count,
                gapped_turns=gaps,
            )
            if read_metrics is not None
            else {}
        ),
        events=reads.list_run_events(store, run_id) if with_events else (),
    )


def list_catalog_rows(
    store: Any,
    *,
    registry: Any,
    run_ids: Sequence[str] | None = None,
    metrics: MetricsScope = MetricsScope.SERIES,
    with_events: bool = False,
) -> CatalogListing:
    """The FR-018 projection for every run the port can reach.

    ``run_ids`` narrows to a named set -- the comparison route's case, where the
    user has already chosen which runs to look at and enumerating the rest would
    be wasted reads. A named run the store cannot resolve is simply absent from
    ``rows``; the route turns that into the contract's ``404``, since "this run
    never existed" is a distinct answer from "the listing is partial".
    """
    if run_ids is not None:
        resolved = (store.get_run(run_id) for run_id in run_ids)
        runs = tuple(found for found in resolved if found is not None)
        partial = False
    else:
        runs, partial = _all_runs(store)

    rows = tuple(
        project_run(
            store,
            run,
            registry=registry,
            metrics=metrics,
            with_events=with_events,
        )
        for run in runs
    )
    return CatalogListing(
        rows=rows,
        listing_is_partial=partial,
        partial_reason=CATALOG_LISTING_IS_PARTIAL if partial else None,
    )
