"""SC-008 at the scale the spec's Assumptions describe (T060).

    "Every route exercised in Phases 3-6 reaches a usable response within 2
    seconds" -- against a 300+ turn run whose late-game turns run into hundreds
    of decision steps, and a 50+ run catalog.

**What this is actually testing, and why the fixture is shaped this way.** Three
compositions in this feature scale with the record rather than with the request,
and every one of them exists because `match-store-port.md` publishes no indexed
read for what the route needs (plan.md Complexity Tracking C1):

1. `store_client/reads.highest_recorded_turn` -- no published read answers "what
   turn is this run on", so it composes `list_save_points` with a bounded
   `get_turn_cycle` probe. It runs on every hit of `/` and `/runs/{id}`.
2. `store_client/catalog.yields_by_turn` -- no published metric-series read, so a
   series costs one `get_turn_cycle` per turn. It runs on `/runs/{id}/metrics`
   and on `/compare`, once per compared run.
3. `store_client/catalog.list_catalog_rows` -- no published catalog listing, so
   `/runs` reads each run and, for Principle III, each run's `turn_gaps()`.

The US3 notes flagged the second of these directly: "T060's scale test should be
read as a check on this composition specifically." So the fixture is built to
make those three expensive rather than to be merely large -- a 300-turn run with
the step count climbing into the hundreds late, and a 50-run catalog on top.

**The budget is wall clock against the fake, deliberately.** The fake is an
in-memory store, so these numbers are a floor, not a prediction of production.
What the assertion catches is a composition that is accidentally quadratic in
turns or steps -- the failure that turns a 30-turn demo into an unusable
300-turn replay. A real store's own latency sits on top and is deliberately not
modelled here, since this feature does not control it.

**Amended 2026-09-22 (T282): wall clock is the tripwire, not the assertion.**
Twelve of the timed cases clear `BUDGET_SECONDS` by 7x or more and are left
exactly as they were. One did not -- `/compare` rendered as HTML, at 1.4x --
and a 1.4x margin against an in-memory fake measures the scheduler as much as
the code. It failed twice on a box running this project's own parallel suites
and was dismissed both times, which is the real cost of a thin bound: it does
not catch regressions, it teaches readers to ignore red. That case now carries
a work-done assertion -- `/compare` pays exactly one series read per compared
run -- and keeps its clock only as this docstring's unbounded-loop tripwire,
at a ceiling derived from measurement. See `COMPARE_HTML_TRIPWIRE_SECONDS`.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest

from web_support.fixtures import (
    make_catalog_store,
    make_client,
    make_configuration,
    make_run,
    make_save_point,
    make_turn_cycle,
)

JSON = {"Accept": "application/json"}

#: SC-008's bound, verbatim.
BUDGET_SECONDS = 2.0

#: MEASURED 2026-09-22, unloaded box, five samples of the `/compare` HTML case at
#: 55 runs x 320 turns: 1.18 / 1.25 / 1.32 / 1.37 / 1.42 s. The worst is the
#: figure kept here, because a tripwire is sized against the bad sample, not the
#: median. The same run measured `/runs` HTML at 0.23-0.31 s (a 7x margin against
#: `BUDGET_SECONDS`) and the `/compare` JSON sibling at 1.15-1.26 s.
COMPARE_HTML_MEASURED_SECONDS = 1.42

#: The `/compare` HTML tripwire, re-derived (T282).
#:
#: **Why this case is not on `BUDGET_SECONDS`.** At 1.42 s measured against a
#: 2.0 s bound it had a 1.4x margin, where every other timed case in this module
#: clears its budget by 7x or more. It failed repeatedly on 2026-09-22 and was
#: twice dismissed as "load on this box"; the load was this project's own
#: parallel suites, and on a quiet box with one suite running it passes. So
#: there is no product defect here -- but a 1.4x wall-clock margin against an
#: in-memory fake is not a signal. It reports scheduler contention as an SC-008
#: product failure, and it did, twice, and was believed neither time. A test
#: that cries wolf is worse than no test: it trains the next reader to dismiss
#: the red, which is exactly how a real regression gets waved through.
#:
#: **What replaced it as the assertion.** SC-008's `/compare` check is not
#: weakened, it is moved to where it holds:
#: `test_comparing_five_long_runs_pays_one_series_read_per_run` counts the reads
#: the route actually makes, on the argument
#: `test_the_catalog_row_reads_only_the_turns_it_shows` already makes below --
#: a timing assertion against an in-memory fake "would go green again on a
#: faster machine even if the series read came back". The read count is the
#: load-immune form of the same claim, and it is strictly stronger: it fails on
#: the *cause* (an accidentally quadratic composition) rather than on its
#: wall-clock symptom.
#:
#: **Why 4x and not a round number.** This number stays only as the
#: unbounded-loop tripwire the module docstring is built around, so it is sized
#: against that failure, which is order-of-magnitude. The regression this
#: fixture exists for -- T075's catalog row reading the whole series to keep one
#: turn -- cost ten seconds where the fixed row costs 0.27 s: a ~37x blow-up. 4x
#: catches anything of that shape with room to spare. At the other end, the
#: contention that actually produced today's red pushed 1.42 s past 2.0 s, i.e.
#: by under 1.5x; 4x leaves that nearly three times' headroom, so a shared core
#: cannot trip it. `BUDGET_SECONDS` is untouched for every case that clears it
#: 7x over, `/runs` on this same page included.
COMPARE_HTML_TRIPWIRE_SECONDS = 4.0 * COMPARE_HTML_MEASURED_SECONDS  # 5.68 s

#: The run's shape. `HEAVY_FROM` onward is the late game the task describes;
#: `HEAVY_STEPS` is "running into hundreds of steps".
TURNS = 320
HEAVY_FROM = 280
HEAVY_STEPS = 200
EARLY_STEPS = 2

CATALOG_RUNS = 55


def _scale_store() -> Any:
    from civsim_web.store_client.fake import FakeMatchStore

    configuration = make_configuration()
    run = make_run("run-1", config_id=configuration.config_id, lifecycle_state="finished")
    records = [
        make_turn_cycle(
            turn,
            run_id="run-1",
            step_count=HEAVY_STEPS if turn >= HEAVY_FROM else EARLY_STEPS,
            yields={"science_output": float(turn), "culture_output": float(turn) / 2},
        )
        for turn in range(1, TURNS + 1)
    ]
    return FakeMatchStore(
        runs=[run],
        configurations=[configuration],
        turn_cycles=records,
        save_points=[make_save_point(turn, run_id="run-1") for turn in range(1, TURNS + 1)],
        # No captures: `/captures/{id}/image` is exercised against the catalog
        # store instead, and seeding one blob per step here would measure the
        # fixture's memory rather than the route's cost.
        captures=[],
    )


@pytest.fixture(scope="module")
def scale_client() -> Iterator[Any]:
    client = make_client(_scale_store())
    with client:
        yield client


#: SC-008's catalog scale is "50+ recorded runs" *and* "a run of 300+ turns" in
#: one sentence, and `catalog_client` only ever had the first half: 55 runs of
#: 30 turns. That mattered because the two expensive catalog compositions scale
#: with *turns*, not with runs, so a 30-turn fixture could not see them (T075).
CATALOG_TURNS = TURNS


@pytest.fixture(scope="module")
def catalog_client() -> Iterator[Any]:
    client = make_client(make_catalog_store(count=CATALOG_RUNS, turns=30))
    with client:
        yield client


@pytest.fixture(scope="module")
def long_catalog_client() -> Iterator[Any]:
    """SC-008's two halves at once: 55 runs, every one of them 320 turns.

    Captures are omitted for the same reason `_scale_store` omits them -- FR-035
    makes the comparison view capture-free by construction, and seeding one blob
    per step across 55 long runs would measure the fixture rather than the route.
    """
    client = make_client(
        make_catalog_store(count=CATALOG_RUNS, turns=CATALOG_TURNS, with_captures=False)
    )
    with client:
        yield client


def _timed(client: Any, path: str) -> tuple[Any, float]:
    started = time.perf_counter()
    response = client.get(path, headers=JSON)
    return response, time.perf_counter() - started


def _assert_within_budget(client: Any, path: str) -> Any:
    response, elapsed = _timed(client, path)
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text[:300]}"
    assert elapsed < BUDGET_SECONDS, (
        f"{path} took {elapsed:.2f}s against a {TURNS}-turn run; SC-008 allows "
        f"{BUDGET_SECONDS}s and this is the in-memory fake, so a real store has "
        f"no headroom left"
    )
    return response


# --------------------------------------------------------------------------
# The single-run routes, against a 320-turn run
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/runs/run-1",
        "/runs/run-1/events",
        "/runs/run-1/metrics",
        f"/runs/run-1/turns/{TURNS}",
        f"/runs/run-1/turns/{TURNS}/steps/1",
        f"/runs/run-1/turns/{TURNS}/steps/{HEAVY_STEPS}",
        f"/runs/run-1/turns/{TURNS}/panels/current_turn.yields",
        f"/runs/run-1/turns/{TURNS}/steps/1/panels/observation.cities",
        "/runs/run-1/panels/run.header",
        "/healthz",
    ],
)
def test_every_single_run_route_answers_within_the_budget(scale_client, path):
    _assert_within_budget(scale_client, path)


def test_the_glance_view_does_not_pay_for_every_turn_it_is_not_showing(scale_client):
    """`/runs/{id}` must be O(current turn), not O(run length).

    `highest_recorded_turn` is the composition at risk here: it probes for the
    latest turn because no published read answers the question. If that probe
    ever degrades into a scan, this is the route where a 300-turn run stops
    being watchable live -- which is US1's entire premise.
    """
    _, elapsed = _timed(scale_client, "/runs/run-1")
    assert elapsed < BUDGET_SECONDS / 2, (
        f"the live glance view took {elapsed:.2f}s on a {TURNS}-turn run; it is "
        f"polled every 2 seconds (research R5), so half the budget is the "
        f"practical bound, not the whole of it"
    )


def test_a_late_game_turn_with_hundreds_of_steps_pages_rather_than_returning_them_all(
    scale_client,
):
    """data-model.md §5: `steps` on the default response is a *bounded window*.

    Asserted as behaviour, not as timing: a route that returned 200 steps inline
    would still pass the clock against an in-memory fake and fall over against a
    real store. The window is what makes the budget hold at scale.
    """
    body = _assert_within_budget(scale_client, f"/runs/run-1/turns/{TURNS}").json()
    window = body["steps"]
    assert 0 < len(window) < HEAVY_STEPS, (
        f"turn {TURNS} has {HEAVY_STEPS} steps and the default response carried "
        f"{len(window)} -- an unbounded step list is the scale failure §5 names"
    )
    window_meta = body["step_window"]
    assert window_meta["total"] == HEAVY_STEPS
    assert window_meta["has_more"] is True
    # "No page may skip an index without marking it" -- so the omitted indices
    # must be *listed*, not absent. An empty list here alongside `has_more`
    # would be the failure §5 names: a page boundary indistinguishable from a
    # record gap.
    skipped = window_meta["skipped_step_indices"]
    assert skipped == list(range(len(window) + 1, HEAVY_STEPS + 1))
    assert len(window) + len(skipped) == HEAVY_STEPS


def test_paging_to_the_end_of_a_heavy_turn_stays_within_budget(scale_client):
    """The last page is the expensive one if paging is implemented by scanning."""
    first = scale_client.get(f"/runs/run-1/turns/{TURNS}", headers=JSON).json()
    size = first["step_window"]["limit"]
    last_offset = ((HEAVY_STEPS - 1) // size) * size

    body = _assert_within_budget(
        scale_client, f"/runs/run-1/turns/{TURNS}?step_offset={last_offset}"
    ).json()
    assert body["steps"], "the last page must carry the tail, not be empty"
    assert body["steps"][-1]["step_index"] == HEAVY_STEPS


def test_the_metric_trajectory_of_a_long_run_stays_within_budget(scale_client):
    """The `yields_by_turn` composition -- one `get_turn_cycle` per turn (C1).

    This is the read the US3 notes singled out for T060. It is linear in turns
    by construction; the assertion is that the constant is small enough that
    SC-008 still holds at 320 turns, and that the series really did cover the
    run rather than quietly truncating to look fast.
    """
    body = _assert_within_budget(scale_client, "/runs/run-1/metrics").json()
    series = {s["metric_name"]: s for s in body["series"]}
    assert "science_output" in series
    assert len(series["science_output"]["points"]) == TURNS


# --------------------------------------------------------------------------
# The catalog routes, against 55 runs
# --------------------------------------------------------------------------


def test_the_catalog_listing_stays_within_budget(catalog_client):
    """`/runs` reads every run *and* every run's `turn_gaps()` (Principle III)."""
    body = _assert_within_budget(catalog_client, "/runs").json()
    assert body["total"] == CATALOG_RUNS
    assert len(body["runs"]) < CATALOG_RUNS, "the listing is server-side paginated"


@pytest.mark.parametrize(
    "query",
    [
        "?sort=turn_count&order=desc",
        "?sort=trend_eligibility.eligible",
        "?civilization=GREECE",
        "?page=2&page_size=25",
    ],
    ids=["sort", "nested_sort", "filter", "page_2"],
)
def test_filtering_and_sorting_the_catalog_stays_within_budget(catalog_client, query):
    _assert_within_budget(catalog_client, f"/runs{query}")


def test_comparing_five_runs_stays_within_budget(catalog_client):
    """`/compare` pays `yields_by_turn` once per compared run."""
    runs = ",".join(f"run-{n:02d}" for n in range(1, 6))
    body = _assert_within_budget(
        catalog_client, f"/compare?runs={runs}&metrics=science_output,culture_output"
    ).json()
    assert len(body["runs"]) == 5
    assert body["series"]


# --------------------------------------------------------------------------
# SC-008's actual sentence: 50+ runs AND 300+ turns, in one catalog (T075)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/runs",
        "/runs?sort=turn_count&order=desc",
        "/runs?sort=outcome_metrics.science_output&order=desc",
        "/runs?civilization=GREECE",
        "/runs?page=2&page_size=25",
    ],
    ids=["listing", "sort", "sort_by_outcome", "filter", "page_2"],
)
def test_the_catalog_stays_within_budget_when_its_runs_are_long(long_catalog_client, path):
    """The half of SC-008 the 30-turn fixture could not see.

    `/runs` projects a row per run, and two of the three compositions in this
    module's docstring scale with *turn count*: `highest_recorded_turn` probes,
    and the metric read. At 55 runs x 30 turns the second was cheap enough to
    hide; at 55 x 320 -- the scale SC-008 names in one sentence, "a run of 300+
    turns and 50+ recorded runs in the catalog" -- it was **ten seconds**,
    five times the budget, because the row asked for the whole series to keep
    only its last turn.

    `MetricsScope.OUTCOME` is the fix; this is the test that would have caught
    it, and that stops the series read reappearing here by default.
    """
    _assert_within_budget(long_catalog_client, path)


def test_comparing_five_long_runs_stays_within_budget(long_catalog_client):
    """`/compare` pays `yields_by_turn` per compared run -- here, 320 turns each.

    This one is genuinely unavoidable: a trajectory *is* the per-turn series, so
    the read cannot be narrowed the way the catalog's could. US4 note 3 called
    this composition the point of the scale test and it had only ever been
    measured against 30-turn runs.
    """
    runs = ",".join(f"run-{n:02d}" for n in range(1, 6))
    body = _assert_within_budget(
        long_catalog_client, f"/compare?runs={runs}&metrics=science_output,culture_output"
    ).json()
    assert len(body["runs"]) == 5
    assert all(
        len(series["points"]) > 300
        for series_list in body["series"].values()
        for series in series_list
    ), "the fixture is not actually exercising 300+ turn trajectories"


class _CountingStore:
    """A pass-through over the fake that tallies the port reads a request makes.

    Wraps rather than replaces, so the route under test sees exactly the store
    the timed cases see and the tally is of the real composition's work rather
    than of a stub's guess at it. Non-callables are forwarded untouched, and an
    attribute the inner store does not have still raises -- `_all_runs` probes
    `getattr(store, "list_runs", None)`, and a proxy that answered every name
    would change which path the catalog takes.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            return attribute(*args, **kwargs)

        return counted


def test_comparing_five_long_runs_pays_one_series_read_per_run(long_catalog_client, monkeypatch):
    """`/compare`'s cost, asserted as work done rather than as wall clock (T282).

    This is the assertion the timing check above was standing in for, written
    the way `test_the_catalog_row_reads_only_the_turns_it_shows` argues for: the
    clock against an in-memory fake "would go green again on a faster machine
    even if the series read came back", so count the reads instead and the
    regression is caught by its cause.

    **What it pins.** `/compare` composes `yields_by_turn` once per compared run
    (module docstring, composition 2) and that read is one `get_turn_cycle` per
    turn. So five runs of 320 turns is five series reads and 1600 turn reads,
    exactly -- no second walk to fill `outcome_metrics` (which is
    `yields_by_turn[max(...)]`, already in hand), and no re-walk per requested
    metric, which is the shape that would turn a two-metric comparison into a
    four-run-lengths one and a ten-metric one into something unusable.

    **What it would catch that the clock would not.** Any composition that
    reappears above this seam: a row that walks the series again for its outcome
    column (T075's regression, one layer out), a per-metric series read, a
    `MetricsScope.SERIES` leaking back into the `/runs` projection that
    `/compare` shares. Every one of those is a multiple of the run length, and
    every one of them is invisible to a wall-clock bound the day someone runs it
    on a faster machine. It is also load-immune, which the clock is not: this is
    the case that failed twice on 2026-09-22 under this project's own parallel
    suites and was twice dismissed as "load on this box".
    """
    from civsim_web.store_client import catalog as catalog_module

    series_reads: list[tuple[str, int]] = []
    unwrapped = catalog_module.yields_by_turn

    def counting_yields_by_turn(store, run_id, **kwargs):
        series_reads.append((run_id, int(kwargs["highest_turn"])))
        return unwrapped(store, run_id, **kwargs)

    # `project_run` resolves the series reader off this module global on every
    # call, so patching the name is what the route will actually reach for.
    monkeypatch.setattr(catalog_module, "yields_by_turn", counting_yields_by_turn)

    app = long_catalog_client.app
    counting = _CountingStore(app.state.store)
    monkeypatch.setattr(app.state, "store", counting)

    runs = [f"run-{n:02d}" for n in range(1, 6)]
    response = long_catalog_client.get(
        f"/compare?runs={','.join(runs)}&metrics=science_output,culture_output",
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 200

    assert [run_id for run_id, _ in series_reads] == runs, (
        f"/compare over {len(runs)} runs took the series read {len(series_reads)} "
        f"times, for {[run_id for run_id, _ in series_reads]} -- one per compared "
        f"run is the whole of what a trajectory costs; anything else is a walk "
        f"that composed on top of it"
    )
    assert all(highest == CATALOG_TURNS for _, highest in series_reads), (
        f"a series read was bounded at {sorted({h for _, h in series_reads})} "
        f"rather than the run's {CATALOG_TURNS} turns -- the fixture is not "
        f"exercising the long-run case this test is named for"
    )

    expected_turn_reads = len(runs) * CATALOG_TURNS
    assert counting.calls["get_turn_cycle"] == expected_turn_reads, (
        f"/compare paid {counting.calls['get_turn_cycle']} get_turn_cycle reads "
        f"for {len(runs)} runs of {CATALOG_TURNS} turns; a trajectory is the "
        f"per-turn series, so {expected_turn_reads} is the floor and also the "
        f"ceiling -- more means a second walk composed on top of the first, "
        f"which is the accidentally-quadratic failure this module exists to "
        f"catch. Full tally: {dict(counting.calls)}"
    )
    # The cheap path through `highest_recorded_turn`: Principle IV puts a
    # quicksave at the start of every turn, so the bound comes off the save list
    # and the forward `get_turn_cycle` probe never runs. If that ever inverts,
    # the turn-read count above absorbs 320 extra reads per run silently.
    assert counting.calls["list_save_points"] == len(runs)


@pytest.mark.parametrize(
    ("path", "ceiling"),
    [
        ("/runs", BUDGET_SECONDS),
        (
            "/compare?runs=run-01,run-02,run-03,run-04,run-05",
            COMPARE_HTML_TRIPWIRE_SECONDS,
        ),
    ],
    ids=["runs", "compare"],
)
def test_the_html_catalog_pages_stay_within_budget_at_full_scale(
    long_catalog_client, path, ceiling
):
    """SC-008 is about a *usable response*, and the user's is the HTML one.

    The module argues HTML is where an unbounded loop shows up, and then timed
    HTML for three single-run routes only. These are the two catalog pages.

    `/runs` is on SC-008's bound verbatim and clears it 7x over. `/compare` is
    on `COMPARE_HTML_TRIPWIRE_SECONDS`, which is derived from a measurement
    rather than from the success criterion, and the constant's own comment says
    why and what took over the SC-008 claim -- this is a tripwire for an
    unbounded loop, not the `/compare` scale assertion, which is now
    `test_comparing_five_long_runs_pays_one_series_read_per_run`.
    """
    started = time.perf_counter()
    response = long_catalog_client.get(path, headers={"Accept": "text/html"})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < ceiling, (
        f"{path} rendered in {elapsed:.2f}s against a {ceiling:.2f}s ceiling. "
        f"That ceiling is sized for an unbounded loop, not for a tight margin, "
        f"so this is not scheduler noise -- look for a walk that composed on "
        f"top of another walk, and check "
        f"test_comparing_five_long_runs_pays_one_series_read_per_run, which "
        f"names the composition directly"
    )


def test_the_catalog_row_reads_only_the_turns_it_shows(long_catalog_client):
    """The mechanism behind the fix, not just its wall-clock symptom (T075).

    A timing assertion against an in-memory fake is a blunt instrument: it would
    go green again on a faster machine even if the series read came back. This
    counts the reads instead, so the regression is caught by its cause.

    `outcome_metrics` must still come from the same per-turn yields the chart is
    drawn from (data-model.md SS1) -- `latest_yields` selects the identical turn
    `max(yields_by_turn)` would have, it just stops there.
    """
    from civsim_web.store_client.catalog import MetricsScope, latest_yields, yields_by_turn

    app_store = long_catalog_client.app.state.store
    registry = long_catalog_client.app.state.registry

    full = yields_by_turn(
        app_store, "run-01", registry=registry, highest_turn=CATALOG_TURNS, gapped_turns=()
    )
    latest = latest_yields(
        app_store, "run-01", registry=registry, highest_turn=CATALOG_TURNS, gapped_turns=()
    )

    assert len(full) > 300, "the fixture must really carry a long series"
    assert latest == {max(full): full[max(full)]}, (
        "latest_yields picked a different turn than the full walk's last -- the "
        "catalog column and the chart would disagree about the same run"
    )
    assert MetricsScope.OUTCOME is not MetricsScope.SERIES


def test_a_capture_image_stays_within_budget(catalog_client):
    """`/captures/{id}/image` -- the one route that returns bytes, not a view."""
    turn = catalog_client.get("/runs/run-01/turns/1", headers=JSON).json()
    capture_id = turn["steps"][0]["capture"]["capture_id"]

    started = time.perf_counter()
    response = catalog_client.get(f"/captures/{capture_id}/image")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < BUDGET_SECONDS


# --------------------------------------------------------------------------
# The rendered pages, which are where an unbounded loop actually shows up
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/runs/run-1", f"/runs/run-1/turns/{TURNS}", "/runs/run-1/metrics"],
)
def test_the_html_rendering_of_a_long_run_stays_within_budget(scale_client, path):
    """SC-008 is about a *usable response*, and the user's is the HTML one.

    Templates walk the serialized model (`_macros.html::node`), so an unbounded
    collection costs more here than in the JSON body it came from.
    """
    started = time.perf_counter()
    response = scale_client.get(path, headers={"Accept": "text/html"})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < BUDGET_SECONDS, f"{path} rendered in {elapsed:.2f}s"
