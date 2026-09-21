"""The 003 contract's value types refuse malformed shapes structurally (T003; data-model.md SS3).

A tagged answer that could be built inconsistently would put the burden back on every reader;
these tests pin the ``__post_init__`` rules so that burden stays in ``store/contract.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from civsim_harness.models.common import CaptureId, RunId
from civsim_harness.models.run import ComparabilityStatus
from civsim_harness.store.contract import (
    CaptureImage,
    CaptureImageStatus,
    DivergenceDetail,
    DivergenceReport,
    MatchTrackingStore,
    MetricPoint,
    MetricSeries,
    ModelCallTotals,
    RunPage,
    RunQuery,
    RunSort,
    StoreSchemaVersion,
    TrendQuery,
)
from civsim_harness.store.port import MatchStore
from civsim_harness.store.sqlite_adapter import SqliteMatchStore


def test_capture_image_content_is_set_exactly_when_available() -> None:
    CaptureImage(status=CaptureImageStatus.AVAILABLE, capture_id=CaptureId("c"), content=b"x")
    with pytest.raises(ValueError):
        CaptureImage(status=CaptureImageStatus.WITHHELD, capture_id=CaptureId("c"), content=b"x")
    with pytest.raises(ValueError):
        CaptureImage(status=CaptureImageStatus.AVAILABLE, capture_id=CaptureId("c"))
    with pytest.raises(ValueError):
        CaptureImage(status=CaptureImageStatus.WITHHELD, capture_id=CaptureId("c"))  # no reason
    with pytest.raises(ValueError):
        CaptureImage(status=CaptureImageStatus.MISSING, capture_id=CaptureId("c"))  # no blob_ref


def test_trend_query_names_exactly_one_population() -> None:
    TrendQuery(run_ids=(RunId("a"),))
    TrendQuery(seed_set_id="s")
    with pytest.raises(ValueError):
        TrendQuery(run_ids=(RunId("a"),), seed_set_id="s")
    with pytest.raises(ValueError):
        TrendQuery()


def test_run_query_bounds() -> None:
    RunQuery(page=1, page_size=1)
    with pytest.raises(ValueError):
        RunQuery(page=0)
    with pytest.raises(ValueError):
        RunQuery(page_size=0)
    with pytest.raises(ValueError):
        RunQuery(page_size=10_000)


def test_run_page_cannot_exceed_its_page_size() -> None:
    with pytest.raises(ValueError):
        RunPage(runs=(), total=-1, page=1, page_size=25, sort=RunSort.STARTED_AT_DESC)


def test_model_call_totals_cost_is_none_exactly_when_nothing_was_priced() -> None:
    ModelCallTotals(
        run_id=RunId("r"),
        call_count=2,
        priced_call_count=0,
        cost_usd=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        latency_ms_total=0,
        fallback_count=0,
        retry_count=0,
    )
    with pytest.raises(ValueError):
        ModelCallTotals(
            run_id=RunId("r"),
            call_count=2,
            priced_call_count=1,
            cost_usd=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            latency_ms_total=0,
            fallback_count=0,
            retry_count=0,
        )
    with pytest.raises(ValueError):
        ModelCallTotals(
            run_id=RunId("r"),
            call_count=1,
            priced_call_count=2,
            cost_usd=1.0,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            latency_ms_total=0,
            fallback_count=0,
            retry_count=0,
        )


def test_metric_series_points_ascend_and_unavailable_means_empty() -> None:
    MetricSeries(
        run_id=RunId("r"),
        metric="m",
        points=(MetricPoint(1, 1.0), MetricPoint(2, 2.0)),
        in_progress=False,
        comparability_status=ComparabilityStatus.COMPARABLE,
    )
    with pytest.raises(ValueError):
        MetricSeries(
            run_id=RunId("r"),
            metric="m",
            points=(MetricPoint(2, 1.0), MetricPoint(1, 2.0)),
            in_progress=False,
            comparability_status=ComparabilityStatus.COMPARABLE,
        )
    with pytest.raises(ValueError):
        MetricSeries(
            run_id=RunId("r"),
            metric="m",
            points=(MetricPoint(1, 1.0),),
            in_progress=False,
            comparability_status=ComparabilityStatus.COMPARABLE,
            unavailable_reason="none",
        )


def test_divergence_report_is_set_exactly_when_something_differed() -> None:
    DivergenceReport(
        run_a=RunId("a"),
        run_b=RunId("b"),
        same_seed_set=True,
        compared_turns=(1,),
        first_divergent_turn=None,
        differed=(),
    )
    with pytest.raises(ValueError):
        DivergenceReport(
            run_a=RunId("a"),
            run_b=RunId("b"),
            same_seed_set=True,
            compared_turns=(1,),
            first_divergent_turn=1,
            differed=(),
        )
    with pytest.raises(ValueError):
        DivergenceReport(
            run_a=RunId("a"),
            run_b=RunId("b"),
            same_seed_set=True,
            compared_turns=(1,),
            first_divergent_turn=None,
            differed=(DivergenceDetail("actions", "x", "y"),),
        )


def test_schema_version_parses_orders_and_gates_readability() -> None:
    assert str(StoreSchemaVersion(1, 1)) == "1.1"
    assert StoreSchemaVersion.parse("1.1") == StoreSchemaVersion(1, 1)
    assert StoreSchemaVersion.parse("2") == StoreSchemaVersion(2, 0)
    assert StoreSchemaVersion(1, 0) < StoreSchemaVersion(1, 1) < StoreSchemaVersion(2, 0)
    assert StoreSchemaVersion(1, 7).readable_by(StoreSchemaVersion(1, 1))
    assert not StoreSchemaVersion(2, 0).readable_by(StoreSchemaVersion(1, 1))
    with pytest.raises(ValueError):
        StoreSchemaVersion(0, 0)


def test_sqlite_adapter_satisfies_both_protocols(tmp_path: Path) -> None:
    """T012: the reference adapter is a `MatchTrackingStore`, and still a `MatchStore`."""
    store = SqliteMatchStore(tmp_path / "s.db")
    try:
        assert isinstance(store, MatchStore)
        assert isinstance(store, MatchTrackingStore)
    finally:
        store.close()
