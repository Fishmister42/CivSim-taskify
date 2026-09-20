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
    # T039 (US2) -- the ViewReference resolution targets. One per reference
    # shape that carries a panel, including the step-scoped shape the contract's
    # route table omits and data-model.md SS12 describes (see routes/panels.py).
    Route("/runs/run-1/panels/run.intervention", "US2", "a run-scoped panel"),
    Route("/runs/run-1/panels/run.timeline", "US2", "a run-scoped panel over a list"),
    Route("/runs/run-1/turns/2/panels/current_turn.yields", "US2", "a turn-scoped panel"),
    Route("/runs/run-1/turns/2/panels/decision.model_call", "US2", "a telemetry panel"),
    Route("/runs/run-1/turns/2/panels/observation.congress", "US2", "a panel with nothing to show"),
    Route(
        "/runs/run-1/turns/2/steps/1/panels/observation.cities",
        "US2",
        "a step-scoped panel (data-model.md SS12's sixth shape)",
    ),
    Route(
        "/runs/run-1/turns/2?attempt=0",
        "US2",
        "a superseded attempt, explaining itself (FR-009)",
        store_kwargs={"replayed_turns": [2]},
    ),
    # T047 (US3) -- the single-run metric trajectory, and the step window.
    Route("/runs/run-1/metrics", "US3", "every metric series this run recorded"),
    Route("/runs/run-1/metrics?series=science_output", "US3", "narrowed by ?series="),
    Route(
        "/runs/run-1/metrics",
        "US3",
        "a run with a gapped turn -- the series omits it and says so",
        store_kwargs={"gap_turns": [2]},
    ),
    Route(
        "/runs/run-1/turns/2?step_offset=1&step_limit=1",
        "US3",
        "a step window, naming the indices it skipped",
        store_kwargs={"step_count": 3},
    ),
    Route(
        "/runs/run-1/turns/2?focus=observation.cities",
        "US3",
        "a focused panel, carried in the URL (FR-015)",
    ),
    # T057 (US4) -- the catalog listing and the comparison. `/compare` is
    # exercised here against a single run because the fixture `make_store` holds
    # one; the multi-run cases (quarantine, divergence, capture-independence)
    # are their own tests below, where the fixture can be shaped.
    Route("/runs", "US4", "the run catalog"),
    Route("/compare?runs=run-1", "US4", "a comparison, with its basis stated"),
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
    # US2's reference targets, added by T039 -- a panel page is a page too.
    "/runs/run-1/panels/run.intervention",
    "/runs/run-1/turns/2/panels/current_turn.yields",
    "/runs/run-1/turns/2/steps/1/panels/observation.cities",
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


# ==========================================================================
# US2 (T039) -- reference resolution, superseded attempts, and the secret scan
#
# `json_html_parity` above is case (a): the matrix now covers every route
# registered so far, panel routes included, so SC-004's audit grew rather than
# acquiring a second implementation.
# ==========================================================================

#: Fields whose value is *supposed* to change between two identical requests.
#: `last_confirmed_current_at` is FR-002's currency stamp -- it says when this
#: response was assembled, so two responses must differ there and nowhere else.
VOLATILE_PATHS = frozenset({"last_confirmed_current_at"})


def _stable(body: Any, prefix: str = "") -> dict[str, Any]:
    """A response's leaves, minus the deliberately volatile ones."""
    flat: dict[str, Any] = {}
    if isinstance(body, dict):
        for key, item in body.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key in VOLATILE_PATHS:
                continue
            flat.update(_stable(item, path))
        return flat
    if isinstance(body, list):
        for index, item in enumerate(body):
            flat.update(_stable(item, f"{prefix}.{index}" if prefix else str(index)))
        return flat
    return {prefix: body}


#: Every canonical reference shape in data-model.md SS12, as a concrete path
#: against the standard fixture. The sixth (step-scoped panel) is here because
#: `refs/reference.py` parses it and twenty shipped panels are `scope: step`.
REFERENCE_SHAPES = (
    "/runs/run-1",
    "/runs/run-1/turns/2",
    "/runs/run-1/turns/2/steps/1",
    "/runs/run-1/turns/2/panels/current_turn.yields",
    "/runs/run-1/turns/2/steps/1/panels/observation.cities",
    "/runs/run-1/panels/run.header",
)


@pytest.mark.parametrize("path", REFERENCE_SHAPES)
def test_ref_resolution_round_trips(path):
    """The reference *is* the URL (data-model.md SS12), so parsing is exact.

    There is no opaque token and no resolver endpoint to drift or expire; a
    reference the user copies out of the address bar parses into the same parts
    it serializes back from, which is the first half of SC-012.
    """
    from civsim_web.refs.reference import parse_view_reference

    assert parse_view_reference(path).path == path


@pytest.mark.parametrize("path", REFERENCE_SHAPES)
def test_ref_resolution_is_identical_for_both_readers(web_client, path):
    """SC-012 / FR-008: one reference, two readers, the same content.

    The user opens the reference in a browser; the directing session `GET`s the
    identical string. This asserts the two receive the same *content* -- every
    field the JSON body carries is cited by the page and vice versa -- and that
    a second identical retrieval is stable except for FR-002's currency stamp.
    Because both are served by one view-model constructor and differ only at the
    final serialization (invariant V8), a failure here means the seam broke, not
    that a percentage slipped.
    """
    as_session = web_client.get(path, headers={"Accept": "application/json"})
    as_user = web_client.get(path, headers={"Accept": "text/html"})
    again = web_client.get(path, headers={"Accept": "application/json"})

    assert as_session.status_code == as_user.status_code == 200, path
    assert json_leaves(as_session.json()) == html_fields(as_user.text), path
    assert _stable(as_session.json()) == _stable(again.json()), path


def test_a_panel_shows_exactly_what_its_page_shows(web_client):
    """FR-007 at panel grain: the panel endpoint is a slice, not a second read.

    Every value the panel response carries names its JSON path *in the enclosing
    view*, and that path must hold that same value on the enclosing view's own
    response. A panel that could show something its page does not -- or a page
    that could show something its panel omits -- is the asymmetry Principle VI
    forbids, and this is what would fail if one appeared.
    """
    turn = web_client.get(
        "/runs/run-1/turns/2", headers={"Accept": "application/json"}
    ).json()

    for panel_id in ("current_turn.yields", "turn.record", "decision.model_call"):
        panel = web_client.get(
            f"/runs/run-1/turns/2/panels/{panel_id}", headers={"Accept": "application/json"}
        ).json()
        assert panel["is_present"], panel_id
        for value in panel["values"]:
            cursor: Any = turn
            for part in value["path"].split("."):
                cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
            assert cursor == value["value"], (
                f"{panel_id} disagrees with its page at {value['path']}"
            )


def test_every_registered_panel_resolves_somewhere(web_client):
    """UP-006: a panel a user can be shown is a panel they can hand over.

    Every declaration in the shipped registry must resolve through the reference
    shape matching its own scope -- with content or with a stated
    `absent_reason`, but never with a 404, which the contract reserves for an
    *unregistered* id.
    """
    from civsim_web.registry.loader import default_panels_dir, load_panel_registry

    registry = load_panel_registry(default_panels_dir())
    paths = {
        "run": "/runs/run-1/panels/{panel_id}",
        "turn": "/runs/run-1/turns/2/panels/{panel_id}",
        "step": "/runs/run-1/turns/2/steps/1/panels/{panel_id}",
    }
    for declaration in registry.declarations:
        path = paths[declaration.scope].format(panel_id=declaration.panel_id)
        body = web_client.get(path, headers={"Accept": "application/json"}).json()
        assert body["panel_id"] == declaration.panel_id, path
        assert body["panel_basis"].strip(), path
        assert body["is_present"] or body["absent_reason"], path


def test_an_unregistered_panel_id_is_a_reference_error(web_client):
    """contracts/web-read-api.md's error table, verbatim in its distinction.

    "`panel_id` not in the Panel Registry -> 404 -- this is a client-side
    reference error, not a 'field unavailable' case, since an unregistered panel
    is not a valid reference at all." A registered panel with nothing to show is
    the other case and must *not* 404.
    """
    missing = web_client.get(
        "/runs/run-1/panels/debug.hidden_ai_intent", headers={"Accept": "application/json"}
    )
    assert missing.status_code == 404
    assert missing.json()["kind"] == "panel_not_found"

    empty = web_client.get(
        "/runs/run-1/turns/2/panels/observation.congress",
        headers={"Accept": "application/json"},
    )
    assert empty.status_code == 200
    assert empty.json()["is_present"] is False
    assert empty.json()["absent_reason"]


# -- superseded attempts (T036, FR-009) ------------------------------------


def test_superseded_attempt_explains_itself(web_store_factory):
    """FR-009: the named attempt, never the authoritative one substituted.

    Turn 2 was attempted, abandoned, and replayed. A reference someone shared
    five minutes ago names attempt 0. It must still return attempt 0's record,
    flagged non-authoritative and pointing at what overtook it -- "rather than
    404 or the current authoritative turn silently substituted".
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory(replayed_turns=[2])) as client:
        bare = client.get("/runs/run-1/turns/2", headers={"Accept": "application/json"}).json()
        named = client.get(
            "/runs/run-1/turns/2?attempt=0", headers={"Accept": "application/json"}
        ).json()

    assert bare["attempt_index"] == 1
    assert bare["is_authoritative"] is True
    assert bare["superseded_by"] is None

    assert named["attempt_index"] == 0
    assert named["is_authoritative"] is False
    assert named["superseded_by"] == 1
    assert named["outcome"] == "abandoned"
    # The two are genuinely different records, not the same one relabelled.
    assert named["steps"][0]["reference"] != "" and named != bare


def test_superseded_attempt_is_distinct_from_things_that_never_existed(web_store_factory):
    """Four outcomes, four `kind`s -- the distinction FR-009 exists to preserve.

    A reference that explains itself, a turn outside the range, a run that never
    was, and an attempt nobody recorded must not look alike to a client.
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory(replayed_turns=[2])) as client:
        superseded = client.get(
            "/runs/run-1/turns/2?attempt=0", headers={"Accept": "application/json"}
        )
        no_turn = client.get("/runs/run-1/turns/77", headers={"Accept": "application/json"})
        no_run = client.get("/runs/nope/turns/1", headers={"Accept": "application/json"})
        no_attempt = client.get(
            "/runs/run-1/turns/2?attempt=9", headers={"Accept": "application/json"}
        )

    assert superseded.status_code == 200
    assert no_turn.status_code == 404 and no_turn.json()["kind"] == "turn_not_found"
    assert no_run.status_code == 404 and no_run.json()["kind"] == "run_not_found"
    assert no_attempt.status_code == 404 and no_attempt.json()["kind"] == "attempt_not_found"
    # And the attempt error points at the authoritative one rather than serving it.
    assert no_attempt.json()["detail"]["authoritative_reference"] == "/runs/run-1/turns/2"


def test_an_unaddressable_attempt_names_the_port_gap_rather_than_substituting(web_store_factory):
    """plan.md C1's fourth instance, answered honestly.

    The published `MatchStore` port selects an attempt with a boolean, not an
    index, so a store without the optional `TurnAttemptReader` capability can
    reach only the authoritative and the newest attempt. Asking for any other
    must say *that* -- never the authoritative turn wearing someone else's
    reference.
    """
    from web_support.fixtures import make_client

    store = web_store_factory(replayed_turns=[2], attempt_reader=False)
    with make_client(store) as client:
        response = client.get(
            "/runs/run-1/turns/2?attempt=0", headers={"Accept": "application/json"}
        )

    body = response.json()
    assert response.status_code == 404
    assert body["kind"] == "attempt_not_addressable"
    assert body["detail"]["addressable_attempts"] == [1]
    assert "MatchStore port" in body["message"]
    assert body["detail"]["authoritative_reference"] == "/runs/run-1/turns/2"


def test_a_turn_whose_every_attempt_was_abandoned_still_explains_itself(web_store_factory):
    """Spec Edge Cases: "a branch was abandoned or rolled back".

    Turn 2's only attempt was abandoned, so the turn is a recorded gap. The bare
    reference answers with the gap (FR-016); the reference that *names* the
    attempt answers with the attempt, marked as the gap it belongs to, because
    FR-009 wants an explanation rather than a dead link and V4 only forbids
    rendering it as a *normal, complete* turn.
    """
    from web_support.fixtures import make_client, make_turn_cycle

    store = web_store_factory(turns=1, turn_gaps={"run-1": [2]})
    store.seed_turn_cycle(
        make_turn_cycle(2, attempt_index=0, is_authoritative=False, outcome="abandoned")
    )

    with make_client(store) as client:
        bare = client.get("/runs/run-1/turns/2", headers={"Accept": "application/json"})
        named = client.get(
            "/runs/run-1/turns/2?attempt=0", headers={"Accept": "application/json"}
        )

    assert bare.status_code == 404 and bare.json()["kind"] == "turn_gap"

    body = named.json()
    assert named.status_code == 200
    assert body["attempt_index"] == 0
    assert body["is_authoritative"] is False
    assert body["superseded_by"] is None, "nothing superseded it -- it was simply abandoned"
    assert body["completeness"]["is_gap"] is True
    assert body["completeness"]["is_complete"] is False


# -- no_secrets (FR-030) ---------------------------------------------------

#: Field names that would carry a credential if one ever reached a response.
#: Matched against every key of every response body *and* every declared field
#: of every view model, so a field that is merely unpopulated in the fixture
#: still fails the scan.
#:
#: A bare `token` is deliberately **not** matched: FR-005 requires per-turn
#: model cost to be visible, and provider usage arrives as `input_tokens` /
#: `output_tokens`. A pattern that flagged those would be switched off inside a
#: week, which is worse than one that names the credential-bearing forms.
_CREDENTIAL_NAMES = re.compile(
    r"(api[_-]?key|secret|password|passwd|credential|authorization|auth[_-]?header"
    r"|private[_-]?key|access[_-]?key|client[_-]?secret|bearer"
    r"|(?:access|auth|refresh|id|api|bearer|session)[_-]tokens?"
    r"|session[_-]?id|cookie)",
    re.IGNORECASE,
)

#: Value shapes common to the credentials this project could plausibly hold --
#: OpenRouter/Anthropic-style keys and HTTP authorization headers (Principle
#: VII routes model calls through a provider layer, so these are the shapes).
_CREDENTIAL_VALUES = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|Basic\s+[A-Za-z0-9+/=]{8,}|xoxb-|ghp_[A-Za-z0-9]{8,})"
)


def test_the_credential_scan_can_actually_fail():
    """A scanner that cannot fail audits nothing.

    Both halves of the scan are exercised against a body that genuinely carries
    a secret, so a future narrowing of either pattern (to quiet a false
    positive, say) cannot silently turn the FR-030 audit into a no-op. The
    counter-examples are the values the same narrowing must keep passing.
    """
    caught = _credential_named_keys(
        {
            "summary": {"api_key": "x"},
            "events": [{"detail": {"authorization": "y", "access_token": "z"}}],
            "note": "Bearer abcdefghijkl",
        }
    )
    assert "summary.api_key" in caught
    assert "events.0.detail.authorization" in caught
    assert "events.0.detail.access_token" in caught
    assert "note (value shape)" in caught

    # And the things it must not flag: FR-005's cost fields, and ordinary prose.
    assert not _credential_named_keys(
        {"cost": {"input_tokens": 900, "output_tokens": 120}, "reasoning": "Settled the capital."}
    )


def _credential_named_keys(body: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(body, dict):
        for key, item in body.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _CREDENTIAL_NAMES.search(str(key)):
                found.append(path)
            found.extend(_credential_named_keys(item, path))
    elif isinstance(body, list):
        for index, item in enumerate(body):
            path = f"{prefix}.{index}" if prefix else str(index)
            found.extend(_credential_named_keys(item, path))
    elif isinstance(body, str) and _CREDENTIAL_VALUES.search(body):
        found.append(f"{prefix} (value shape)")
    return found


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.id)
def test_no_secrets_in_any_response(web_store_factory, route):
    """FR-030: no credential reaches a LAN-visible, unauthenticated page.

    Any device on the local network can read this interface with no login, so a
    secret that reached a response would be published rather than merely
    logged. 002 redacts its own `RunEvent.detail` (its FR-043) and this feature
    narrows `ModelConfigLike` to identity fields; this is the audit that the two
    together actually hold at this boundary, run on every build rather than by
    hand per release.
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory(**route.store_kwargs)) as client:
        body = client.get(route.path, headers={"Accept": "application/json"}).json()

    assert not _credential_named_keys(body), route.path


def test_no_view_model_declares_a_credential_shaped_field():
    """The same scan against the *schemas*, not only the sampled bodies.

    A fixture that happens not to populate a field proves nothing about the
    field. This walks every view model reachable from a registered route's
    response and fails on a credential-shaped name whether or not any fixture
    fills it.
    """
    import importlib
    import pkgutil

    from pydantic import BaseModel

    import civsim_web.viewmodels as viewmodels
    from civsim_web.routes.common import ErrorView
    from civsim_web.viewmodels.base import ViewModel

    offenders: list[str] = []
    seen: set[type] = {ErrorView}
    for module in pkgutil.iter_modules(viewmodels.__path__):
        loaded = importlib.import_module(f"civsim_web.viewmodels.{module.name}")
        for attribute in vars(loaded).values():
            if isinstance(attribute, type) and issubclass(attribute, BaseModel):
                seen.add(attribute)

    checked = [model for model in seen if issubclass(model, ViewModel | ErrorView)]
    assert len(checked) >= 10, "the scan found almost no view models -- it is not scanning"
    for model in checked:
        for name in model.model_fields:
            if _CREDENTIAL_NAMES.search(name):
                offenders.append(f"{model.__name__}.{name}")
    assert not offenders, offenders


# ==========================================================================
# US3 (T047) -- `metric_series_gaps`: a gapped turn is never a value, and the
#               line that draws it never joins across it
# ==========================================================================


def _metrics(client, path="/runs/run-1/metrics"):
    response = client.get(path, headers={"Accept": "application/json"})
    assert response.status_code == 200, response.text
    return response.json()


def test_metric_series_never_carries_a_gapped_turn_as_a_value(web_store_factory):
    """T047, verbatim: "`points` never includes a turn present in that run's
    `turn_gaps()` as if it were a real value" (data-model.md SS10).

    The store is seeded so turn 2 has no attempt at all, which is what makes the
    fake's own `turn_gaps()` report it -- the gap is computed from the records,
    not asserted over them, so this test cannot pass by the fixture lying.
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory(turns=5, gap_turns=[2])) as client:
        body = _metrics(client)

    assert body["gapped_turns"] == [2]
    assert body["series"], "the run recorded metrics; the series should not be empty"
    for series in body["series"]:
        turns = [point["turn"] for point in series["points"]]
        assert 2 not in turns, (
            f"series {series['metric_name']!r} carries turn 2 as a real value while "
            f"the store records it as a gap"
        )
        assert series["gapped_turns"] == [2], (
            "the gap is omitted from `points` *and* named on the series -- omitting "
            "it silently would leave a hole nobody could account for"
        )


def test_a_gapped_turn_renders_as_a_break_in_the_line_not_a_join(web_store_factory):
    """The rendering half of the same rule -- and the one a regression hides in.

    data-model.md SS10 permits a gapped turn to be omitted from `points` *only*
    if the gap is visible on the chart, "e.g. a break in the line". The server
    already omits it, so a renderer that joins `points` end to end draws a
    straight line **across** the gap: the chart then says the run progressed
    smoothly through a turn nobody recorded, which is the interpolation the same
    paragraph forbids and the exact hole Principle III's quarantine exists to
    surface.

    So: the page must draw more than one `<polyline>` for a series whose turns
    are not contiguous, and must mark where the break falls. A regression to a
    single joined polyline fails here.
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory(turns=5, gap_turns=[3])) as client:
        body = _metrics(client, "/runs/run-1/metrics?series=science_output")
        markup = client.get(
            "/runs/run-1/metrics?series=science_output", headers={"Accept": "text/html"}
        ).text

    points = body["series"][0]["points"]
    assert [p["turn"] for p in points] == [1, 2, 4, 5]

    polylines = re.findall(r'<polyline class="series"[^>]*>', markup)
    assert len(polylines) == 2, (
        f"a series broken by a gap must be drawn as one polyline per unbroken run "
        f"of turns; found {len(polylines)}. One polyline through all four points "
        f"joins straight across turn 3."
    )
    assert 'data-break-turn="3"' in markup, (
        "the break is drawn, not merely implied by the absence of a line"
    )

    # And no single polyline carries points from both sides of the gap.
    for polyline in polylines:
        coordinates = re.search(r'points="([^"]*)"', polyline)
        assert coordinates is not None
        assert len(coordinates.group(1).split()) == 2


def test_a_gapped_run_is_flagged_unfit_for_trend_comparison_on_its_own_chart(
    web_store_factory,
):
    """FR-016's second clause, where a reader is most likely to need it.

    A trajectory is the most trendable-looking thing this feature draws, so the
    page that draws one has to say whether it may be trended at all. The run is
    still shown -- FR-016 requires marking, not hiding.
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory(turns=5, gap_turns=[2])) as client:
        body = _metrics(client)
        markup = client.get("/runs/run-1/metrics", headers={"Accept": "text/html"}).text

    assert body["trend_eligibility"]["eligible"] is False
    assert body["trend_eligibility"]["assessed"] is True
    assert body["series"], "a quarantined run still gets its own chart"
    assert "unfit" in markup.lower() or "trending" in markup.lower()


def test_a_step_window_names_every_index_it_skipped(web_store_factory):
    """T043 / data-model.md SS5: "no page may skip an index without marking it".

    A page boundary and a record gap look identical to a reader who is only
    shown what is on the page, and SS5 spends its whole lazy-loading clause on
    those two not being confusable.
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory(turns=3, step_count=4)) as client:
        response = client.get(
            "/runs/run-1/turns/2?step_offset=1&step_limit=2",
            headers={"Accept": "application/json"},
        )
        past_end = client.get(
            "/runs/run-1/turns/2?step_offset=9", headers={"Accept": "application/json"}
        )

    body = response.json()
    assert [step["step_index"] for step in body["steps"]] == [2, 3]
    assert body["step_window"]["skipped_step_indices"] == [1, 4]
    assert body["step_window"]["total"] == 4
    assert body["step_window"]["has_more"] is True

    assert past_end.status_code == 404
    assert past_end.json()["kind"] == "step_window_out_of_range"


def test_panel_focus_travels_in_the_url_and_is_visible_to_both_readers(
    web_store_factory,
):
    """FR-015 / Principle VI: focus is in the reference, so both readers see it.

    A focus the browser rendered and the JSON did not mention would be a page
    the directing session cannot reproduce from the same URL -- the asymmetry
    Principle VI forbids, in miniature.
    """
    from web_support.fixtures import make_client

    path = "/runs/run-1/turns/2?focus=observation.cities"
    with make_client(web_store_factory()) as client:
        body, markup = fetch_both(client, path)
        unknown = client.get(
            "/runs/run-1/turns/2?focus=not.a.panel", headers={"Accept": "application/json"}
        )

    assert body["focus_panel_id"] == "observation.cities"
    assert 'class="observation-entry focused"' in markup
    # Stepping to the next turn keeps it.
    assert "/runs/run-1/turns/3?focus=observation.cities" in markup

    assert unknown.status_code == 400
    assert unknown.json()["kind"] == "unknown_focus_panel"


# ==========================================================================
# US4 (T057) -- comparison never needs captures, and the Principle III
#               quarantine actually excludes what it claims to
# ==========================================================================


def _comparison(client, path="/compare?runs=run-01,run-02,run-03&metrics=science_output"):
    response = client.get(path, headers={"Accept": "application/json"})
    assert response.status_code == 200, response.text
    return response.json()


def test_comparison_never_needs_captures():
    """FR-035 / SC-016: identical output, every capture clean or every one withheld.

    The plan states this test in its Constraints section: *"enforced by a test
    that removes all capture data from the fake store and re-runs the comparison
    assertions."* Two stores differing in nothing but `screening_status` must
    produce identical comparison bodies -- if a capture could influence a
    comparison at all, these would differ somewhere.

    Run three times rather than twice, because the three ways a capture can be
    unusable are not the same code path: screened clean, withheld, and no
    capture record at all.
    """
    from web_support.fixtures import make_catalog_store, make_client

    bodies = []
    for kwargs in (
        {"capture_status": "screened_clean"},
        {"capture_status": "withheld"},
        {"with_captures": False},
    ):
        store = make_catalog_store(count=3, turns=6, **kwargs)
        with make_client(store) as client:
            bodies.append(_comparison(client))

    assert bodies[0] == bodies[1] == bodies[2]


def test_no_comparison_response_mentions_a_capture():
    """Invariant V7, at the response boundary rather than at the type.

    contracts/web-read-api.md: *"no `ComparisonView` response contains an image
    byte or capture reference anywhere in its JSON."*

    `panel_registry.panel_ids` is excluded from the scan and nothing else is.
    That field is the registry *manifest* invariant V10 requires on every
    top-level response -- the full set of panel ids in force, `capture.screen`
    among them -- and it says which declarations exist, not which data this
    response carries. Excluding the manifest keeps the scan honest; excluding
    anything else would make it decorative.
    """
    from web_support.fixtures import make_catalog_store, make_client

    with make_client(make_catalog_store(count=3, turns=6)) as client:
        body = _comparison(client)
        markup = client.get(
            "/compare?runs=run-01,run-02,run-03", headers={"Accept": "text/html"}
        ).text

    manifest = body["panel_registry"].pop("panel_ids")
    assert "capture.screen" in manifest, "the exclusion is excluding nothing"

    serialized = str(body).lower()
    for forbidden in ("capture", "screening_status", "image_url", "blob"):
        assert forbidden not in serialized, forbidden

    lowered = markup.lower()
    assert "<img" not in lowered, "the comparison page loads an image"
    for forbidden in ("screening_status", "image_url", "/captures/", "blob"):
        assert forbidden not in lowered, f"{forbidden} in the rendered page"


def test_quarantine_keeps_an_incomplete_run_visible_and_out_of_the_trend():
    """data-model.md SS11, verbatim, in both directions.

    *"A run in `quarantined_run_ids` still appears in `runs` (so the user can
    see why it is excluded) but is excluded from `series` and
    `divergence_points` computation"* for any run whose
    `record_completeness_status != complete`.

    Both halves are asserted, because a future edit is far likelier to get one
    right than both: hiding the run loses the explanation, and including it in
    the series launders incomplete data into every conclusion drawn from the
    chart (Principle III).
    """
    from web_support.fixtures import make_catalog_store, make_client

    store = make_catalog_store(
        count=3,
        turns=6,
        completeness={"run-02": "has_gaps"},
        turn_gaps={"run-02": [3]},
    )
    with make_client(store) as client:
        body = _comparison(client)

    assert body["quarantined_run_ids"] == ["run-02"]
    assert "run-02" in [run["run_id"] for run in body["runs"]], "quarantine hid the run"

    quarantined_summary = next(r for r in body["runs"] if r["run_id"] == "run-02")
    assert quarantined_summary["trend_eligibility"]["eligible"] is False
    assert quarantined_summary["trend_eligibility"]["explanation"], "no reason given"

    for metric, series_list in body["series"].items():
        assert "run-02" not in [s["run_id"] for s in series_list], metric
    for point in body["divergence_points"]:
        assert point["leader_run_id"] != "run-02"
        assert "run-02" not in point["refs"]
    for metric, axis in body["axes"].items():
        assert "run-02" not in axis["run_ids"], metric


def test_a_run_the_store_calls_complete_while_listing_gaps_is_still_quarantined():
    """Principle III's own words, where they are stronger than FR-021's.

    FR-021 quarantines on `record_completeness_status`. The constitution
    quarantines on the *record having gaps*: *"A run's results MUST NOT be used
    for trending, datamining, or optimization input if its turn-by-turn record
    has gaps."* `MatchStore.turn_gaps()` is a separately published read of
    exactly that, and a store that answers the two questions differently is a
    store this feature must not average.

    Neither status is re-derived here (invariant V5) -- both are read verbatim,
    and disagreement fails closed.
    """
    from web_support.fixtures import make_catalog_store, make_client

    store = make_catalog_store(
        count=2,
        turns=6,
        completeness={"run-02": "complete"},  # the store says complete ...
        turn_gaps={"run-02": [4]},  # ... while listing turn 4 as missing
    )
    with make_client(store) as client:
        body = _comparison(client, "/compare?runs=run-01,run-02&metrics=science_output")

    assert body["quarantined_run_ids"] == ["run-02"]
    summary = next(r for r in body["runs"] if r["run_id"] == "run-02")
    assert summary["record_completeness_status"] == "complete"
    assert summary["trend_eligibility"]["eligible"] is False
    assert "turn_gaps_recorded" in summary["trend_eligibility"]["reasons"]
    assert summary["trend_eligibility"]["gapped_turns"] == [4]
    assert "run-02" not in [s["run_id"] for s in body["series"]["science_output"]]


def test_the_comparison_model_refuses_to_carry_a_quarantined_run():
    """The guard is structural, not a filter someone could quietly delete.

    `build_comparison_view` excludes quarantined runs; `ComparisonView`'s own
    validator then re-checks the outcome. This test constructs the model
    directly with the filter's effect undone -- which is what a future edit
    removing the filter would produce -- and asserts construction fails.

    Without this, deleting one comprehension in `build_comparison_view` would
    silently start averaging gapped runs and every other test here would still
    pass, because they all go through the very function that was edited.
    """
    from civsim_web.viewmodels.base import (
        HealthState,
        HealthStatus,
        PanelRegistryVersion,
        RunSummaryView,
        TrendEligibility,
        derive_trend_eligibility,
    )
    from civsim_web.viewmodels.comparison import ComparisonBasis, ComparisonView
    from civsim_web.viewmodels.metrics import MetricPoint, MetricSeriesView
    from civsim_web.viewmodels.provenance import Provenance

    def summary(run_id: str, eligibility: TrendEligibility) -> RunSummaryView:
        return RunSummaryView(
            run_id=run_id,
            lifecycle_state="finished",
            health=HealthStatus(state=HealthState.FINISHED, lifecycle_state="finished"),
            record_completeness_status="has_gaps",
            comparability_status="comparable",
            trend_eligibility=eligibility,
        )

    gapped = derive_trend_eligibility(
        record_completeness_status="has_gaps", gapped_turns=[3]
    )
    provenance = Provenance(panel_registry_version="1", panel_registry_content_hash="x")
    panel_registry = PanelRegistryVersion(version="1", content_hash="x")

    with pytest.raises(ValueError, match="Principle III"):
        ComparisonView(
            runs=(summary("run-bad", gapped),),
            quarantined_run_ids=("run-bad",),
            basis=ComparisonBasis(),
            provenance=provenance,
            panel_registry=panel_registry,
            series={
                "science_output": [
                    MetricSeriesView(
                        run_id="run-bad",
                        metric_name="science_output",
                        points=(MetricPoint(turn=1, value=1.0),),
                    )
                ]
            },
        )

    # ... and hiding the run instead of quarantining it is refused too.
    with pytest.raises(ValueError, match="marking, not hiding"):
        ComparisonView(
            runs=(),
            quarantined_run_ids=("run-bad",),
            basis=ComparisonBasis(),
            provenance=provenance,
            panel_registry=panel_registry,
        )


def test_a_metric_series_never_carries_a_gapped_turn_as_a_value():
    """data-model.md SS10, verbatim, enforced at construction.

    *"`points` never includes a turn present in that run's `turn_gaps()` as if
    it were a real value."* The gapped turn is omitted and named, never
    interpolated or zero-filled (FR-025, UP-005).
    """
    from civsim_web.viewmodels.metrics import (
        MetricPoint,
        MetricSeriesView,
        build_metric_series,
    )

    series = build_metric_series(
        "run-1",
        "science_output",
        yields_by_turn={1: {"science_output": 3.0}, 3: {"science_output": 9.0}},
        gapped_turns=[2],
    )
    assert [point.turn for point in series.points] == [1, 3]
    assert series.gapped_turns == (2,)

    with pytest.raises(ValueError, match="gaps"):
        MetricSeriesView(
            run_id="run-1",
            metric_name="science_output",
            points=(MetricPoint(turn=2, value=6.0),),
            gapped_turns=(2,),
        )


def test_the_catalog_lists_every_run_and_says_which_may_be_trended():
    """FR-018 with FR-016: an incomplete run is marked, never hidden."""
    from web_support.fixtures import make_catalog_store, make_client

    store = make_catalog_store(count=4, turns=5, completeness={"run-02": "has_gaps"})
    with make_client(store) as client:
        body = client.get("/runs", headers={"Accept": "application/json"}).json()

    assert [run["run_id"] for run in body["runs"]] == ["run-04", "run-03", "run-02", "run-01"]
    assert body["total"] == 4
    assert body["trend_eligible_count"] == 3
    assert body["quarantined_run_ids"] == ["run-02"]
    assert body["listing_is_partial"] is False


def test_the_catalog_says_so_when_the_port_can_only_reach_active_runs():
    """plan.md C1: a partial listing says it is partial.

    A catalog that silently shows only active runs looks exactly like a catalog
    of a store that has only ever had active runs. That is the one failure mode
    here capable of quietly truncating the population a trend is drawn from,
    and it is the state every store that implements only the *published* port
    will be in until deliverable 3 publishes a catalog-listing read.
    """
    from web_support.fixtures import make_catalog_store, make_client, published_port_only

    store = published_port_only(
        make_catalog_store(count=3, turns=3, lifecycle_states=["playing"])
    )
    with make_client(store) as client:
        body = client.get("/runs", headers={"Accept": "application/json"}).json()

    assert body["listing_is_partial"] is True
    assert "no historical run listing" in body["partial_reason"]
    # The published read still answers, so the page is a real (partial) catalog
    # rather than an error -- the runs it *can* see are all active.
    assert [run["run_id"] for run in body["runs"]]


def test_an_unrecognised_filter_field_is_refused_rather_than_ignored():
    """FR-019: a filter that quietly does nothing shows an unfiltered catalog."""
    from web_support.fixtures import make_catalog_store, make_client

    with make_client(make_catalog_store(count=3, turns=3)) as client:
        response = client.get(
            "/runs?civilisation=GREECE", headers={"Accept": "application/json"}
        )

    assert response.status_code == 400
    body = response.json()
    assert body["kind"] == "unknown_filter_field"
    assert "civilization" in body["detail"]["known_fields"]


def test_a_comparison_of_runs_with_different_starting_conditions_says_so():
    """Principle IV, on the page rather than in a reviewer's head.

    *"Optimization, branching, backtracking, and ablation work MUST run against
    a fixed set of initial seeds under a consistent civilization and ruleset."*
    Nothing stops a user selecting runs that share none of those; what this
    interface must not do is draw them on one axis without saying so.
    """
    from web_support.fixtures import make_catalog_store, make_client

    mixed = make_catalog_store(
        count=2, turns=4, seeds=["SEED-0001", "SEED-0002"], civilizations=["GREECE", "ROME"]
    )
    with make_client(mixed) as client:
        body = _comparison(client, "/compare?runs=run-01,run-02")
        markup = client.get("/compare?runs=run-01,run-02", headers={"Accept": "text/html"}).text

    assert body["basis"]["is_uniform"] is False
    assert set(body["basis"]["divergent_dimensions"]) >= {"seed", "civilization"}
    assert "Principle IV" in body["basis"]["note"]
    assert "Principle IV" in markup

    uniform = make_catalog_store(
        count=2,
        turns=4,
        seeds=["SEED-0001"],
        civilizations=["GREECE"],
        leaders=["PERICLES"],
        models=["anthropic/claude-sonnet-4"],
    )
    with make_client(uniform) as client:
        body = _comparison(client, "/compare?runs=run-01,run-02")
    assert body["basis"]["is_uniform"] is True
    assert body["basis"]["divergent_dimensions"] == []


def test_a_comparison_basis_the_port_cannot_verify_is_not_reported_as_uniform():
    """plan.md C1 again: unverifiable is not uniform-by-default."""
    from web_support.fixtures import make_catalog_store, make_client

    store = make_catalog_store(count=2, turns=3, with_configuration=False)
    with make_client(store) as client:
        body = _comparison(client, "/compare?runs=run-01,run-02")

    assert body["basis"]["is_uniform"] is False
    assert set(body["basis"]["unverifiable_dimensions"]) >= {"seed", "civilization", "ruleset"}
    assert "could not be verified" in body["basis"]["note"]


def test_comparing_a_run_that_never_existed_is_a_404():
    """contracts/web-read-api.md's error table, on this route too."""
    from web_support.fixtures import make_catalog_store, make_client

    with make_client(make_catalog_store(count=2, turns=3)) as client:
        response = client.get(
            "/compare?runs=run-01,run-99", headers={"Accept": "application/json"}
        )

    assert response.status_code == 404
    assert response.json()["kind"] == "run_not_found"


def test_compare_with_nothing_selected_explains_itself():
    """An empty selection is an explanatory 200, not an error and not a guess."""
    from web_support.fixtures import make_catalog_store, make_client

    with make_client(make_catalog_store(count=2, turns=3)) as client:
        response = client.get("/compare", headers={"Accept": "application/json"})

    assert response.status_code == 200
    assert response.json()["empty_state_reason"]
    assert response.json()["series"] == {}


US4_CONTROL_FREE_PAGES = ("/runs", "/compare?runs=run-01,run-02")


@pytest.mark.parametrize("path", US4_CONTROL_FREE_PAGES)
def test_no_us4_page_renders_a_control(path):
    """FR-026 / UP-010 on the two pages US4 adds.

    Filtering and sorting are links that re-issue a `GET`, not a form. A page
    whose only interactive elements are anchors cannot be wired to a mutating
    request by a later edit that adds a handler to one.
    """
    from web_support.fixtures import make_catalog_store, make_client

    with make_client(make_catalog_store(count=2, turns=3)) as client:
        markup = client.get(path, headers={"Accept": "text/html"}).text.lower()

    for element in ("<form", "<button", "<input", "<textarea", "<select", 'method="post"'):
        assert element not in markup, f"{path} renders {element}"
