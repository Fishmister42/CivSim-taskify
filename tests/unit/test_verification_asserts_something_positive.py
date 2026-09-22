"""A verification predicate must say what the action MADE TRUE (T322).

Two things are pinned here, and they are different kinds of thing.

**The instance.** ``prompts.ai_diplomatic_approach`` verified with ``target not in
prompt.options`` -- a negative over a container, satisfied by success, by the prompt vanishing, by
the board moving on, and by the container simply emptying. Every reading below is quoted from
``civsim-match-store.db`` rather than invented, and the quotation is exact rather than
approximate: every one of this action's fourteen ``applied`` records has ``confirm_attempts: 1``
and ``confirm_elapsed_s: 0.0``, so the observation ``act.verify.confirm_execution`` actually
verified against is byte-for-byte the observation the run loop then persisted as the NEXT step's
``observation`` (``run/decision_loop.py``: ``next_observation = next_fresh.observation``).

**The class.** A rule telling future catalog authors to "assert something positive" would make
Principle I rest on every author remembering. The catalog rejects the shape at load time instead
(``capability/verification_shape.py``, wired into ``capability/loader.py``), and the tests at the
bottom drive that through the real shipped catalog plus a negative control -- because a guard whose
own test only ever feeds it good input encodes the author's fixtures rather than the contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.act.predicates import (
    PredicateEvaluationError,
    build_predicate_bindings,
    evaluate_predicate,
)
from civsim_harness.capability.loader import load_catalog
from civsim_harness.capability.verification_shape import (
    KNOWN_NEGATIVE_VERIFICATIONS,
    assert_no_stale_negative_verification_exceptions,
    assert_verification_asserts_something_positive,
    negative_only_branches,
)
from civsim_harness.errors import CatalogError
from civsim_harness.models.common import (
    CatalogVersionRef,
    DeclarationId,
    DecisionStepId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.turn import Observation, ObservationEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"

_ACTION = DeclarationId("prompts.ai_diplomatic_approach")
_GREETING_ACCEPT = "Would you like to visit our nearby city and sample our hospitality?"
_GREETING_DECLINE = "Thanks for the introduction, but we have no time for further pleasantries."


@pytest.fixture(scope="module")
def catalog() -> Any:
    return load_catalog(CATALOG_ROOT)


def _observation(screen_state: Mapping[str, Any]) -> Observation:
    return Observation(
        observation_id=ObservationId("obs-1"),
        decision_step_id=DecisionStepId("step-1"),
        assembled_at=datetime(2026, 9, 22, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="test", content_hash="deadbeef"),
        entries=[
            ObservationEntry(
                declaration_id=DeclarationId("game.screen_state"),
                key="game.screen_state",
                value=dict(screen_state),
                context=LuaContext.IN_GAME,
            )
        ],
        screen_identity=str(screen_state["screen"]),
    )


def _verifies(catalog: Any, *, target: str, after: Mapping[str, Any]) -> bool:
    declaration = catalog.declarations[_ACTION]
    assert declaration.verification_predicate is not None
    bindings = build_predicate_bindings(observation=_observation(after), target=target)
    return evaluate_predicate(declaration.verification_predicate, bindings)


# ---------------------------------------------------------------------------
# The live record, quoted
# ---------------------------------------------------------------------------

#: The conversation as it stood when each answer was dispatched.
_OFFERING_GOODBYE: dict[str, Any] = {
    "screen": "prompt.diplomatic_approach",
    "prompt_options": ["Goodbye"],
    "raw_screen_id": "DiplomacyActionView",
    "has_blocking_prompt": True,
    "recognized": True,
}
_OFFERING_FIRST_MEETING: dict[str, Any] = {
    "screen": "prompt.diplomatic_approach",
    "prompt_options": [_GREETING_ACCEPT, _GREETING_DECLINE],
    "raw_screen_id": "DiplomacyActionView",
    "has_blocking_prompt": True,
    "recognized": True,
}

#: THE DEFECT. The diplomacy context is still the one the engine reports; only the option list
#: emptied. 7 of the 14 `applied` records verified against exactly this (steps `d3a02162`,
#: `6305e45f`, `6692fb64`, `075e67c3`, `9171598e`, `6ea51298`, `c19febd8`).
_AFTER_OPTIONS_EMPTIED: dict[str, Any] = {
    "screen": "diplomacy",
    "prompt_options": [],
    "raw_screen_id": "DiplomacyActionView",
    "has_blocking_prompt": False,
    "recognized": True,
}

#: THE CONVERSATION ENDED. The engine's own context left `DiplomacyActionView` for `InGame`.
#: 5 of the 14 (steps `b9b7f026`, `e3178ba4`, `205e04d6`, `b2de619e`, `b9dffce9`).
_AFTER_WORLD_IS_BACK: dict[str, Any] = {
    "screen": "world",
    "prompt_options": [],
    "raw_screen_id": "InGame",
    "has_blocking_prompt": False,
    "recognized": True,
}

#: THE LEADER REPLIED. Still up, still blocking, offering a genuinely different set. 2 of the 14
#: (steps `f3d2b280` and `ee6ab6c1` -- the latter is block 37's first apply, 21:20:57.426362Z).
_AFTER_LEADER_REPLIED = dict(_OFFERING_GOODBYE)


def test_the_simulated_readings_really_are_the_production_condition() -> None:
    """Assert the simulation fired, before asserting anything about the predicate.

    ``.specify/memory/loop-state.md``: "If your test environment cannot reproduce the production
    condition, the obvious test passes vacuously -- simulate the condition explicitly AND assert
    the simulation fired." The condition is *the option list emptied while the engine's screen
    context did not change*. A fixture that quietly drifted to ``screen: "world"``, or to an
    ABSENT ``prompt_options`` rather than an empty one, would make the test below pass against the
    broken predicate for the wrong reason -- absence is a different defect, and ``d74a4c7`` closed
    that one.
    """
    assert "Goodbye" in _OFFERING_GOODBYE["prompt_options"], "target must be on offer at dispatch"
    assert "prompt_options" in _AFTER_OPTIONS_EMPTIED, "PRESENT, not absent (d74a4c7 covers absent)"
    assert _AFTER_OPTIONS_EMPTIED["prompt_options"] == [], "and EMPTY -- that is this defect"
    assert (
        _AFTER_OPTIONS_EMPTIED["raw_screen_id"] == _OFFERING_GOODBYE["raw_screen_id"]
    ), "the engine's screen context must be UNCHANGED across the action"
    assert _AFTER_WORLD_IS_BACK["raw_screen_id"] != _OFFERING_GOODBYE["raw_screen_id"], (
        "the positive control must vary exactly that dimension, or it proves nothing"
    )


def test_the_old_predicate_was_satisfied_by_the_options_merely_emptying() -> None:
    """The defect, reproduced against the retired expression itself.

    Kept as an executable statement of what was wrong, so the finding does not rest on prose.
    ``"Goodbye" not in []`` is legitimately ``True`` -- **an empty list is a value** -- and
    ``d74a4c7`` cannot reach it because ``_bind_prompt_namespace`` hard-defaults ``options`` to a
    real list, so no operand is ever absent.
    """
    bindings = build_predicate_bindings(
        observation=_observation(_AFTER_OPTIONS_EMPTIED), target="Goodbye"
    )
    assert evaluate_predicate("target not in prompt.options", bindings) is True
    assert bindings["prompt"]["options"] == [], "the right operand is a VALUE, never absent"


#: (name, target, post-execution reading, what the new predicate must say).
_LIVE_READINGS: Sequence[tuple[str, str, dict[str, Any], bool]] = (
    ("options_emptied_view_stayed", "Goodbye", _AFTER_OPTIONS_EMPTIED, False),
    ("conversation_ended", "Goodbye", _AFTER_WORLD_IS_BACK, True),
    ("conversation_ended_after_decline", _GREETING_DECLINE, _AFTER_WORLD_IS_BACK, True),
    ("leader_replied", _GREETING_ACCEPT, _AFTER_LEADER_REPLIED, True),
    ("answer_swallowed_options_identical", _GREETING_ACCEPT, _OFFERING_FIRST_MEETING, False),
)


@pytest.mark.parametrize(("name", "target", "after", "expected"), _LIVE_READINGS)
def test_the_predicate_reads_each_live_ending_correctly(
    catalog: Any, name: str, target: str, after: dict[str, Any], expected: bool
) -> None:
    """The falsifiable prediction, pinned.

    Replaying the fourteen ``applied`` records against the new predicate must reclassify the
    **7** that verified against an emptied list under an unchanged diplomacy context (``applied``
    -> ``rejected``), and must leave the **5** that reached the world and the **2** where the
    leader replied exactly as they were. A change that merely moved the number the other way, or
    moved every record, would not match the prediction and would not count.
    """
    assert _verifies(catalog, target=target, after=after) is expected


def test_a_different_prompt_taking_over_does_not_confirm_the_answer(catalog: Any) -> None:
    """Negative control on the branch that keeps the conversation open.

    ``prompt.is_active and target not in prompt.options`` on its own would be confirmed by ANY
    other blocking prompt appearing -- the tech/civic popup offers ``["continue"]``, which does
    not contain ``"Goodbye"`` either. The screen identity is pinned in the same branch precisely
    so that a different screen cannot stand in for the leader's reply.
    """
    other_prompt = {
        "screen": "prompt.tech_civic_completed",
        "prompt_options": ["continue"],
        "raw_screen_id": "TechCivicCompletedPopup",
        "has_blocking_prompt": True,
        "recognized": True,
    }
    assert _verifies(catalog, target="Goodbye", after=other_prompt) is False


# ---------------------------------------------------------------------------
# The class: absence surviving a bare `not`
# ---------------------------------------------------------------------------


def test_negating_an_absent_field_is_unevaluable_not_success() -> None:
    """``not <absent field>`` used to read ``True`` -- the fabricating direction.

    ``_refuse_unresolved_operand`` (T314/``d74a4c7``) guards ``ast.Compare`` only, so
    ``not (target in X)`` was covered by the ``in`` inside it while a BARE field read was not:
    ``not great_person.is_recruitable`` is ``UnaryOp(Not, Attribute)`` with no comparison anywhere,
    and ``not None`` is ``True``. Found by enumerating the shape over every shipped predicate, not
    by chasing an instance.
    """
    bindings = build_predicate_bindings(observation=_observation(_AFTER_WORLD_IS_BACK))
    with pytest.raises(PredicateEvaluationError) as raised:
        evaluate_predicate("not great_person.is_recruitable", bindings)
    assert "not a value to negate" in raised.value.message


def test_negating_a_field_that_is_present_still_works() -> None:
    """Positive control for the guard above: a field that is genuinely ``False`` still negates to
    ``True``, and one that is genuinely ``True`` still negates to ``False``. Refusing absence must
    not cost a decided answer."""
    assert evaluate_predicate("not game.recognized", {"game": {"recognized": False}}) is True
    assert evaluate_predicate("not game.recognized", {"game": {"recognized": True}}) is False


def test_an_explicit_null_may_still_be_negated_on_purpose() -> None:
    """The carve-out the comparison guard already has: an author writing ``null`` is asking about
    absence deliberately, and the new unary guard must not take that idiom away."""
    assert evaluate_predicate("not null", {}) is True


# ---------------------------------------------------------------------------
# The class: rejected at load time
# ---------------------------------------------------------------------------


def _verification_predicates(catalog: Any) -> dict[DeclarationId, str]:
    return {
        declaration_id: declaration.verification_predicate
        for declaration_id, declaration in catalog.declarations.items()
        if declaration.verification_predicate is not None
    }


def test_the_real_catalog_loads_and_every_action_is_checked(catalog: Any) -> None:
    """A positive control for the check itself: the shipped catalog must still load, and the
    check must actually have something to run against. A silently empty declaration set would make
    every assertion below vacuously true."""
    predicates = _verification_predicates(catalog)
    assert len(predicates) >= 40, f"only {len(predicates)} action predicates -- catalog not loaded"


def test_the_fixed_declaration_no_longer_needs_an_exception(catalog: Any) -> None:
    """The action this pass fixes must pass the structural check on its own merits, not by being
    listed as a known offender."""
    assert _ACTION not in KNOWN_NEGATIVE_VERIFICATIONS
    assert negative_only_branches(_verification_predicates(catalog)[_ACTION]) == ()


def test_every_shipped_action_either_asserts_something_positive_or_is_a_listed_exception(
    catalog: Any,
) -> None:
    """The ratchet. Adding a declaration whose verification a no-op satisfies fails here (and at
    catalog load), unless the author also adds it to ``KNOWN_NEGATIVE_VERIFICATIONS`` with a
    reason and a failure direction -- a diff a reviewer sees."""
    for declaration_id, predicate in _verification_predicates(catalog).items():
        assert_verification_asserts_something_positive(
            declaration_id=declaration_id, verification_predicate=predicate
        )


def test_no_exception_outlives_the_shape_it_excuses(catalog: Any) -> None:
    """An exception that is no longer needed is how a table like this rots into a blanket
    permission -- the same staleness discipline ``KNOWN_PHANTOM_PREDICATE_FIELDS`` applies."""
    assert_no_stale_negative_verification_exceptions(_verification_predicates(catalog))


@pytest.mark.parametrize(
    ("predicate", "is_bare_negative"),
    (
        # The retired expression, and its relatives across the catalog.
        ("target not in prompt.options", True),
        ("not (target in player.available_policies)", True),
        ("not great_person.is_recruitable", True),
        ('game.current_screen != "prompt.era_dedication"', True),
        ('other_player.diplomatic_state != "war"', True),
        # Positive: a state was reached.
        ('player.current_government == target', False),
        ("target in city.production_queue", False),
        ("city.is_selected", False),
        ("player.diplomatic_favor < observed_diplomatic_favor", False),
        # Positive by carve-out: an explicit `null` is a presence test, and a `!=` against an
        # `observed_*` snapshot is a change against a recorded baseline rather than a literal.
        ("spy.mission != null", False),
        ("prompt.options != observed_prompt_options", False),
        # Every `or` branch must stand on its own -- one good branch does not excuse a bare one.
        ('game.current_screen == "world" or target not in prompt.options', True),
        (
            '(game.current_screen == "world" and game.raw_screen_id == "InGame")'
            ' or (prompt.is_active and target in prompt.options)',
            False,
        ),
    ),
)
def test_the_shape_check_classifies_each_form(predicate: str, is_bare_negative: bool) -> None:
    """The negative control the guard needs to be worth anything: it is fed forms it MUST reject
    and forms it MUST accept, varying the dimension the rule constrains (does this expression
    assert that something became true?) rather than only the expression's length or namespace."""
    assert bool(negative_only_branches(predicate)) is is_bare_negative


def test_a_bare_negative_declaration_cannot_be_loaded(tmp_path: Path) -> None:
    """End to end, through the real loader: the catalog REFUSES the shape rather than reporting
    it. This is the difference between a rule and a control -- and it is asserted against a
    declaration that is otherwise entirely well-formed, so the refusal can only be about shape."""
    catalogs = tmp_path / "catalogs"
    (catalogs / "actions").mkdir(parents=True)
    (catalogs / "observations").mkdir()
    (catalogs / "VERSION").write_text("test.1\n", encoding="utf-8")
    (catalogs / "capabilities.yaml").write_text(
        "- capability_id: prompts.orders\n"
        "  path: firetuner\n"
        "  implementation_ref: lua/ingame/screens.lua\n"
        "  reads: [the open prompt]\n"
        "  writes: [prompt responses]\n",
        encoding="utf-8",
    )
    (catalogs / "actions" / "bad.yaml").write_text(
        "- declaration_id: prompts.made_up\n"
        "  kind: action\n"
        "  summary: A made-up prompt answer.\n"
        "  parity_basis: A human clicks the button that is on the screen.\n"
        "  context: InGame\n"
        "  capability_id: prompts.orders\n"
        "  availability_predicate: target in prompt.options\n"
        "  verification_predicate: target not in prompt.options\n"
        "  target_kind: option\n"
        "  target_hint: one of the offered options\n"
        '  introduced_in_version: "2026.09.20"\n',
        encoding="utf-8",
    )
    with pytest.raises(CatalogError) as raised:
        load_catalog(catalogs)
    assert "satisfiable by a no-op" in str(raised.value)
