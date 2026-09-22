"""The pure trend derivations (T028; 003 contract T1-T4; research R4, R5)."""

from __future__ import annotations

from civsim_harness.models.run import ComparabilityStatus, RecordCompletenessStatus
from civsim_harness.store.contract import ExclusionReason
from civsim_harness.store.trends import (
    exclusion_for,
    fingerprint_differences,
    first_divergence,
    turn_fingerprint,
    turn_metrics,
)
from store_support.builders import make_run, make_turn_cycle_record


def test_turn_metrics_are_numeric_yields_plus_derived_counts_and_nothing_else() -> None:
    """FR-018 names eight per-turn metrics: "science, culture, gold, faith, production and food
    per turn, plus city and unit counts". ``turn_metrics`` is a generic pass-through of a turn's
    ``yields`` numeric non-bool keys, so all six yield names work by construction -- but before
    this fixture, ``gold``, ``production`` and ``food`` appeared in no test in this file or in
    ``tests/contract/test_match_tracking_store.py`` at all, and ``faith`` appeared only once
    there (``test_t2_a_metric_the_record_never_carries_is_unavailable_not_zero``), as the name of
    a metric a fixture's ``yields`` deliberately *omits*, to prove an absent metric reports
    unavailable rather than zero -- never as a populated value. This fixture carries all six
    named yields with real values (plus a bool and a
    string key, to prove those two are excluded -- a flag plotted as ``1.0`` would be a
    fabricated measurement) alongside ``cities``/``units``, so the requirement's full eight-name
    set is demonstrated here rather than only implied by the pass-through's genericity.

    **Honest boundary**: this exercises FR-018's named set against a synthetic record only, and
    this test fabricates no yield anywhere outside this fixture.

    **Corrected 2026-09-22 (T272).** This docstring used to add "002's ``compute_yields`` is a
    no-op and no observation declares per-turn yields, so the science/culture/gold/faith/
    production/food series are empty ... on every run this project has actually recorded". That
    stopped being true on 2026-09-21, when ``catalogs/observations/yields.yaml``,
    ``lua/ingame/yields.lua`` and ``src/civsim_harness/run/yields.py`` landed (T258) and the
    production composition stopped using the ``_no_yields`` placeholder. MEASURED on the live
    store, 2026-09-22: **77 of 93 recorded turn cycles carry real yields**, every one of them all
    seven of ``science``, ``culture``, ``gold``, ``faith``, ``tourism``, ``gold_balance`` and
    ``faith_balance``; ``city_count`` and ``unit_count`` derive cleanly from all 93. The sentence
    was left here after the fact it described was fixed, and it was then read back out of this
    file into two separate audit reports on 2026-09-22 as though it were current -- which is what
    a stale claim in a test file costs. What is genuinely absent is ``production`` and ``food``,
    for the reason FR-018 now records (no empire-level source in the standard UI; Principle I).
    """
    record = make_turn_cycle_record(
        "r",
        3,
        yields={
            "science": 4,
            "culture": 2.5,
            "gold": 12,
            "faith": 3.5,
            "production": 7,
            "food": 1.5,
            "flag": True,
            "name": "x",
        },
        cities=2,
        units=5,
    )
    assert turn_metrics(record) == {
        "science": 4.0,
        "culture": 2.5,
        "gold": 12.0,
        "faith": 3.5,
        "production": 7.0,
        "food": 1.5,
        "city_count": 2.0,
        "unit_count": 5.0,
    }


def test_missing_yields_and_missing_entries_are_absent_not_zero() -> None:
    assert turn_metrics(make_turn_cycle_record("r", 1)) == {}
    only_cities = make_turn_cycle_record("r", 1, cities=1)
    assert turn_metrics(only_cities) == {"city_count": 1.0}


def test_fingerprint_is_order_sensitive_on_actions_and_canonical_on_parameters() -> None:
    a = turn_fingerprint(
        make_turn_cycle_record(
            "r",
            1,
            num_steps=2,
            actions=[("units.move_to", {"a": 1, "b": 2}), ("turn.end_turn", {})],
        )
    )
    same = turn_fingerprint(
        make_turn_cycle_record(
            "s",
            1,
            num_steps=2,
            actions=[("units.move_to", {"b": 2, "a": 1}), ("turn.end_turn", {})],
        )
    )
    swapped = turn_fingerprint(
        make_turn_cycle_record(
            "t",
            1,
            num_steps=2,
            actions=[("turn.end_turn", {}), ("units.move_to", {"a": 1, "b": 2})],
        )
    )
    assert fingerprint_differences(a, same) == ()
    assert [d.dimension for d in fingerprint_differences(a, swapped)] == ["actions"]


def test_first_divergence_names_the_first_shared_turn_that_differs_and_every_dimension() -> None:
    def prints(run_id: str, diverge_at: int, *, until: int):
        out = []
        for turn in range(1, until + 1):
            differs = turn >= diverge_at
            out.append(
                turn_fingerprint(
                    make_turn_cycle_record(
                        run_id,
                        turn,
                        yields={"science": 10 + (1 if differs else 0)},
                        cities=1 + (1 if differs else 0),
                        actions=[("units.found_city" if differs else "units.move_to", {})],
                    )
                )
            )
        return out

    turn, differed = first_divergence(prints("a", 12, until=15), prints("b", 99, until=13))
    assert turn == 12
    assert [d.dimension for d in differed] == ["actions", "city_count", "science"]
    assert differed[2].value_a == "11" and differed[2].value_b == "10"

    assert first_divergence(prints("a", 99, until=5), prints("b", 99, until=8)) == (None, ())
    # A turn only one side holds is never a difference.
    assert first_divergence(prints("a", 6, until=8), prints("b", 99, until=5)) == (None, ())


def test_exclusion_rule_matrix() -> None:
    comparable = make_run("r")
    degraded = make_run("d", comparability_status="visually_degraded")
    not_comparable = make_run("n", comparability_status="not_comparable")

    gapped = exclusion_for(
        comparable, RecordCompletenessStatus.HAS_GAPS, [3, 4], include_visually_degraded=False
    )
    assert (
        gapped is not None and gapped.reason is ExclusionReason.HAS_GAPS and gapped.gaps == (3, 4)
    )
    unknown = exclusion_for(
        comparable, RecordCompletenessStatus.UNKNOWN, [], include_visually_degraded=True
    )
    assert unknown is not None and unknown.reason is ExclusionReason.COMPLETENESS_UNKNOWN
    assert (
        exclusion_for(
            comparable, RecordCompletenessStatus.COMPLETE, [], include_visually_degraded=False
        )
        is None
    )

    by_default = exclusion_for(
        degraded, RecordCompletenessStatus.COMPLETE, [], include_visually_degraded=False
    )
    assert by_default is not None and by_default.reason is ExclusionReason.VISUALLY_DEGRADED
    assert (
        exclusion_for(
            degraded, RecordCompletenessStatus.COMPLETE, [], include_visually_degraded=True
        )
        is None
    )

    for override in (False, True):
        never = exclusion_for(
            not_comparable,
            RecordCompletenessStatus.COMPLETE,
            [],
            include_visually_degraded=override,
        )
        assert never is not None and never.reason is ExclusionReason.NOT_COMPARABLE
    assert degraded.comparability_status is ComparabilityStatus.VISUALLY_DEGRADED

    # R14 (2026-09-21): a gap-free, comparable record whose game turns never advanced is still
    # excluded, under its own reason -- the report must not call a stall a gap.
    stalled = exclusion_for(
        comparable,
        RecordCompletenessStatus.COMPLETE,
        [],
        include_visually_degraded=False,
        stalled_turns=[2, 3],
    )
    assert stalled is not None
    assert stalled.reason is ExclusionReason.GAME_TURN_DID_NOT_ADVANCE
    assert stalled.gaps == (2, 3)
