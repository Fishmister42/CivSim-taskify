"""The live view, end to end against the `MatchStore` fake (T034).

quickstart.md Scenario 1 names this file and its two cases by selector:

    uv run pytest tests/integration/test_live_view.py -k advancing_run
    uv run pytest tests/integration/test_live_view.py -k empty_state

**On not sleeping.** SC-003 puts a 5-second bound on a newly recorded turn
appearing in an open live view. That bound is the sum of two things and nothing
else: how long the client waits between polls (fixed at 2s by research R5, in
`static/poll.js`) and how long a response takes. A test that slept for five
seconds ten times over would take a minute to assert something it can assert
directly -- so this suite measures the second term, reads the first out of
`poll.js`, and asserts their sum is inside the window. That is a stronger claim
than a sleep-and-look test, because it fails when the *budget* is exceeded
rather than only when a particular machine happens to be slow.

The stall case is checked the same way. SC-009's 60-second bound is met because
`stalled` appears on the very next poll after 002 records the event -- this
feature runs no timer of its own and forms no independent judgment about
whether a run is stuck (research R7, invariant V3), so the only latency is the
poll interval.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

REPO_ROOT = Path(__file__).resolve().parents[2]
POLL_JS = REPO_ROOT / "src" / "civsim_web" / "static" / "poll.js"

#: spec.md SC-003.
CURRENCY_WINDOW_SECONDS = 5.0
#: spec.md SC-009.
STALL_WINDOW_SECONDS = 60.0


def poll_interval_seconds() -> float:
    """The interval `static/poll.js` actually uses, read from the file.

    Read rather than restated: a test asserting against its own copy of the
    number would keep passing after someone changed the real one.
    """
    source = POLL_JS.read_text(encoding="utf-8")
    match = re.search(r"POLL_INTERVAL_MS\s*=\s*(\d+)", source)
    assert match, "poll.js no longer declares POLL_INTERVAL_MS"
    return int(match.group(1)) / 1000.0


def timed_get(client, path: str) -> tuple[object, float]:
    """One poll, as the browser makes it, with its wall-clock cost."""
    started = time.perf_counter()
    response = client.get(path, headers={"Accept": "application/json"})
    return (response, time.perf_counter() - started)


# ==========================================================================
# advancing_run
# ==========================================================================


def test_advancing_run_reflects_each_new_turn_within_the_currency_window(web_store_factory):
    """Ten consecutive turns, each visible on the poll after it is recorded.

    This is User Story 1's Independent Test in miniature: follow ten turns
    without ever focusing the game window, and at every point be able to state
    the current turn, the last action, its stated reason, and the run's health.
    """
    from web_support.fixtures import make_client, make_save_point, make_turn_cycle

    store = web_store_factory(turns=1)
    interval = poll_interval_seconds()
    assert interval < CURRENCY_WINDOW_SECONDS, (
        f"a {interval}s poll interval cannot meet SC-003's {CURRENCY_WINDOW_SECONDS}s window"
    )

    worst_response = 0.0
    with make_client(store) as client:
        for turn in range(2, 12):
            # The harness records a turn...
            store.seed_turn_cycle(make_turn_cycle(turn))
            store.seed_save_point(make_save_point(turn))

            # ...and the next poll shows it. No reload, no re-navigation.
            response, elapsed = timed_get(client, "/runs/run-1")
            worst_response = max(worst_response, elapsed)
            assert response.status_code == 200

            body = response.json()
            assert body["summary"]["turn_count"] == turn
            assert body["current_turn"]["turn_number"] == turn

            # The four facts SC-001 times a user against, all present with no
            # further navigation (FR-001, UP-003).
            assert body["latest_decision"]["action_label"]
            assert body["latest_decision"]["reasoning_label"]
            assert body["summary"]["health"]["state"] == "running"
            assert body["last_confirmed_current_at"]

    assert interval + worst_response < CURRENCY_WINDOW_SECONDS, (
        f"poll interval {interval}s plus worst response {worst_response:.3f}s "
        f"exceeds SC-003's {CURRENCY_WINDOW_SECONDS}s currency window"
    )


def test_advancing_run_surfaces_a_stall_only_from_the_harness_own_event(web_store_factory):
    """FR-003 / SC-009, and the rule that keeps it honest.

    Stopping advancement alone must *not* produce `stalled`: "no new turn
    recently" is exactly the independently computed judgment research R7 and
    invariant V3 rule out, and a second detector disagreeing with 002's is the
    asymmetry Principle VI exists to prevent. Only 002's own
    `hang_detected`/`unresponsive_detected` event may do it.
    """
    from web_support.fixtures import make_client, make_event

    store = web_store_factory(turns=3)

    with make_client(store) as client:
        # Advancement has stopped. Nothing has been recorded about why.
        first = client.get("/runs/run-1", headers={"Accept": "application/json"}).json()
        assert first["summary"]["health"]["state"] == "running", (
            "a run that has merely stopped advancing is not stalled -- a slow "
            "turn and a stalled run are distinguishable only by 002's own event"
        )

        # 002 records the hang.
        store.seed_event(make_event("hang_detected", minutes=30))

        response, elapsed = timed_get(client, "/runs/run-1")
        body = response.json()

    health = body["summary"]["health"]
    assert health["state"] == "stalled"
    assert health["reason_event_id"], "a stall must name the event that justifies it"
    assert poll_interval_seconds() + elapsed < STALL_WINDOW_SECONDS

    # FR-027 / spec Acceptance Scenario US1 SS4: the last-known good turn is
    # identified, so the stale state is never presented as current.
    assert body["intervention_info"]["last_known_good_turn"] is not None


def test_advancing_run_shows_the_capture_beside_the_structured_panels(web_client):
    """FR-031 / UP-009: the picture and the numbers, together."""
    markup = web_client.get("/runs/run-1", headers={"Accept": "text/html"}).text

    assert 'class="capture-image"' in markup
    assert "/captures/" in markup
    assert "What the agent could see" in markup
    # The out-of-game telemetry panel is visually distinct from the in-game
    # observation panels (data-model.md SS8 Validation, FR-013).
    assert 'class="telemetry"' in markup
    assert "out of game" in markup


def test_advancing_run_shows_provider_failures_as_timeline_events(web_store_factory):
    """FR-004: a retrying provider chain is a visible event, not a silent pause."""
    from web_support.fixtures import make_client, make_event

    store = web_store_factory(
        turns=2,
        events=[
            make_event("model_call_failed", minutes=5, detail={"provider": "primary"}),
            make_event("model_fallback_used", minutes=6, detail={"to": "secondary"}),
        ],
    )
    with make_client(store) as client:
        glance = client.get("/runs/run-1", headers={"Accept": "application/json"}).json()
        timeline = client.get("/runs/run-1/events", headers={"Accept": "application/json"}).json()

    assert [event["event_type"] for event in glance["recent_events"]] == [
        "model_call_failed",
        "model_fallback_used",
    ]
    # The glance's window and the full timeline read the same source, so they
    # can never disagree about what happened.
    assert [event["event_type"] for event in timeline["events"]] == [
        event["event_type"] for event in glance["recent_events"]
    ]


# ==========================================================================
# empty_state
# ==========================================================================


def test_empty_state_for_a_run_with_no_recorded_turns(web_store_factory):
    """data-model.md SS3: an explicit empty state, never a blank panel.

    Both readers get the same sentence. An empty state visible only to the user
    would be an asymmetry, and an unexplained `null` is the "absence rendered as
    confirmed fact" UP-005 forbids.
    """
    from web_support.fixtures import make_client

    with make_client(web_store_factory(turns=0)) as client:
        response = client.get("/runs/run-1", headers={"Accept": "application/json"})
        body = response.json()
        markup = client.get("/runs/run-1", headers={"Accept": "text/html"}).text

    assert response.status_code == 200
    assert body["current_turn"] is None
    assert body["latest_decision"] is None
    assert body["summary"]["turn_count"] == 0
    assert body["empty_state_reason"]

    assert "No turns recorded yet" in markup
    assert body["empty_state_reason"] in markup

    # The run is still identifiable and still actionable elsewhere (FR-027).
    assert body["intervention_info"]["run_id"] == "run-1"
    assert body["summary"]["health"]["state"]


def test_empty_state_still_renders_health_and_intervention_information(web_store_factory):
    """UP-005: an empty turn record does not make the rest of the page empty."""
    from web_support.fixtures import make_client

    with make_client(web_store_factory(turns=0, lifecycle_state="preparing")) as client:
        body = client.get("/runs/run-1", headers={"Accept": "application/json"}).json()

    assert body["summary"]["health"]["state"] == "running"
    assert body["summary"]["health"]["lifecycle_state"] == "preparing"
    assert body["intervention_info"]["lifecycle_status"] == "preparing"
