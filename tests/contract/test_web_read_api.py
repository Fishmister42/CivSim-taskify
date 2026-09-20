"""Web Read API conformance (contracts/web-read-api.md).

This suite is this feature's analogue of 002's port-conformance tests: it runs
against the `MatchStore` fake, needs no harness and no game client, and turns
three of spec.md's release-blocking audits into something CI answers on every
build rather than something a human re-checks per release.

**This file accumulates across stories.** T033 (US1) opened it; T039 (US2),
T047 (US3), T057 (US4), and T061 (Polish) each add cases. Two conventions keep
those additions from colliding:

1. **`ROUTES` is the parity matrix.** T039's "full matrix across every route
   registered so far" is discharged by *appending a `Route` to that list*, not
   by writing another parity test. Every route a story adds should appear there
   the moment it exists.
2. **One section per story**, in task order, each under its own banner comment.
   Add to your story's section; leave the others alone.

The parity mechanism itself is worth understanding before extending it. Every
template renders values through the macros in `templates/_macros.html`, which
emit `data-field="<dotted.json.path>"` on each leaf. So "every field in the JSON
body is in the HTML and vice versa" (FR-007, SC-004, UP-002) is checkable by
walking the JSON and matching paths -- no per-field checklist, and no way for a
model to grow a field that appears in one reader and not the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("fastapi")

# --------------------------------------------------------------------------
# Shared helpers -- used by every story's section below
# --------------------------------------------------------------------------

_DATA_FIELD = re.compile(r'data-field="([^"]*)"')


def json_leaves(value: Any, prefix: str = "") -> set[str]:
    """Every leaf path in a serialized response body.

    An *empty* container is a leaf: `cost: {}` is a value a reader must be able
    to see, and a row that simply vanishes is indistinguishable from one nobody
    thought about (UP-005). The `node` macro treats empty containers the same
    way, which is what makes the two halves of this comparison line up.
    """
    if isinstance(value, dict):
        if not value:
            return {prefix}
        found: set[str] = set()
        for key, item in value.items():
            found |= json_leaves(item, f"{prefix}.{key}" if prefix else str(key))
        return found
    if isinstance(value, list):
        if not value:
            return {prefix}
        found = set()
        for index, item in enumerate(value):
            found |= json_leaves(item, f"{prefix}.{index}" if prefix else str(index))
        return found
    return {prefix}


def html_fields(markup: str) -> set[str]:
    """Every JSON path the rendered page cites."""
    return {match for match in _DATA_FIELD.findall(markup) if match}


def fetch_both(client: Any, path: str) -> tuple[Any, str]:
    """The same URL, as the directing session sees it and as the user does.

    Two requests to one path, differing only in `Accept` -- which is the whole
    of the content-negotiation contract. Anything that differs between them
    beyond serialization is a Principle VI violation.
    """
    as_json = client.get(path, headers={"Accept": "application/json"})
    as_html = client.get(path, headers={"Accept": "text/html"})
    assert as_json.status_code == as_html.status_code, (
        f"{path} answered {as_json.status_code} to a JSON caller and "
        f"{as_html.status_code} to a browser"
    )
    return (as_json.json(), as_html.text)


@dataclass(frozen=True)
class Route:
    """One entry in the JSON/HTML parity matrix."""

    path: str
    story: str
    note: str = ""
    store_kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.story}:{self.path}"


#: **The parity matrix.** Append the routes your story adds; do not write a
#: second parity test. Order is by story, then by the contract's route table.
ROUTES: list[Route] = [
    Route("/", "US1", "landing -- redirects to the single active run"),
    Route("/runs/run-1", "US1", "the live glance"),
    Route("/runs/run-1/turns/2", "US1", "one turn's full record"),
    Route("/runs/run-1/turns/2/steps/1", "US1", "one decision step"),
    Route("/runs/run-1/events", "US1", "the run timeline"),
    # T039 (US2): /runs/{id}/turns/{n}/panels/{panel_id}, /runs/{id}/panels/{panel_id}
    # T047 (US3): /runs/{id}/metrics
    # T057 (US4): /runs, /compare
]


# ==========================================================================
# US1 (T033) -- JSON/HTML parity, and the capture fail-closed matrix
# ==========================================================================


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.id)
def test_json_html_parity(web_store_factory, route):
    """Every field in the JSON body is in the HTML, and vice versa.

    This is SC-004's per-release panel audit ("100% of panels presented to the
    user are retrievable by the directing session, and 100% of what the session
    retrieves is presentable to the user"), made mechanical. A field present in
    one reader and absent from the other fails here, on every build.
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory(**route.store_kwargs)) as client:
        body, markup = fetch_both(client, route.path)

    expected = json_leaves(body)
    rendered = html_fields(markup)

    missing_from_html = sorted(expected - rendered)
    assert not missing_from_html, (
        f"{route.path}: the JSON body carries fields the page never shows: "
        f"{missing_from_html[:10]}"
    )
    missing_from_json = sorted(rendered - expected)
    assert not missing_from_json, (
        f"{route.path}: the page cites fields the JSON body lacks: "
        f"{missing_from_json[:10]}"
    )


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.id)
def test_format_override_matches_accept_header(web_store_factory, route):
    """`?format=json` and `Accept: application/json` are the same branch.

    contracts/web-read-api.md offers the query override so a human can inspect a
    raw body in a browser tab. It must not be a second negotiation path that
    could answer differently from the header one.
    """
    from web_support.fixtures import make_client

    separator = "&" if "?" in route.path else "?"
    with make_client(web_store_factory(**route.store_kwargs)) as client:
        by_header = client.get(route.path, headers={"Accept": "application/json"}).json()
        by_query = client.get(f"{route.path}{separator}format=json").json()

    assert json_leaves(by_header) == json_leaves(by_query)


def test_landing_redirects_to_the_single_active_run(web_client):
    """`GET /` resolves to whichever run is live (contracts/web-read-api.md)."""
    response = web_client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].startswith("/runs/run-1")


def test_landing_offers_a_chooser_when_several_runs_are_active(web_store_factory):
    """Spec Edge Cases: the user can always tell which run they are looking at."""
    from web_support.fixtures import make_client, make_run

    store = web_store_factory(extra_runs=[make_run("run-2", lifecycle_state="playing")])
    with make_client(store) as client:
        response = client.get("/", follow_redirects=False, headers={"Accept": "application/json"})
        assert response.status_code == 200
        body = response.json()

    assert {run["run_id"] for run in body["active_runs"]} == {"run-1", "run-2"}


def test_landing_explains_itself_when_no_run_is_active(web_store_factory):
    """No runs is an explanatory empty state, never an error or a blank page."""
    from web_support.fixtures import make_client

    store = web_store_factory(turns=0, lifecycle_state="finished")
    with make_client(store) as client:
        response = client.get("/", headers={"Accept": "application/json"})

    assert response.status_code == 200
    assert response.json()["empty_state_reason"]


# -- the capture fail-closed matrix (data-model.md V2, SC-014) --------------

#: Every `screening_status` this feature knows to check, plus one it does not.
#: The unknown value is the point of the matrix: a future 002 schema addition
#: must resolve to unavailable, never to clean-by-default.
CAPTURE_MATRIX: tuple[tuple[str, dict[str, Any], bool, str], ...] = (
    ("screened_clean", {"capture_status": "screened_clean"}, True, ""),
    ("withheld", {"capture_status": "withheld"}, False, "withheld"),
    (
        "withheld_capture_failed",
        {"capture_status": "withheld", "withheld_reason": "capture_failed"},
        False,
        "capture_failed",
    ),
    ("missing_record", {"with_captures": False}, False, "missing_record"),
    (
        "unrecognized_status",
        {"capture_status": "quarantined_pending_review_v3"},
        False,
        "unrecognized_status",
    ),
)


@pytest.mark.parametrize(
    ("case", "store_kwargs", "expected_available", "expected_reason"),
    CAPTURE_MATRIX,
    ids=[case[0] for case in CAPTURE_MATRIX],
)
def test_capture_fail_closed(
    web_store_factory, case, store_kwargs, expected_available, expected_reason
):
    """`available` is `screening_status == "screened_clean"`, full stop.

    data-model.md SS7 states the rule and its consequence in the same breath:
    `withheld`, an absent record, and a status this code does not recognize all
    resolve to `available = false`. The unrecognized case is the one that
    matters most -- a future schema value must not silently un-gate a capture
    this feature was never updated to understand.
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory(**store_kwargs)) as client:
        body = client.get("/runs/run-1/turns/2", headers={"Accept": "application/json"}).json()

    capture = body["steps"][0]["capture"]
    assert capture["available"] is expected_available, case
    if expected_available:
        assert capture["unavailable_reason"] is None
        assert capture["image_url"] == f"/captures/{capture['capture_id']}/image"
    else:
        assert capture["unavailable_reason"] == expected_reason, case
        # No URL is offered for something that may not be shown: an image_url a
        # client could still GET would make the gate advisory rather than real.
        assert capture["image_url"] is None


@pytest.mark.parametrize(
    ("case", "store_kwargs", "expected_available", "expected_reason"),
    CAPTURE_MATRIX,
    ids=[case[0] for case in CAPTURE_MATRIX],
)
def test_capture_image_route_honours_the_same_gate(
    web_store_factory, case, store_kwargs, expected_available, expected_reason
):
    """`/captures/{id}/image` 404s whatever `CaptureView.available` refuses.

    Never a 200 with a placeholder image standing in for the real one, which
    contracts/web-read-api.md rules out because a placeholder can be mistaken
    for content.
    """
    from web_support.fixtures import make_client

    capture_id = "cap-tc-run-1-t2-a0-s1"
    with make_client(web_store_factory(**store_kwargs)) as client:
        response = client.get(f"/captures/{capture_id}/image")

    if expected_available:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/")
        return

    assert response.status_code == 404, case
    body = response.json()
    assert body["kind"] == "capture_unavailable"
    assert body["detail"]["unavailable_reason"] == expected_reason


def test_a_withheld_capture_still_renders_its_structured_panels(web_store_factory):
    """FR-034 / SC-015: the turn's panels render normally, capture marked absent."""
    from web_support.fixtures import make_client

    with make_client(web_store_factory(capture_status="withheld")) as client:
        body, markup = fetch_both(client, "/runs/run-1/turns/2")

    step = body["steps"][0]
    assert step["observation"], "observation panels must still render"
    assert step["decision"]["action_label"]
    assert step["capture"]["available"] is False
    assert "no capture shown" in markup


# -- the remaining contract obligations US1's routes can already be held to --


def test_store_unreachable_is_503_everywhere_but_healthz(web_store_factory):
    """The contract's blanket rule, and the distinction it exists to preserve.

    "Store unreachable" and "run not found" must not look alike to a client: one
    means nothing can be read right now, the other means this particular thing
    does not exist.
    """
    from web_support.fixtures import make_client

    store = web_store_factory()
    store.set_health(ok=False, detail="fixture marked the store unreachable")

    with make_client(store) as client:
        health = client.get("/healthz", headers={"Accept": "application/json"})
        assert health.status_code == 200
        assert health.json()["store"]["ok"] is False

        for path in ("/", "/runs/run-1", "/runs/run-1/turns/1", "/runs/run-1/events"):
            response = client.get(path, headers={"Accept": "application/json"})
            assert response.status_code == 503, path
            assert response.json()["kind"] == "store_unreachable", path


def test_a_gap_turn_is_distinguishable_from_a_turn_that_never_existed(web_store_factory):
    """FR-016: a gap is marked, never silently skipped or shown as complete."""
    from web_support.fixtures import make_client

    store = web_store_factory(turns=4, turn_gaps={"run-1": [3]})
    with make_client(store) as client:
        gap = client.get("/runs/run-1/turns/3", headers={"Accept": "application/json"})
        absent = client.get("/runs/run-1/turns/77", headers={"Accept": "application/json"})

    assert gap.status_code == 404
    assert gap.json()["kind"] == "turn_gap"
    assert 3 in gap.json()["detail"]["turn_gaps"]

    assert absent.status_code == 404
    assert absent.json()["kind"] == "turn_not_found"
    assert gap.json()["kind"] != absent.json()["kind"]


def test_an_unregistered_observation_entry_is_dropped(web_store_factory):
    """data-model.md SS6, verbatim: dropped, not passed through with a label.

    The count of dropped entries is reported -- it says how many the filter
    removed, never what they were -- so the drop is honest rather than silent
    (UP-005).
    """
    from web_support.fixtures import make_client, make_turn_cycle

    store = web_store_factory(turns=1)
    store.seed_turn_cycle(
        make_turn_cycle(
            2,
            observation_declaration_ids=("cities.state", "debug.hidden_ai_intent"),
        )
    )

    with make_client(store) as client:
        body = client.get("/runs/run-1/turns/2", headers={"Accept": "application/json"}).json()

    step = body["steps"][0]
    rendered = {entry["panel_id"] for entry in step["observation"]}
    assert rendered == {"observation.cities"}
    assert step["dropped_observation_count"] == 1
    assert "debug" not in str(body)


def test_every_observation_entry_carries_its_parity_basis(web_store_factory):
    """SC-005's per-panel auditability, on the response rather than the registry.

    The registry refusing to load an `in_game` panel without a `parity_basis`
    (T016) proves the declarations are complete. This proves the responses
    actually carry them, which is what an auditor reads.
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory()) as client:
        body = client.get("/runs/run-1/turns/2", headers={"Accept": "application/json"}).json()

    entries = body["steps"][0]["observation"]
    assert entries
    for entry in entries:
        assert entry["panel_basis"].strip(), entry["panel_id"]


def test_reasoning_degrades_gracefully(web_store_factory):
    """data-model.md SS8: empty is labelled, long is truncated with an expander.

    Both panels still identify the action taken regardless of what happened to
    the reasoning text (spec Edge Cases, UP-005).
    """
    from civsim_web.viewmodels.decision import NO_REASONING_LABEL, REASONING_COLLAPSE_LIMIT
    from web_support.fixtures import make_client

    with make_client(web_store_factory(reasoning="")) as client:
        empty = client.get("/runs/run-1/turns/1", headers={"Accept": "application/json"}).json()
    decision = empty["steps"][0]["decision"]
    assert decision["reasoning_is_empty"] is True
    assert decision["reasoning_label"] == NO_REASONING_LABEL
    assert decision["action_label"]

    long_text = "why. " * (REASONING_COLLAPSE_LIMIT // 2)
    with make_client(web_store_factory(reasoning=long_text)) as client:
        body, markup = fetch_both(client, "/runs/run-1/turns/1")
    decision = body["steps"][0]["decision"]
    assert decision["reasoning_is_truncated"] is True
    assert len(decision["reasoning_preview"]) <= REASONING_COLLAPSE_LIMIT
    # The full text is still in the same response for both readers.
    assert decision["reasoning"].startswith("why.")
    assert "Show the full reasoning" in markup


def test_configuration_columns_render_unavailable_when_the_port_cannot_reach_them():
    """plan.md C1: never blank, never a plausible default -- named, with a reason.

    An empty civilization column that might mean "no civilization" is worse than
    one that says why it is missing (UP-005, FR-025's honest-state rule applied
    to a port gap).
    """
    from web_support.fixtures import make_client, make_store

    with make_client(make_store(with_configuration=False)) as client:
        body = client.get("/runs/run-1", headers={"Accept": "application/json"}).json()

    summary = body["summary"]
    assert summary["civilization"] is None
    reasons = {entry["field"]: entry["reason"] for entry in summary["unavailable"]}
    assert "RunConfiguration.civilization" in reasons
    assert "MatchStore port" in reasons["RunConfiguration.civilization"]


def test_intervention_info_is_present_and_carries_no_control(web_client):
    """FR-027 with UP-010: the facts to act elsewhere, and nothing to act with."""
    body, markup = fetch_both(web_client, "/runs/run-1")

    info = body["intervention_info"]
    assert info["run_id"] == "run-1"
    assert info["lifecycle_status"]
    assert info["last_known_good_turn"] is not None
    assert info["last_known_good_save_id"]

    # Nothing on this model could be wired to a mutating request (invariant V9).
    for key in info:
        assert "url" not in key and "action" not in key and "endpoint" not in key


CONTROL_FREE_PAGES = (
    "/",
    "/runs/run-1",
    "/runs/run-1/turns/2",
    "/runs/run-1/turns/2/steps/1",
    "/runs/run-1/events",
)


@pytest.mark.parametrize("path", CONTROL_FREE_PAGES)
def test_no_page_renders_a_control(web_client, path):
    """FR-026 / UP-010: the interface reports; it never acts.

    Asserted against the rendered markup rather than against the templates, so a
    control introduced by any route, macro, or model still fails here.
    """
    markup = web_client.get(path, headers={"Accept": "text/html"}).text.lower()
    for element in ("<form", "<button", "<input", "<textarea", "<select", 'method="post"'):
        assert element not in markup, f"{path} renders {element}"
