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
    record = make_turn_cycle_record(
        "r", 3, yields={"science": 4, "culture": 2.5, "flag": True, "name": "x"}, cities=2, units=5
    )
    assert turn_metrics(record) == {
        "science": 4.0,
        "culture": 2.5,
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
