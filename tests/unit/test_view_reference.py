"""View-reference round-trip (T018, data-model.md SS12; FR-008, SC-012).

The reference *is* the URL path, so "round-trips exactly" is not a convenience
property -- it is the whole guarantee. SC-012 asks for a reference handed from
the user to the directing session to resolve to the identical run, turn, and
panel in 100% of attempts, and the way that becomes a property of the code
rather than a percentage to test toward is: one path shape, parsed and rendered
by one pair of functions that invert each other.
"""

from __future__ import annotations

import pytest

from civsim_web.refs.reference import (
    ViewReference,
    ViewReferenceError,
    parse_view_reference,
)

SHAPES = [
    ("/runs/run-1", ViewReference(run_id="run-1")),
    ("/runs/run-1/turns/34", ViewReference(run_id="run-1", turn_number=34)),
    (
        "/runs/run-1/turns/34/steps/7",
        ViewReference(run_id="run-1", turn_number=34, step_index=7),
    ),
    (
        "/runs/run-1/turns/34/panels/current_turn.city_yields",
        ViewReference(run_id="run-1", turn_number=34, panel_id="current_turn.city_yields"),
    ),
    (
        "/runs/run-1/turns/34/steps/7/panels/turn.model_call_cost",
        ViewReference(
            run_id="run-1", turn_number=34, step_index=7, panel_id="turn.model_call_cost"
        ),
    ),
    (
        "/runs/run-1/panels/run.intervention_info",
        ViewReference(run_id="run-1", panel_id="run.intervention_info"),
    ),
]


@pytest.mark.parametrize(("path", "expected"), SHAPES, ids=[s[0] for s in SHAPES])
def test_every_canonical_shape_parses(path, expected):
    assert parse_view_reference(path) == expected


@pytest.mark.parametrize(("path", "reference"), SHAPES, ids=[s[0] for s in SHAPES])
def test_every_canonical_shape_serializes(path, reference):
    assert reference.path == path
    assert str(reference) == path


@pytest.mark.parametrize(("path", "_reference"), SHAPES, ids=[s[0] for s in SHAPES])
def test_round_trip_is_exact(path, _reference):
    """parse -> serialize -> parse is a fixed point, which is what SC-012 needs."""
    once = parse_view_reference(path)
    assert once.path == path
    assert parse_view_reference(once.path) == once


def test_query_and_fragment_are_not_part_of_the_reference():
    """`?format=json` changes the serialization, never the referent (FR-007)."""
    reference = parse_view_reference("/runs/run-1/turns/34?format=json")
    assert reference == ViewReference(run_id="run-1", turn_number=34)


def test_a_trailing_slash_resolves_to_the_same_reference():
    assert parse_view_reference("/runs/run-1/") == ViewReference(run_id="run-1")


def test_run_ids_needing_escaping_round_trip():
    """Run ids are opaque; a reference must survive one containing a slash."""
    reference = ViewReference(run_id="run/with slash")
    assert reference.path == "/runs/run%2Fwith%20slash"
    assert parse_view_reference(reference.path) == reference


def test_panel_ids_keep_their_dots():
    """Panel ids are dotted and human-readable; escaping must not mangle them."""
    reference = ViewReference(run_id="r", turn_number=1, panel_id="current_turn.city_yields")
    assert reference.path.endswith("/panels/current_turn.city_yields")


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/runs",
        "/runs/run-1/turns",
        "/runs/run-1/turns/not-a-number",
        "/runs/run-1/steps/3",
        "/compare?runs=a,b",
        "/healthz",
    ],
)
def test_non_references_are_rejected(path):
    """A malformed reference is distinct from one that resolves to nothing.

    FR-009 needs three outcomes to stay separate: unparseable (an error here),
    parseable-but-never-existed (a 404 at the route), and
    parseable-but-superseded (a 200 that explains itself). Collapsing any two
    loses the distinction FR-009 was written to preserve.
    """
    with pytest.raises(ViewReferenceError):
        parse_view_reference(path)


def test_a_step_reference_must_name_its_turn():
    """There is no route shape for a step without a turn, so there is no reference."""
    with pytest.raises(ValueError):
        ViewReference(run_id="r", step_index=3)


@pytest.mark.parametrize(("turn", "step"), [(0, None), (-1, None), (1, 0)])
def test_indices_are_one_based(turn, step):
    """002's data model makes both turn_number and step_index 1-based."""
    with pytest.raises(ValueError):
        ViewReference(run_id="r", turn_number=turn, step_index=step)
