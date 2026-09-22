"""The availability-field ratchet (2026-09-22 live-lane fix).

A completed enumeration of every shipped goal (`tests/live/goals/*.yaml`) found the defect was
never general rot: ten of thirteen goals' `prerequisites` already ask the game what it is
offering right now, all ten the same way (a game-computed *offer* field: `available_productions`,
`available_beliefs`, `researchable_techs`, `movable_count`, `promotions_available_count`,
`prompt_options`, `available_policies`, or -- for the two selection/save goals -- the action's own
availability_predicate quoted directly). Three were written in a different idiom and nothing made
them agree: they asked only whether the *ingredients* existed (a unit count, a resource balance),
which a board can satisfy while the actual action stays refused for a reason no count captures.
`use_a_builder` proved this live at real cost, 2026-09-22: 5 Builders, 15 charges, every one
standing on the capital's city-centre plot where no improvement was legal -- the run reported
"unreached" when the truth was "unreachable by construction".

This module is the ratchet that makes the thirteen agree going forward: every shipped goal not
named in `live.goal_run.KNOWN_AVAILABILITY_EXCEPTIONS` (each entry there carries its own reviewed,
load-bearing reason -- see that dict's docstring) must have at least one `prerequisites` predicate
that references one of `live.goal_run.AVAILABILITY_FACT_NAMES`. `test_a_synthetic_ingredients_
only_goal_fails_the_ratchet` is the negative control this ratchet is required to have: it proves,
by actually invoking the checker and the assertion helper against a goal built to be wrong, that
the ratchet can fail -- "a check nobody has proven can fail is not a check".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

from live.goal_run import (  # noqa: E402
    AVAILABILITY_FACT_NAMES,
    KNOWN_AVAILABILITY_EXCEPTIONS,
    Goal,
    assert_goal_prerequisites_reference_an_availability_field,
    goal_references_availability_field,
    load_goals,
)

# --------------------------------------------------------------------------
# 1. The ratchet itself, over the real shipped goal library
# --------------------------------------------------------------------------


def _non_exempt_goals() -> list[Goal]:
    goals = load_goals()
    assert set(KNOWN_AVAILABILITY_EXCEPTIONS) <= set(goals), (
        "KNOWN_AVAILABILITY_EXCEPTIONS names a goal id that is not shipped any more -- stale entry"
    )
    return [g for g in goals.values() if g.goal_id not in KNOWN_AVAILABILITY_EXCEPTIONS]


@pytest.mark.parametrize(
    "goal_id",
    sorted(set(load_goals()) - set(KNOWN_AVAILABILITY_EXCEPTIONS)),
)
def test_every_non_exempt_shipped_goal_references_an_availability_field(goal_id: str) -> None:
    """The ratchet, run one goal at a time so a single regression names exactly which goal broke.

    Includes `use_a_builder` (this fix: `player.units.selected_available_builds`, guarded on
    `selected_is_builder`) and the eight goals that already did this correctly before this change.
    Deliberately excludes `found_second_city` and `send_delegation` -- see
    `KNOWN_AVAILABILITY_EXCEPTIONS` for why each is a reviewed exception rather than a silent gap.
    """
    goal = load_goals()[goal_id]
    assert_goal_prerequisites_reference_an_availability_field(goal)


def test_every_non_exempt_goal_via_the_boolean_checker_too() -> None:
    """The same assertion through the boolean-returning half of the API, not just the raiser."""
    for goal in _non_exempt_goals():
        assert goal_references_availability_field(goal), goal.goal_id


def test_the_known_availability_exceptions_are_exactly_these_and_no_others() -> None:
    """Pins the exception list so a new one can only be added by a reviewed diff to this test.

    `found_second_city` and `send_delegation` are NOT the same kind of exception --
    `found_second_city` is a confirmed observation-surface gap (no field says a legal settle site
    is reachable); `send_delegation` is a confirmed-broken goal deliberately left unfixed and
    tracked as a task instead. `select_city_then_unit` and `save_named_game` are not defects at
    all: for those two actions a bare count already IS the game's own offer condition. Each
    entry's own value states which case it is; this test only pins the key set.
    """
    assert set(KNOWN_AVAILABILITY_EXCEPTIONS) == {
        "select_city_then_unit",
        "save_named_game",
        "found_second_city",
        "send_delegation",
    }
    for goal_id, reason in KNOWN_AVAILABILITY_EXCEPTIONS.items():
        assert len(reason.split()) >= 8, f"{goal_id}'s exception reason does not say enough"


def test_the_published_field_set_is_not_empty_and_carries_no_bare_ingredient_name() -> None:
    """The published set is the ratchet's data half -- guard it against the exact
    ingredients-exist anti-pattern this ratchet exists to reject (the raw counts/resources the
    three originally-broken goals used: how many Settlers/Builders/civs-met a player has, a
    charge or gold total), so it can never be widened into uselessness by someone adding an
    ingredient field to make a future goal pass trivially.

    Not a bare `*_count` ban: `promotions_available_count` and (if ever added) similarly-named
    fields legitimately belong here -- they count what the game is OFFERING, not what the player
    merely possesses, which is exactly the distinction this ratchet is built to preserve.
    """
    assert AVAILABILITY_FACT_NAMES
    banned_leaves = {
        "builder_count",
        "settler_count",
        "builder_charges_total",
        "met_civ_count",
        "gold",
        "units_count",
        "cities_count",
    }
    for name in AVAILABILITY_FACT_NAMES:
        leaf = name.rsplit(".", 1)[-1]
        assert leaf not in banned_leaves, name


# --------------------------------------------------------------------------
# 2. The negative control: a synthetic goal that MUST fail
# --------------------------------------------------------------------------


def _ingredients_only_synthetic_goal() -> Goal:
    """Deliberately in `send_delegation`'s own broken shape (a met-civ count and a gold balance),
    built here rather than loaded from a file so this control exists independently of whether
    `send_delegation` itself ever changes -- it is a control on the CHECKER, not on that goal.
    """
    return Goal(
        goal_id="synthetic_ingredients_only_control",
        title="Negative control: ingredients only, never an offer field",
        objective="Do a thing the game may or may not currently be offering.",
        success="player.cities.count >= 1",
        prerequisites=(
            "player.met_civ_count >= 1",
            "player.gold >= 25",
        ),
        turn_cap=1,
    )


def test_a_synthetic_ingredients_only_goal_fails_the_boolean_checker() -> None:
    control = _ingredients_only_synthetic_goal()
    assert goal_references_availability_field(control) is False


def test_a_synthetic_ingredients_only_goal_fails_the_ratchet_assertion() -> None:
    """The control observed actually failing, not merely asserted to: the assertion helper is
    invoked for real and its `AssertionError` is caught here, carrying the load-bearing sentence
    so a contributor who trips this in the real ratchet learns the rule from the message itself.
    """
    control = _ingredients_only_synthetic_goal()
    with pytest.raises(AssertionError) as excinfo:
        assert_goal_prerequisites_reference_an_availability_field(control)
    message = str(excinfo.value)
    assert "Does the predicate ask the game what is offered" in message
    assert "ask whether the ingredients exist" in message
    assert control.goal_id in message


def test_a_synthetic_goal_with_one_real_offer_field_passes_the_same_control() -> None:
    """The control's other half: the checker is not simply always-False. Adding exactly one
    predicate that references a published offer field flips the same synthetic goal to passing,
    proving the checker responds to the field, not to the goal's identity or shape.
    """
    offered = Goal(
        goal_id="synthetic_with_offer_field",
        title="Negative control's twin: one offer field added",
        objective="Do a thing the game is offering.",
        success="player.cities.count >= 1",
        prerequisites=(
            "player.met_civ_count >= 1",
            "player.researchable_techs != []",
        ),
        turn_cap=1,
    )
    assert goal_references_availability_field(offered) is True
    assert_goal_prerequisites_reference_an_availability_field(offered)  # must not raise
