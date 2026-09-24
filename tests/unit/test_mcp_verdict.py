"""M2 — a tool call that returned is not an action that applied.

Every coverage number the pivot produces rests on this classification, so these cases are
evidence-grade rather than unit-test decoration. Each one is a real reply shape observed
against civ6-mcp on 2026-09-24.
"""

from __future__ import annotations

import pytest

from civsim_harness.agent.mcp_verdict import Verdict, classify_reply, is_applied

SUMMARY_HEAD = """=== TURN SUMMARY ===

-- STATE --
Turn 76 | Persia (Cyrus) | Score: 113 | Emperor
Gold: 382 (+6/turn) | Science: 9.0 | Culture: 7.7
Research: Masonry | Civic: Defensive Tactics
Cities: 2 | Population: 12 | Units: 12

-- RESEARCH & CIVICS --
Masonry — 51%, 2 turns [Boost: Build a Quarry.]
  (a unit cannot be built here without Bronze Working)

-- CITIES & PRODUCTION --
  (unavailable: RuntimeError: connection reset)
"""


@pytest.mark.parametrize(
    "body,expected",
    [
        # Transport. Twelve of these in a row were once recorded as successful calls.
        ("Cannot connect to Civ 6 at 127.0.0.1:4318.", Verdict.TRANSPORT_FAILURE),
        ("Error executing tool get_diplomacy: 0 bytes read on a total of 8 expected",
         Verdict.TRANSPORT_FAILURE),
        # Structured refusal signals, anywhere in the body.
        ("POLICIES_SET|Policies updated.\nWARN:SILENT_FAILURE — engine rejected: slot 0",
         Verdict.ENGINE_REFUSED),
        ("ERR:CANNOT_FOUND|Too close to another city", Verdict.ENGINE_REFUSED),
        # Server error text opening the reply.
        ("Error: Empty overview response", Verdict.ENGINE_REFUSED),
        # end_turn's runtime rejection — three model blocks ended zero turns over this.
        ("Empty reflections: tactical, strategic, tooling, planning, hypothesis.",
         Verdict.ENGINE_REFUSED),
        ("Unit cannot found cities (not a settler or no moves)", Verdict.ENGINE_REFUSED),
        # Successes, including replies whose text contains refusal words further in.
        ("FOUNDED|11,23", Verdict.APPLIED),
        ("Turn 103 -> 104 | Score: 437", Verdict.APPLIED),
        ("CAPTURE_MOVE|47,14|from:46,21|now_at:46,21|BLOCKED (path may be blocked)",
         Verdict.APPLIED),
    ],
)
def test_classify_reply(body: str, expected: Verdict) -> None:
    assert classify_reply(body) is expected


def test_long_aggregate_is_not_a_refusal() -> None:
    """A long correct summary must not be a refusal because prose appears inside it.

    Named for what it protects. This exact regression made a working tool look broken, and
    because the harness aborts on repeated refusals it would have killed runs over a tool
    that was fine. It is the case a future edit to the marker lists is most likely to
    reintroduce.
    """
    assert classify_reply(SUMMARY_HEAD) is Verdict.APPLIED
    assert is_applied(SUMMARY_HEAD)


def test_is_error_flag_is_honoured_but_not_relied_on() -> None:
    """isError is respected when set — and useless on its own.

    The server does not set it for game-level refusals, which is why a body that plainly
    reports a refusal must still classify as one with the flag clear.
    """
    assert classify_reply("anything", is_error=True) is Verdict.MCP_ERROR
    assert classify_reply("ERR:NO_SESSION", is_error=False) is Verdict.ENGINE_REFUSED


def test_transport_outranks_refusal() -> None:
    """'The game said no' and 'nothing was asked' are different facts.

    A reply that mentions both must read as transport: counting an unreachable client as a
    refusal would attribute a decision to a game that never received the call.
    """
    body = "ERR:SOMETHING\nCannot connect to Civ 6 at 127.0.0.1:4318."
    assert classify_reply(body) is Verdict.TRANSPORT_FAILURE


def test_unconfirmed_replies_are_flagged_without_being_refusals() -> None:
    """"Action completed (no response)" confirms nothing and must be visible as such.

    A real confirmation looks like PRODUCING|DISTRICT_CAMPUS|4 turns or FOUNDED|11,23.
    Observed 2026-09-24: within one block, two set_city_production calls confirmed and two
    returned no response at all. Treating those four as identical would be exactly the
    "a call that returned is an action that applied" assumption this module exists to
    refuse — applied here to our own numbers rather than only to the upstream server's.
    """
    from civsim_harness.agent.mcp_verdict import is_unconfirmed

    assert is_unconfirmed("Action completed (no response).")
    assert not is_unconfirmed("PRODUCING|DISTRICT_CAMPUS|4 turns")
    assert not is_unconfirmed("FOUNDED|11,23")
    # Still not a refusal: nothing says it failed.
    assert classify_reply("Action completed (no response).") is Verdict.APPLIED
