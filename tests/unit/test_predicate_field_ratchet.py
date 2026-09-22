"""The phantom-predicate-field ratchet (T306).

`diplomacy.declare_war`'s `availability_predicate`/`verification_predicate` both read
`other_player.diplomatic_state` -- a name `catalogs/observations/diplomacy.yaml`'s own
`output_schema` declares, but `lua/gamecore/diplomacy.lua` is confirmed never to actually put in
the emitted JSON (see `civsim_harness.act.predicates`'s own header comment above
`KNOWN_SCHEMA_BODY_DISAGREEMENTS` for the full, cited derivation: `Player:GetDiplomaticAI()`
MEASURED absent in `GameCore_Tuner`, the GameCore-side fallback UNVERIFIED, and empirically -- 670
real captured `decision_steps.bundle_json` rows from today's live runs, including relations for a
met civilization -- the key union of every `diplomacy.state.relations[]` entry ever actually
captured is exactly `{player_id, has_met, civilization, has_delegation}`, never
`diplomatic_state`). Because `civsim_harness.act.predicates._resolve_attribute` resolves an absent
key to `None`/`False` rather than raising (deliberately -- so a live availability/verification
check reads as an ordinary "not available" rather than crashing), this defect shape is invisible
at runtime: `evaluate_predicate` never raises for it, and a `declare_war` that actually lands is
recorded `rejected` forever.

This module is the static ratchet that makes that defect shape impossible to reintroduce
unnoticed, across every declared action, not just diplomacy: every `kind: action` declaration in
the real catalog, not named in `civsim_harness.act.predicates.KNOWN_PHANTOM_PREDICATE_FIELDS`
(each entry there is a reviewed, already-reported finding from the T306 sweep -- see that table's
own comments), must have every identifier its `availability_predicate`/`verification_predicate`
reference resolve to a field some observation body actually emits, per
`civsim_harness.act.predicates.known_predicate_fields` -- itself derived mechanically from the
loaded catalog's own declared `output_schema`s through the exact tables the runtime evaluator
binds from, not hand-typed.

`test_a_synthetic_declaration_referencing_a_field_nobody_emits_fails_the_ratchet` is the negative
control this ratchet is required to have: it proves, by actually invoking
`assert_predicate_fields_resolve` against a declaration built to be wrong (not in the exception
table), that the ratchet can fail -- "a check nobody has proven can fail is not a check".

The trailing two tests cover the other half of the T306 sweep -- `tests/live/goals/*.yaml`'s
`success`/`prerequisites` predicates, which use a different grammar and already have their own
load-time field-resolution gate (`live.goal_run.validate_predicate`); this file exercises it and
gives it the negative control it did not previously have, rather than building a second, duplicate
mechanism.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from civsim_harness.act.predicates import (
    KNOWN_PHANTOM_PREDICATE_FIELDS,
    DeclarationId,
    assert_predicate_fields_resolve,
    known_predicate_fields,
)
from civsim_harness.capability.loader import load_catalog
from civsim_harness.models.catalog import DeclarationKind

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"

if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))


@pytest.fixture(scope="module")
def catalog():
    return load_catalog(CATALOG_ROOT)


@pytest.fixture(scope="module")
def fields(catalog):
    return known_predicate_fields(catalog.declarations)


def _action_declarations(catalog):
    return [
        declaration
        for declaration in catalog.declarations.values()
        if declaration.kind is DeclarationKind.ACTION
    ]


def test_every_declared_action_predicate_field_resolves(catalog, fields) -> None:
    """Every `kind: action` declaration in the real, shipped catalog -- except the reviewed
    exceptions in `KNOWN_PHANTOM_PREDICATE_FIELDS` -- has every identifier its
    availability/verification predicates reference resolve to a field some observation body
    actually emits."""
    declarations = _action_declarations(catalog)
    assert len(declarations) >= 40, (
        f"only {len(declarations)} action declarations found under {CATALOG_ROOT} -- "
        "the catalog root or discovery glob may be wrong; this ratchet checks nothing if the "
        "list it iterates is empty or truncated"
    )
    failures: list[str] = []
    for declaration in declarations:
        try:
            assert_predicate_fields_resolve(
                declaration_id=declaration.declaration_id,
                availability_predicate=declaration.availability_predicate,
                verification_predicate=declaration.verification_predicate,
                known_fields=fields,
            )
        except AssertionError as exc:
            failures.append(str(exc))
    assert not failures, "\n".join(failures)


def test_the_known_phantom_predicate_fields_are_exactly_these_and_no_others(catalog) -> None:
    """Pins `KNOWN_PHANTOM_PREDICATE_FIELDS`'s keys so a new exception cannot be added silently --
    the same staleness discipline `live.goal_run`'s
    `test_the_known_availability_exceptions_are_exactly_these_and_no_others` already applies to
    goals. Growing this set is a diff a reviewer sees, in this file, not an accident."""
    assert set(KNOWN_PHANTOM_PREDICATE_FIELDS) == {
        DeclarationId("diplomacy.declare_war"),
        DeclarationId("diplomacy.make_peace"),
        DeclarationId("camera.move"),
        DeclarationId("camera.zoom"),
        DeclarationId("camera.set_view_mode"),
    }
    # And every key must still actually be a declared action -- an exception for a declaration
    # that was renamed or removed is a stale exception, silently excusing nothing real.
    declared_ids = {d.declaration_id for d in _action_declarations(catalog)}
    assert set(KNOWN_PHANTOM_PREDICATE_FIELDS) <= declared_ids


def test_declare_war_and_make_peace_are_exactly_the_diplomatic_state_phantom(fields) -> None:
    """Names the specific proven defect this whole ratchet exists to catch: both predicates read
    `other_player.diplomatic_state`, a field no observation body emits."""
    assert "diplomatic_state" not in fields["other_player"]
    assert KNOWN_PHANTOM_PREDICATE_FIELDS[DeclarationId("diplomacy.declare_war")] == frozenset(
        {("other_player", "diplomatic_state")}
    )
    assert KNOWN_PHANTOM_PREDICATE_FIELDS[DeclarationId("diplomacy.make_peace")] == frozenset(
        {("other_player", "diplomatic_state")}
    )


def test_camera_namespace_is_never_wired_so_nothing_resolves_against_it(fields) -> None:
    """`build_predicate_bindings` hardcodes `camera: {}` -- confirms the ratchet's own account of
    why the three camera actions are in the exception table, from the published data itself."""
    assert fields["camera"] == frozenset()


def test_a_synthetic_declaration_referencing_a_field_nobody_emits_fails_the_ratchet(fields) -> None:
    """THE NEGATIVE CONTROL. `other_player.frobnicate_the_treaty` is not a real field, this
    synthetic declaration id is not in `KNOWN_PHANTOM_PREDICATE_FIELDS`, and the assertion must
    therefore raise -- proving the ratchet can fail, and that its failure message says the useful
    thing: that the guarded action will be recorded as refused even when it actually works.
    """
    with pytest.raises(AssertionError) as excinfo:
        assert_predicate_fields_resolve(
            declaration_id=DeclarationId("synthetic.never_declared_action"),
            availability_predicate="other_player.has_met and not other_player.frobnicate_the_treaty",
            verification_predicate="other_player.frobnicate_the_treaty",
            known_fields=fields,
        )
    message = str(excinfo.value)
    assert "other_player.frobnicate_the_treaty" in message
    assert (
        "a field no observation body emits can never be true, so the action it guards will be "
        "recorded as refused even when it works" in message
    )


def test_a_synthetic_declaration_with_only_real_fields_passes(fields) -> None:
    """The positive twin of the negative control: a synthetic declaration built entirely out of
    real, resolving fields must NOT raise -- proving the ratchet does not simply always fail."""
    assert_predicate_fields_resolve(
        declaration_id=DeclarationId("synthetic.always_resolves"),
        availability_predicate="other_player.has_met and not other_player.has_delegation",
        verification_predicate="other_player.has_delegation",
        known_fields=fields,
    )


# --------------------------------------------------------------------------
# The other half of the sweep: `tests/live/goals/*.yaml`'s `success`/`prerequisites` predicates,
# which use a different grammar (`live.goal_run`'s dotted fact tree, not the action-catalog
# namespaces above) and already have their own load-time field-resolution gate
# (`live.goal_run.validate_predicate`, checked against `known_fact_names`) -- exercised here
# rather than duplicated, per the same "prove it can fail" discipline.
# --------------------------------------------------------------------------


def test_every_shipped_goal_predicate_field_resolves() -> None:
    """All 13 goals under `tests/live/goals/` load without a `GoalError` -- i.e. every identifier
    every goal's `success`/`prerequisites` predicate references resolves against
    `live.goal_run.known_fact_names` (itself derived from `derive_facts`, which is built only from
    fields `catalogs/observations/*.yaml` declarations actually carry -- see that function's own
    docstring). This is the goal-side half of the T306 sweep: it found zero phantom identifiers,
    unlike the action-catalog side, which found five (see `KNOWN_PHANTOM_PREDICATE_FIELDS`)."""
    from live.goal_run import load_goals

    goals = load_goals()
    assert len(goals) == 13, f"expected 13 shipped goals, found {len(goals)}"


def test_negative_control_a_goal_predicate_referencing_an_unknown_fact_fails() -> None:
    """THE NEGATIVE CONTROL for the goal-predicate side. `live.goal_run.validate_predicate` is the
    mechanism `load_goals`/`parse_goal` already run at goal-load time; this proves it can actually
    fail, by handing it a predicate naming a fact `derive_facts` does not produce."""
    from live.goal_run import GoalError, validate_predicate

    with pytest.raises(GoalError, match="unknown fact"):
        validate_predicate("player.frobnicate_the_treaty >= 1", label="synthetic")
