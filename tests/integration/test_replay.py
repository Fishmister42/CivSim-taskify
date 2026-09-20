"""Turn-by-turn replay of a completed run (T046, US3).

The fixture is one completed run carrying, deliberately together, the three
things a replay has to survive:

- **a turn-number gap** (turn 3 was never written at all) and **a turn whose
  every attempt was abandoned** (turn 7). Both are gaps by
  ``turn_gaps()``; they are not the same fact, and only one of them has a
  record a reference can still resolve;
- **an abandoned-then-replayed attempt pair** (turn 5: attempt 0 abandoned,
  attempt 1 authoritative) with the crash and resume in the run timeline --
  FR-017's "one continuous run with its interruption and resume point visible
  in sequence";
- **a withheld capture** (turn 6), so replay is exercised on a turn whose
  structured panels must render normally with the image marked unavailable
  (FR-034, SC-015).

One property of the fixture is worth stating because it makes the test
stronger than it looks: the ``Run`` record says ``record_completeness_status:
complete`` while the store's own ``turn_gaps()`` lists two turns. 002's store
would normally agree with itself, but the two are *separately published reads*
and can disagree -- and FR-021 quarantines on the status while the constitution
quarantines on the record having gaps (US4 notes, finding 4). Seeding the
disagreement deliberately means every quarantine assertion below is passing on
the ``turn_gaps()`` layer alone, with the status layer saying the opposite.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

pytest.importorskip("fastapi")


GAPPED_TURN = 3
"""Never written at all: no attempt, no record, nothing to resolve."""

ABANDONED_ONLY_TURN = 7
"""Attempted, abandoned, never replayed -- a gap that still has a record."""

REPLAYED_TURN = 5
"""Crash and resume: attempt 0 abandoned, attempt 1 authoritative (FR-009)."""

WITHHELD_CAPTURE_TURN = 6


@pytest.fixture
def replay_store() -> Any:
    from web_support.fixtures import make_event, make_store, make_turn_cycle

    events = [
        make_event("run_started", minutes=0),
        make_event("turn_started", minutes=5, turn_number=REPLAYED_TURN),
        make_event("crash_detected", minutes=6, turn_number=REPLAYED_TURN),
        make_event(
            "save_restored",
            minutes=7,
            turn_number=REPLAYED_TURN,
            detail={"save_name": "civsim__run-1__t0005"},
        ),
        make_event("resumed", minutes=8, turn_number=REPLAYED_TURN),
        make_event("turn_started", minutes=9, turn_number=ABANDONED_ONLY_TURN),
        make_event("crash_detected", minutes=10, turn_number=ABANDONED_ONLY_TURN),
        make_event("run_finished", minutes=20),
    ]
    store = make_store(
        turns=8,
        lifecycle_state="finished",
        gap_turns=[GAPPED_TURN, ABANDONED_ONLY_TURN],
        replayed_turns=[REPLAYED_TURN],
        withheld_capture_turns=[WITHHELD_CAPTURE_TURN],
        step_count=2,
        events=events,
    )
    # Turn 7 was attempted and abandoned, and nothing replaced it. It is a gap
    # by `turn_gaps()` -- no authoritative attempt -- but unlike turn 3 there is
    # a record behind it, which is the case `build_turn_cycle_view(allow_gap=True)`
    # exists for (US2 notes).
    store.seed_turn_cycle(
        make_turn_cycle(
            ABANDONED_ONLY_TURN,
            attempt_index=0,
            is_authoritative=False,
            outcome="abandoned",
            step_count=2,
        )
    )
    return store


@pytest.fixture
def replay_client(replay_store):
    from web_support.fixtures import make_client

    with make_client(replay_store) as client:
        yield client


def _json(client, path: str, expect: int = 200) -> Any:
    response = client.get(path, headers={"Accept": "application/json"})
    assert response.status_code == expect, f"{path} -> {response.status_code}: {response.text}"
    return response.json()


def _html(client, path: str, expect: int = 200) -> str:
    response = client.get(path, headers={"Accept": "text/html"})
    assert response.status_code == expect, f"{path} -> {response.status_code}"
    return response.text


# --------------------------------------------------------------------------
# The gap is marked, and the run is flagged unfit for trend comparison
# --------------------------------------------------------------------------


def test_the_fixture_really_has_gaps(replay_store):
    """Guard on the fixture itself, so the assertions below cannot pass vacuously."""
    assert replay_store.turn_gaps("run-1") == [GAPPED_TURN, ABANDONED_ONLY_TURN]
    assert replay_store.get_run("run-1").record_completeness_status == "complete", (
        "the fixture deliberately seeds a store that calls the run complete while "
        "listing gapped turns -- see the module docstring"
    )


def test_a_gapped_turn_is_marked_never_rendered_as_a_normal_turn(replay_client):
    """FR-016, invariant V4. A gap and a turn that never existed are distinct.

    Both are 404s -- the server cannot serve either -- but they carry different
    ``kind``s, because "this run stopped before turn 99" and "turn 3 of this run
    is missing from the record" are different facts and a client may act on them
    differently.
    """
    gap = _json(replay_client, f"/runs/run-1/turns/{GAPPED_TURN}", expect=404)
    assert gap["kind"] == "turn_gap"
    assert gap["detail"]["turn_gaps"] == [GAPPED_TURN, ABANDONED_ONLY_TURN]
    assert "unfit for trend comparison" in gap["message"]

    beyond = _json(replay_client, "/runs/run-1/turns/99", expect=404)
    assert beyond["kind"] == "turn_not_found"


def test_a_turn_whose_every_attempt_was_abandoned_still_explains_itself(replay_client):
    """The second gap kind: a record exists, so a reference to it must resolve.

    A bare turn reference still gets the gap 404 -- there is no authoritative
    attempt and the bare reference has always meant "the authoritative one". But
    a reference naming the attempt returns that attempt, marked as the gap it
    belongs to, rather than answering like a dead link (FR-009 over FR-016's
    404; ``allow_gap`` in ``viewmodels/turn.py``).
    """
    bare = _json(replay_client, f"/runs/run-1/turns/{ABANDONED_ONLY_TURN}", expect=404)
    assert bare["kind"] == "turn_gap"

    named = _json(replay_client, f"/runs/run-1/turns/{ABANDONED_ONLY_TURN}?attempt=0")
    assert named["attempt_index"] == 0
    assert named["is_authoritative"] is False
    assert named["outcome"] == "abandoned"
    assert named["completeness"]["is_gap"] is True
    assert named["completeness"]["is_complete"] is False


def test_the_run_is_flagged_unfit_for_trend_comparison_everywhere_it_is_shown(
    replay_client,
):
    """FR-016's second clause plus Principle III, on all three surfaces.

    The run is never hidden -- FR-016 requires marking, not hiding -- and it is
    excluded from every trend line. Both halves are asserted, because showing
    only the first would look like an oversight and showing only the second
    would launder incomplete data into the trend.
    """
    metrics = _json(replay_client, "/runs/run-1/metrics")
    assert metrics["trend_eligibility"]["eligible"] is False
    assert metrics["trend_eligibility"]["assessed"] is True
    assert metrics["gapped_turns"] == [GAPPED_TURN, ABANDONED_ONLY_TURN]
    assert metrics["series"], "a quarantined run still gets its own chart"

    catalog = _json(replay_client, "/runs")
    assert "run-1" in catalog["quarantined_run_ids"]
    assert "run-1" in [row["run_id"] for row in catalog["runs"]]

    comparison = _json(replay_client, "/compare?runs=run-1")
    assert comparison["quarantined_run_ids"] == ["run-1"]
    assert "run-1" in [run["run_id"] for run in comparison["runs"]]
    for series_list in comparison["series"].values():
        assert [s for s in series_list if s["run_id"] == "run-1"] == []


def test_a_gapped_turn_is_a_break_in_the_trajectory_not_a_join(replay_client):
    """The rendering half of Principle III, and the one a regression hides in.

    data-model.md SS10 allows a gapped turn to be omitted from ``points`` only
    because the gap stays visible on the chart. A renderer that joins ``points``
    end to end draws a straight line **across** turns 3 and 7 -- the chart then
    reports smooth progress through two turns nobody recorded.

    This asserts the property on the *server-rendered* page, which is the one
    both readers get: one ``<polyline>`` per unbroken run of turns, a drawn
    marker at each break, and no polyline spanning a gap.
    """
    body = _json(replay_client, "/runs/run-1/metrics?series=science_output")
    turns = [point["turn"] for point in body["series"][0]["points"]]
    assert turns == [1, 2, 4, 5, 6, 8]

    markup = _html(replay_client, "/runs/run-1/metrics?series=science_output")
    polylines = re.findall(r'<polyline class="series"[^>]*points="([^"]*)"', markup)
    assert len(polylines) == 3, (
        f"turns {turns} form three unbroken runs (1-2, 4-6, 8), so the line must be "
        f"drawn as three polylines; found {len(polylines)}"
    )
    assert [len(p.split()) for p in polylines] == [2, 3, 1]
    for turn in (GAPPED_TURN, ABANDONED_ONLY_TURN):
        assert f'data-break-turn="{turn}"' in markup, (
            f"the break at turn {turn} is drawn, not merely implied by the absence "
            f"of a line"
        )


# --------------------------------------------------------------------------
# The abandoned/authoritative attempt pair, with its timeline (FR-009, FR-017)
# --------------------------------------------------------------------------


def test_both_attempts_of_a_replayed_turn_appear_and_neither_substitutes_the_other(
    replay_client,
):
    """FR-009: a reference someone shared five minutes ago still explains itself."""
    authoritative = _json(replay_client, f"/runs/run-1/turns/{REPLAYED_TURN}")
    assert authoritative["attempt_index"] == 1
    assert authoritative["is_authoritative"] is True
    assert authoritative["superseded_by"] is None

    abandoned = _json(replay_client, f"/runs/run-1/turns/{REPLAYED_TURN}?attempt=0")
    assert abandoned["attempt_index"] == 0
    assert abandoned["is_authoritative"] is False
    assert abandoned["superseded_by"] == 1
    assert abandoned["outcome"] == "abandoned"
    # The turn itself is not a gap: an authoritative attempt exists.
    assert abandoned["completeness"]["is_gap"] is False

    markup = _html(replay_client, f"/runs/run-1/turns/{REPLAYED_TURN}?attempt=0")
    assert "superseded" in markup
    assert f"/runs/run-1/turns/{REPLAYED_TURN}" in markup


def test_the_interruption_and_the_resume_are_in_the_timeline_in_sequence(replay_client):
    """FR-017: one continuous run, with the interruption visible in order."""
    timeline = _json(replay_client, "/runs/run-1/events")
    types = [event["event_type"] for event in timeline["events"]]
    assert types.index("crash_detected") < types.index("save_restored") < types.index(
        "resumed"
    )

    crash = next(e for e in timeline["events"] if e["event_type"] == "crash_detected")
    assert crash["turn_number"] == REPLAYED_TURN

    # The run is still presented as one run, not two.
    detail = _json(replay_client, "/runs/run-1")
    assert detail["summary"]["run_id"] == "run-1"
    assert detail["summary"]["parent_run_id"] is None


def test_a_withheld_capture_leaves_the_structured_panels_intact(replay_client):
    """FR-034 / SC-015: replay a turn whose image is withheld, and it still reads."""
    turn = _json(replay_client, f"/runs/run-1/turns/{WITHHELD_CAPTURE_TURN}")
    capture = turn["steps"][0]["capture"]
    assert capture["available"] is False
    assert capture["unavailable_reason"] == "withheld"
    assert capture["image_url"] is None

    # The decision and its evidence are still there (UP-004).
    assert turn["steps"][0]["observation"]
    assert turn["steps"][0]["decision"]["action_label"]

    markup = _html(replay_client, f"/runs/run-1/turns/{WITHHELD_CAPTURE_TURN}")
    assert "no capture shown" in markup
    assert "What the agent could see" in markup


# --------------------------------------------------------------------------
# Stepping between turns preserves the focused panel (FR-015)
# --------------------------------------------------------------------------


def test_stepping_between_adjacent_turns_preserves_the_focused_panel(replay_client):
    """FR-015, followed the way a user would: by clicking the link on the page.

    The test does not construct the next URL itself. It reads the step-forward
    link out of the rendered page and follows it, so what is asserted is that
    *the page's own navigation* carries the focus -- which is the thing a user
    actually depends on and the thing a template edit could silently drop.
    """
    focus = "observation.cities"
    first = _html(replay_client, f"/runs/run-1/turns/1?focus={focus}")
    assert 'class="observation-entry focused"' in first

    forward = re.search(r'class="step-forward" href="([^"]+)"', first)
    assert forward is not None, "the replay page must offer a step-forward link"
    next_url = forward.group(1)
    assert f"focus={focus}" in next_url, (
        f"the step-forward link dropped the focused panel: {next_url}"
    )

    second = _html(replay_client, next_url)
    assert 'class="observation-entry focused"' in second

    # And the directing session resolving the identical reference sees the same
    # focus, which is what makes this a shared view rather than a browser state.
    body = _json(replay_client, next_url)
    assert body["turn_number"] == 2
    assert body["focus_panel_id"] == focus


def test_jumping_to_a_turn_preserves_the_focused_panel(replay_client):
    """The other half of FR-015's navigation: jump, not step."""
    focus = "observation.cities"
    page = _html(replay_client, f"/runs/run-1/turns/4?focus={focus}")

    jumps = re.findall(r'<a href="(/runs/run-1/turns/\d+[^"]*)">\d+</a>', page)
    assert jumps, "the replay page must offer jump-to-turn links"
    assert all(f"focus={focus}" in href for href in jumps), (
        f"a jump link dropped the focused panel: "
        f"{[href for href in jumps if 'focus=' not in href][:3]}"
    )

    landed = _json(replay_client, jumps[0])
    assert landed["focus_panel_id"] == focus


def test_the_trajectory_point_for_a_turn_links_to_that_turn(replay_client):
    """FR-015's third clause: selecting a point on the trajectory jumps to a turn.

    Asserted on the server-rendered chart, because that is the one both readers
    get. ``static/trajectory.js`` enhances it; it does not supply the
    navigation, and a chart whose points were only clickable once a script ran
    would be a chart the directing session could not follow.
    """
    markup = _html(replay_client, "/runs/run-1/metrics?series=science_output")
    points = re.findall(
        r'<a class="turn-point" href="(/runs/run-1/turns/\d+)"[^>]*data-point-turn="(\d+)"',
        markup,
    )
    assert points, "every trajectory point must be a link to its own turn"
    assert {int(turn) for _, turn in points} == {1, 2, 4, 5, 6, 8}
    for href, turn in points:
        assert href.endswith(f"/turns/{turn}")
        assert _json(replay_client, href)["turn_number"] == int(turn)
