"""Catalog and comparison, end to end against the fake store (T058).

US4's Independent Test, made mechanical:

    With several completed runs recorded, select five of them and identify which
    reached the highest science output by turn 50 and the turn at which the
    leader separated from the rest, using only the interface.

Two halves, matching the task:

1. **A 50+ run catalog**, exercising filter and sort correctness at the scale
   spec.md's Scale section names -- including that paging is stable, that a
   filter narrows rather than reorders, and that an incomplete run is listed
   (FR-016) while being counted out of the trend-eligible set (Principle III).
2. **A five-run comparison**, where *following* a divergence point's `refs`
   actually opens the matching turn in each compared run (FR-022). The refs are
   checked by issuing them, not by matching them against a pattern: a reference
   that looks right and 404s is the failure FR-008 exists to prevent.

Everything here runs against the `MatchStore` fake -- this feature never touches
the game client, only the store (tasks.md Notes), so there is no live tier.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

CATALOG_SIZE = 55
COMPARED = ("run-01", "run-02", "run-03", "run-04", "run-05")


@pytest.fixture
def big_catalog() -> Any:
    """55 runs: four civilizations, two models, two of them recorded incomplete.

    `run-07` is marked `has_gaps` by the store. `run-11` is the harder case the
    constitution cares about: the store calls its record `complete` while also
    listing turn 4 as having no authoritative attempt, so the two published
    reads disagree and the run must still be kept out of every trend.
    """
    from web_support.fixtures import make_catalog_store

    return make_catalog_store(
        count=CATALOG_SIZE,
        turns=12,
        seeds=["SEED-0001", "SEED-0002"],
        civilizations=["GREECE", "ROME", "EGYPT", "NORWAY"],
        leaders=["PERICLES", "TRAJAN", "CLEOPATRA", "HARALD"],
        models=["anthropic/claude-sonnet-4", "openai/gpt-5"],
        completeness={"run-07": "has_gaps"},
        turn_gaps={"run-07": [5], "run-11": [4]},
    )


@pytest.fixture
def catalog_client(big_catalog: Any) -> Any:
    from fastapi.testclient import TestClient

    from web_support.fixtures import make_app

    with TestClient(make_app(big_catalog)) as client:
        yield client


def _json(client: Any, path: str) -> Any:
    response = client.get(path, headers={"Accept": "application/json"})
    assert response.status_code == 200, f"{path} -> {response.status_code} {response.text[:400]}"
    return response.json()


# --------------------------------------------------------------------------
# The catalog at scale (FR-018, FR-019)
# --------------------------------------------------------------------------


def test_the_catalog_lists_every_run_and_pages_through_them(catalog_client):
    """Paging covers the catalog exactly once, with no run seen twice or missed.

    The tiebreak on `run_id` is what makes this true: without it two runs
    sharing a sort value could swap between pages and one would never appear --
    a paging bug that looks exactly like a missing run.
    """
    seen: list[str] = []
    page = 1
    while True:
        body = _json(catalog_client, f"/runs?page={page}&page_size=10&sort=run_id&order=asc")
        seen.extend(run["run_id"] for run in body["runs"])
        assert body["total"] == CATALOG_SIZE
        if not body["has_more"]:
            break
        page += 1

    assert len(seen) == CATALOG_SIZE
    assert len(set(seen)) == CATALOG_SIZE
    assert seen == sorted(seen)


def test_filtering_narrows_the_catalog_without_hiding_the_count(catalog_client):
    """FR-019: filter by any field, and the page says what it filtered on."""
    everything = _json(catalog_client, "/runs?page_size=200")
    greece = _json(catalog_client, "/runs?civilization=GREECE&page_size=200")

    assert greece["total"] < everything["total"]
    assert greece["total"] > 0
    assert {run["civilization"] for run in greece["runs"]} == {"GREECE"}
    assert [(f["field"], f["operator"], f["value"]) for f in greece["filters"]] == [
        ("civilization", "eq", "GREECE")
    ]


def test_filters_compose_and_operators_work(catalog_client):
    """Several filters narrow together; `__gte` and `__contains` are honoured."""
    body = _json(
        catalog_client,
        "/runs?civilization=GREECE&turn_count__gte=12&model_primary__contains=claude&page_size=200",
    )
    assert body["runs"], "the composed filter matched nothing to check"
    for run in body["runs"]:
        assert run["civilization"] == "GREECE"
        assert run["turn_count"] >= 12
        assert "claude" in run["model_primary"]

    # A filter that excludes everything is an empty catalog with its reason,
    # not an error and not the unfiltered list (UP-005).
    empty = _json(catalog_client, "/runs?civilization=GREECE&civilization__ne=GREECE")
    assert empty["runs"] == []
    assert empty["empty_state_reason"]


def test_a_nested_field_is_filterable_and_sortable(catalog_client):
    """"Any `RunSummaryView` field" includes the nested ones (FR-019)."""
    quarantined = _json(
        catalog_client, "/runs?trend_eligibility.eligible=false&page_size=200"
    )
    assert {run["run_id"] for run in quarantined["runs"]} == {"run-07", "run-11"}

    sorted_page = _json(
        catalog_client, "/runs?sort=health.state&order=asc&page_size=200"
    )
    states = [run["health"]["state"] for run in sorted_page["runs"]]
    assert states == sorted(states)


def test_sorting_is_correct_in_both_directions(catalog_client):
    """A sort is a sort: reversing the order reverses the sequence."""
    ascending = _json(catalog_client, "/runs?sort=turn_count&order=asc&page_size=200")
    descending = _json(catalog_client, "/runs?sort=turn_count&order=desc&page_size=200")

    up = [run["turn_count"] for run in ascending["runs"]]
    down = [run["turn_count"] for run in descending["runs"]]
    assert up == sorted(up)
    assert down == sorted(down, reverse=True)
    assert set(r["run_id"] for r in ascending["runs"]) == set(
        r["run_id"] for r in descending["runs"]
    )


def test_an_incomplete_run_is_listed_and_counted_out_of_the_trend(catalog_client):
    """FR-016 marks rather than hides; Principle III counts it out anyway."""
    body = _json(catalog_client, "/runs?page_size=200")

    listed = {run["run_id"] for run in body["runs"]}
    assert {"run-07", "run-11"} <= listed
    assert set(body["quarantined_run_ids"]) == {"run-07", "run-11"}
    assert body["trend_eligible_count"] == CATALOG_SIZE - 2

    gapped = next(run for run in body["runs"] if run["run_id"] == "run-11")
    assert gapped["record_completeness_status"] == "complete"
    assert gapped["trend_eligibility"]["eligible"] is False
    assert gapped["trend_eligibility"]["gapped_turns"] == [4]


def test_the_catalog_page_answers_the_same_way_to_both_readers(catalog_client):
    """Principle VI on this route: one object, serialized twice."""
    path = "/runs?civilization=EGYPT&sort=turn_count&order=desc"
    as_json = catalog_client.get(path, headers={"Accept": "application/json"}).json()
    as_html = catalog_client.get(path, headers={"Accept": "text/html"}).text

    for run in as_json["runs"]:
        assert run["run_id"] in as_html
    assert str(as_json["total"]) in as_html


# --------------------------------------------------------------------------
# The five-run comparison (FR-020, FR-021, FR-022)
# --------------------------------------------------------------------------


@pytest.fixture
def divergent_client() -> Any:
    """Five runs on one seed whose science trajectories cross at a known turn.

    `run-02` starts behind and gains faster, so it overtakes `run-01` partway
    through: the leader change is arranged rather than incidental, which is what
    makes "the turn at which the leader separated" an assertable fact rather
    than a property of noise.
    """
    from fastapi.testclient import TestClient

    from web_support.fixtures import make_app, make_catalog_store

    store = make_catalog_store(
        count=5,
        turns=50,
        seeds=["SEED-0001"],
        civilizations=["GREECE"],
        leaders=["PERICLES"],
        models=["anthropic/claude-sonnet-4"],
        science_base=[60.0, 5.0, 4.0, 3.0, 2.0],
        science_step=[0.5, 3.0, 0.4, 0.3, 0.2],
    )
    with TestClient(make_app(store)) as client:
        yield client


def test_a_five_run_comparison_names_the_leader_and_where_it_separated(divergent_client):
    """US4's Independent Test, as an assertion.

    "Which reached the highest science output by turn 50, and the turn at which
    the leader separated from the rest" -- both answerable from one response.
    """
    body = _json(
        divergent_client,
        "/compare?runs=" + ",".join(COMPARED) + "&metrics=science_output",
    )

    assert [run["run_id"] for run in body["runs"]] == list(COMPARED)
    assert body["quarantined_run_ids"] == []
    assert body["basis"]["is_uniform"] is True

    series = {s["run_id"]: s for s in body["series"]["science_output"]}
    assert set(series) == set(COMPARED)
    final = {run_id: s["points"][-1]["value"] for run_id, s in series.items()}
    assert max(final, key=lambda run_id: final[run_id]) == "run-02"

    changes = [p for p in body["divergence_points"] if p["kind"] == "leader_change"]
    assert changes, "no leader change detected in a fixture built to contain one"
    assert changes[0]["leader_run_id"] == "run-02"
    assert changes[0]["previous_leader_run_id"] == "run-01"
    assert 1 < changes[0]["turn"] <= 50


def test_following_a_divergence_ref_opens_that_turn_in_each_compared_run(divergent_client):
    """FR-022, by issuing the references rather than by matching their shape.

    data-model.md SS11: *"One ready-to-navigate reference per compared run at
    this turn, so a client can jump to 'this turn, in each compared run' with no
    extra round trip."* A reference that looks right and 404s would satisfy a
    pattern match and fail a user, so each one is fetched.
    """
    body = _json(
        divergent_client,
        "/compare?runs=" + ",".join(COMPARED) + "&metrics=science_output",
    )
    point = next(p for p in body["divergence_points"] if p["kind"] == "leader_change")

    assert set(point["refs"]) == set(COMPARED)
    for run_id, ref in point["refs"].items():
        turn = _json(divergent_client, ref)
        assert turn["run_id"] == run_id
        assert turn["turn_number"] == point["turn"]
        # And the same reference opens a real page for the user (FR-008).
        page = divergent_client.get(ref, headers={"Accept": "text/html"})
        assert page.status_code == 200
        assert f"Turn {point['turn']}" in page.text


def test_a_quarantined_run_is_shown_and_excluded_across_the_whole_comparison():
    """FR-021 and Principle III together, through the route rather than the model.

    The comparison still *shows* the run -- the user has to be able to see why
    it was left out -- and no trend line, axis, or divergence point is drawn
    from it.
    """
    from fastapi.testclient import TestClient

    from web_support.fixtures import make_app, make_catalog_store

    store = make_catalog_store(
        count=5,
        turns=20,
        seeds=["SEED-0001"],
        civilizations=["GREECE"],
        completeness={"run-03": "has_gaps"},
        turn_gaps={"run-03": [9], "run-05": [12]},
    )
    with TestClient(make_app(store)) as client:
        body = _json(client, "/compare?runs=" + ",".join(COMPARED))
        markup = client.get(
            "/compare?runs=" + ",".join(COMPARED), headers={"Accept": "text/html"}
        ).text

    assert set(body["quarantined_run_ids"]) == {"run-03", "run-05"}
    assert {run["run_id"] for run in body["runs"]} == set(COMPARED)

    for metric, series_list in body["series"].items():
        drawn = {s["run_id"] for s in series_list}
        assert drawn == {"run-01", "run-02", "run-04"}, metric
    for point in body["divergence_points"]:
        assert set(point["refs"]).isdisjoint({"run-03", "run-05"})

    # Both facts are on the page the user reads, not only in the JSON body.
    assert "run-03" in markup
    assert "Quarantined from the trend" in markup


def test_the_comparison_is_unchanged_when_every_capture_is_withheld():
    """FR-035 / SC-016 end to end: the answer does not depend on a screenshot."""
    from fastapi.testclient import TestClient

    from web_support.fixtures import make_app, make_catalog_store

    bodies = []
    for capture_status in ("screened_clean", "withheld"):
        store = make_catalog_store(
            count=5, turns=20, seeds=["SEED-0001"], capture_status=capture_status
        )
        with TestClient(make_app(store)) as client:
            bodies.append(_json(client, "/compare?runs=" + ",".join(COMPARED)))

    assert bodies[0] == bodies[1]
    assert bodies[0]["divergence_points"] or bodies[0]["series"]


def test_the_catalog_links_into_a_comparison_of_what_it_is_showing(catalog_client):
    """The path a user actually walks: catalog -> compare, in three clicks.

    SC-006's spirit applied to US4 -- the comparison is reachable from the
    catalog without composing a query by hand.
    """
    markup = catalog_client.get(
        "/runs?civilization=GREECE&page_size=5", headers={"Accept": "text/html"}
    ).text
    assert "/compare?runs=" in markup

    start = markup.index("/compare?runs=")
    href = markup[start : markup.index('"', start)]
    body = _json(catalog_client, href.replace("&amp;", "&"))
    assert len(body["runs"]) == 5
