"""quickstart.md Scenarios 1-5, run as a script (T063).

``quickstart.md`` is how an operator stands this interface up and satisfies
themselves it does what the spec says. Every one of its Scenario 1-5 claims is
checkable against the ``MatchStore`` fake with no live harness and no game
client, so none of them has any business being a paragraph somebody re-reads by
hand per release.

This suite is deliberately **broad and shallow**, and that is the difference
between it and the rest of the tree. The contract and integration suites prove
each rule properly, with their fixtures and their edge cases; this one walks the
document top to bottom and asserts that what it promises a reader is still true.
A quickstart that has quietly drifted from the software is worse than no
quickstart, because someone follows it and concludes the software is broken.

Each test names the scenario and the requirements that scenario claims to
validate, so a failure points at the paragraph to fix as well as at the code.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from web_support.fixtures import make_catalog_store, make_client, make_store

JSON = {"Accept": "application/json"}
BROWSER = {"Accept": "text/html"}


# --------------------------------------------------------------------------
# Setup -- `civsim-web doctor`
# --------------------------------------------------------------------------


def test_setup_doctor_reports_all_green_in_the_documented_shape(capsys):
    """quickstart Setup: `uv run civsim-web doctor` must report all green.

    The document shows five labelled lines. `doctor` is the operator's first
    contact with this feature, so the shape it prints is part of the contract
    with them, not incidental formatting -- this test pins the shape (five
    labels, four of them reporting `ok`, none `FAILED`) and the one number in
    the coverage line that is this feature's own invariant: the last one, `0
    unregistered fields reachable from a view model`, which is the claim
    `doctor` exits non-zero on. It deliberately does NOT pin the coverage
    line's other four counts (fields scanned, marked out-of-game, registered,
    unregistered and unrendered) -- those track 002's `data-model.md` and move
    whenever that deliverable adds, removes, or reclassifies a field,
    independently of anything this feature does. Pinning a cross-deliverable
    count here would fail this suite on every 002 field addition, which is how
    a guard becomes something contributors edit to make green rather than a
    check that catches a regression (see quickstart.md and tasks.md T076).
    """
    from civsim_web.cli import main

    assert main(["--store", "fake", "--bind", "127.0.0.1", "doctor"]) == 0
    printed = capsys.readouterr().out

    for label in (
        "store             :",
        "panel registry    :",
        "registry coverage :",
        "routes            :",
        "bind address(es)  :",
    ):
        assert label in printed, f"doctor no longer prints the {label.strip(' :')!r} line"
    for ok_label in (
        "store             :",
        "panel registry    :",
        "registry coverage :",
        "routes            :",
    ):
        assert re.search(rf"^{re.escape(ok_label)} ok\b", printed, re.MULTILINE), (
            f"{ok_label.strip(' :')!r} line does not report ok"
        )
    assert "FAILED" not in printed
    # The four scanned/out-of-game/registered/unregistered counts are 002's,
    # not this feature's, and are deliberately left as \d+: only the coverage
    # line's last number -- this feature's own invariant -- is pinned to 0.
    assert re.search(
        r"registry coverage : ok \(\d+ fields scanned, \d+ marked out-of-game, "
        r"\d+ registered, \d+ unregistered and unrendered, "
        r"0 unregistered fields reachable from a view model\)",
        printed,
    ), "coverage line's final count must read '0 unregistered fields reachable from a view model'"
    # The freeze is the claim the same paragraph makes about rule P6.
    assert re.search(r"panel registry\s+: ok \(version \d+, \d+ panels, frozen\)", printed)


def test_setup_doctor_exits_non_zero_when_the_registry_cannot_load(tmp_path, capsys):
    """"A registry that fails to load ... aborts startup rather than serving."""
    from civsim_web.cli import main

    empty = tmp_path / "panels"
    empty.mkdir()

    assert main(["--panels", str(empty), "--bind", "127.0.0.1", "doctor"]) != 0
    assert "panel registry    : FAILED" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Scenario 1 -- watch a live run without opening the game
# --------------------------------------------------------------------------


@pytest.fixture
def live_client() -> Any:
    return make_client(make_store(turns=3, step_count=2))


def test_scenario_1_everything_the_glance_view_promises_is_on_one_page(live_client):
    """FR-001, FR-002, FR-031, FR-034 -- "all visible ... with no click required"."""
    body = live_client.get("/runs/run-1", headers=JSON).json()

    assert body["current_turn"]["turn_number"] == 3
    assert body["latest_decision"]["action_label"]
    assert body["latest_decision"]["reasoning"]
    assert body["summary"]["health"]["state"]
    assert body["last_confirmed_current_at"], "the 'last confirmed current at' indicator"
    # A capture beside the structured panels, for the current turn.
    assert body["current_turn"]["steps"][0]["capture"]["available"] is True
    # Intervention info: exactly run_id, lifecycle_status, last-known-good save.
    intervention = body["intervention_info"]
    assert intervention["run_id"] == "run-1"
    assert intervention["lifecycle_status"]
    assert intervention["last_known_good_save_id"]


def test_scenario_1_no_control_of_any_kind_is_rendered(live_client):
    """FR-026, FR-027 -- the page offers nothing to press.

    Asserted against the rendered HTML rather than the view model, because the
    failure this guards is a template acquiring a control the model never grew
    a field for.
    """
    html = live_client.get("/runs/run-1", headers=BROWSER).text.lower()
    for control in ("<form", "<button", "<input", "<select", "<textarea"):
        assert control not in html, f"a {control!r} appeared on the run view"
    assert 'method="post"' not in html


def test_scenario_1_a_withheld_capture_still_renders_the_panel(live_client):
    """"the panel still renders with the capture explicitly unavailable"."""
    client = make_client(make_store(turns=1, capture_status="withheld"))
    capture = client.get("/runs/run-1", headers=JSON).json()["current_turn"]["steps"][0][
        "capture"
    ]

    assert capture["available"] is False
    assert capture["unavailable_reason"], "an unavailable capture must say why (UP-005)"
    html = client.get("/runs/run-1", headers=BROWSER).text
    assert capture["unavailable_reason"] in html


def test_scenario_1_an_empty_state_is_explicit_never_blank(live_client):
    """spec Edge Cases -- "opened before the first turn was recorded"."""
    client = make_client(make_store(turns=0))
    body = client.get("/runs/run-1", headers=JSON).json()

    assert body["current_turn"] is None
    assert body["empty_state_reason"], "a blank panel is the thing UP-005 forbids"


# --------------------------------------------------------------------------
# Scenario 2 -- same view for the user and the directing session
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/runs/run-1",
        "/runs/run-1/turns/2",
        "/runs/run-1/turns/2/steps/1",
        "/runs/run-1/events",
    ],
)
def test_scenario_2_the_same_url_answers_both_readers(live_client, path):
    """FR-007, FR-008, SC-012 -- one URL, two serializations, one content."""
    as_json = live_client.get(path, headers=JSON)
    as_html = live_client.get(path, headers=BROWSER)

    assert as_json.status_code == as_html.status_code == 200
    assert as_json.headers["content-type"].startswith("application/json")
    assert as_html.headers["content-type"].startswith("text/html")
    # `?format=json` is the browser-tab override the same section documents.
    # `last_confirmed_current_at` is *this response's* wall clock -- FR-002's
    # "last confirmed current at" indicator -- so it differs between two
    # requests by construction and is excluded rather than frozen.
    override = live_client.get(f"{path}?format=json", headers=BROWSER)
    volatile = {"last_confirmed_current_at"}
    assert {k: v for k, v in override.json().items() if k not in volatile} == {
        k: v for k, v in as_json.json().items() if k not in volatile
    }


def test_scenario_2_a_superseded_attempt_explains_itself(live_client):
    """FR-009 -- "never the current authoritative turn silently substituted"."""
    client = make_client(make_store(turns=3, replayed_turns=[2]))
    body = client.get("/runs/run-1/turns/2?attempt=0", headers=JSON).json()

    assert body["attempt_index"] == 0
    assert body["is_authoritative"] is False
    assert body["superseded_by"] is not None

    authoritative = client.get("/runs/run-1/turns/2", headers=JSON).json()
    assert authoritative["is_authoritative"] is True
    assert authoritative["attempt_index"] == 1


def test_scenario_2_a_turn_that_never_existed_is_a_distinct_404(live_client):
    """The contract's error table: not-found and superseded are different answers."""
    missing = live_client.get("/runs/run-1/turns/99", headers=JSON)
    assert missing.status_code == 404
    assert "99" in missing.text


# --------------------------------------------------------------------------
# Scenario 3 -- replay and inspect a completed run turn by turn
# --------------------------------------------------------------------------


def test_scenario_3_a_gap_is_marked_and_the_run_is_unfit_for_trending():
    """FR-016, SC-010, SC-011, Principle III."""
    client = make_client(make_store(turns=5, gap_turns=[3], lifecycle_state="finished"))

    listing = client.get("/runs", headers=JSON).json()
    row = next(r for r in listing["runs"] if r["run_id"] == "run-1")
    assert row["trend_eligibility"]["eligible"] is False
    assert 3 in row["trend_eligibility"]["gapped_turns"]

    # The gapped turn is not silently skipped in the sequence: it is answerable
    # and says what it is.
    gapped = client.get("/runs/run-1/turns/3?attempt=0", headers=JSON)
    assert gapped.status_code in (200, 404)
    if gapped.status_code == 200:
        assert gapped.json()["completeness"]["is_gap"] is True


def test_scenario_3_the_abandoned_and_authoritative_attempts_both_appear():
    """FR-017 -- the crash/resume pair is visible, not collapsed to the winner."""
    client = make_client(make_store(turns=3, replayed_turns=[2]))

    abandoned = client.get("/runs/run-1/turns/2?attempt=0", headers=JSON).json()
    authoritative = client.get("/runs/run-1/turns/2", headers=JSON).json()
    assert abandoned["outcome"] == "abandoned"
    assert authoritative["outcome"] != "abandoned"


def test_scenario_3_stepping_between_turns_preserves_panel_focus():
    """spec Acceptance Scenario US3 §3, FR-015.

    Focus travels in the URL, so it survives a plain `<a href>` and the
    directing session resolving the identical reference sees the identical page.
    """
    client = make_client(make_store(turns=3, step_count=2))

    focused = client.get("/runs/run-1/turns/2?focus=observation.cities", headers=JSON)
    assert focused.status_code == 200
    assert focused.json()["focus_panel_id"] == "observation.cities"

    html = client.get(
        "/runs/run-1/turns/2?focus=observation.cities", headers=BROWSER
    ).text
    assert "focus=observation.cities" in html, "navigation must carry the focus onward"

    unknown = client.get("/runs/run-1/turns/2?focus=no.such.panel", headers=JSON)
    assert unknown.status_code == 400, "an unregistered focus is refused, not ignored"


def test_scenario_3_a_gapped_turn_is_a_break_in_the_trajectory():
    """data-model.md §10 -- omitted from `points`, and *visible* as omitted."""
    client = make_client(make_store(turns=6, gap_turns=[3], lifecycle_state="finished"))
    body = client.get("/runs/run-1/metrics", headers=JSON).json()

    for series in body["series"]:
        assert 3 not in [point["turn"] for point in series["points"]]


# --------------------------------------------------------------------------
# Scenario 4 -- compare runs and spot trends across them
# --------------------------------------------------------------------------


@pytest.fixture
def catalog_client() -> Any:
    return make_client(
        make_catalog_store(count=5, turns=8, completeness={"run-03": "incomplete"})
    )


def test_scenario_4_the_catalog_lists_every_fr_018_column(catalog_client):
    """FR-018, FR-019."""
    body = catalog_client.get("/runs", headers=JSON).json()
    assert body["total"] == 5

    row = body["runs"][0]
    for column in (
        "seed",
        "civilization",
        "ruleset",
        "model_primary",
        "turn_count",
        "outcome_metrics",
        "record_completeness_status",
        "started_at",
        "ended_at",
    ):
        assert column in row, f"FR-018 names {column} and the catalog row lacks it"

    # Filterable and sortable "by any RunSummaryView field", nested ones included.
    sorted_desc = catalog_client.get("/runs?sort=turn_count&order=desc", headers=JSON).json()
    counts = [r["turn_count"] for r in sorted_desc["runs"]]
    assert counts == sorted(counts, reverse=True)
    filtered = catalog_client.get("/runs?civilization=GREECE", headers=JSON).json()
    assert all(r["civilization"] == "GREECE" for r in filtered["runs"])


def test_scenario_4_the_incomplete_run_is_quarantined_not_plotted(catalog_client):
    """FR-021 + Principle III -- visibly quarantined, never plotted as comparable."""
    body = catalog_client.get(
        "/compare?runs=run-01,run-02,run-03,run-04,run-05"
        "&metrics=science_output,culture_output",
        headers=JSON,
    ).json()

    assert "run-03" in body["quarantined_run_ids"]
    assert "run-03" in [r["run_id"] for r in body["runs"]], "quarantined, not hidden"
    for series in body["series"].values():
        assert "run-03" not in [s["run_id"] for s in series]
    for point in body["divergence_points"]:
        assert "run-03" not in point["refs"]


def test_scenario_4_following_a_divergence_ref_opens_that_turn(catalog_client):
    """FR-022 -- "jump to this turn in each compared run", verified by following."""
    body = catalog_client.get(
        "/compare?runs=run-01,run-02,run-03,run-04,run-05&metrics=science_output",
        headers=JSON,
    ).json()
    assert body["divergence_points"], "the fixture is shaped to diverge"

    point = body["divergence_points"][0]
    for run_id, ref in point["refs"].items():
        response = catalog_client.get(ref, headers=JSON)
        assert response.status_code == 200
        assert response.json()["run_id"] == run_id
        assert response.json()["turn_number"] == point["turn"]


def test_scenario_4_the_comparison_answer_does_not_depend_on_captures():
    """FR-035, SC-016 -- identical output whether captures exist or are withheld."""
    query = "/compare?runs=run-01,run-02,run-03&metrics=science_output"
    clean = make_client(make_catalog_store(count=3, capture_status="screened_clean"))
    withheld = make_client(make_catalog_store(count=3, capture_status="withheld"))

    def comparable(payload: dict) -> dict:
        return {k: v for k, v in payload.items() if k != "provenance"}

    assert comparable(clean.get(query, headers=JSON).json()) == comparable(
        withheld.get(query, headers=JSON).json()
    )


# --------------------------------------------------------------------------
# Scenario 5 -- the parity and capture audits, made mechanical
# --------------------------------------------------------------------------


def test_scenario_5_every_in_game_panel_has_a_parity_basis():
    """SC-005, run in CI rather than by hand per release."""
    from civsim_web.registry.loader import default_panels_dir, load_panel_registry

    for panel in load_panel_registry(default_panels_dir()).declarations:
        if panel.category == "in_game":
            assert (panel.parity_basis or "").strip(), panel.panel_id
        else:
            assert panel.parity_basis is None, panel.panel_id


@pytest.mark.parametrize(
    ("screening_status", "expected"),
    [
        ("screened_clean", True),
        ("withheld", False),
        ("capture_failed", False),
        ("a_status_from_the_future", False),
    ],
)
def test_scenario_5_the_capture_rule_fails_closed(screening_status, expected):
    """SC-014 -- including for a status this feature does not recognise."""
    client = make_client(make_store(turns=1, capture_status=screening_status))
    capture = client.get("/runs/run-1", headers=JSON).json()["current_turn"]["steps"][0][
        "capture"
    ]
    assert capture["available"] is expected

    image = client.get(f"/captures/{capture['capture_id']}/image")
    assert (image.status_code == 200) is expected
    if not expected:
        assert capture["unavailable_reason"] in image.text


def test_scenario_5_a_missing_capture_record_is_also_unavailable():
    """The matrix's fourth case: no record at all, not a `screening_status`."""
    client = make_client(make_store(turns=1, with_captures=False))
    capture = client.get("/runs/run-1", headers=JSON).json()["current_turn"]["steps"][0][
        "capture"
    ]
    assert capture is None or capture["available"] is False


def test_scenario_5_no_harness_telemetry_is_declared_in_game():
    """FR-013 -- telemetry is out-of-game, never laundered into an in-game panel."""
    from civsim_web.registry.loader import default_panels_dir, load_panel_registry

    telemetry_entities = {"ModelCall", "ModelConfig", "SavePoint", "RunConfiguration"}
    for panel in load_panel_registry(default_panels_dir()).declarations:
        if panel.category != "in_game":
            continue
        for source in panel.parsed_source_fields:
            assert source.entity not in telemetry_entities, (
                f"panel {panel.panel_id} is in_game and reads {source}"
            )
