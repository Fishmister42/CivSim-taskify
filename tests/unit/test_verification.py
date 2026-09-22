"""Unit tests for verification-derived execution outcomes (T108).

Covers :func:`~civsim_harness.act.verify.verify_execution`'s derivation of
:class:`~civsim_harness.models.decision.ActionExecution` and
:class:`~civsim_harness.models.turn.StepProgress` from a declaration's ``verification_predicate``
evaluated against pre/post observations -- never asserted by a caller. Also covers
:func:`~civsim_harness.act.verify.observed_snapshot`, and, together with
:mod:`tests.unit.test_predicates`'s real-catalog finding, documents the concrete consequence for
``turn.end_turn`` for whoever picks up that grammar/catalog mismatch: its verification predicate
cannot be evaluated by this module either, and this module's own "unevaluable is never a free pass
to applied" rule means that surfaces as ``rejected``, not a crash and not a false ``applied``.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from civsim_harness.act.verify import ExecutionVerification, observed_snapshot, verify_execution
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.decision import ActionExecution, ExecutionOutcome, RejectionReason
from civsim_harness.models.turn import Observation, ObservationEntry, StepProgress

VERIFIED_AT = datetime(2026, 1, 1, 12, 0, 0)

_HARNESS_ROOT = Path(__file__).resolve().parents[2] / "src" / "civsim_harness"


def _action_declaration(
    *, declaration_id: str = "demo.do_thing", verification_predicate: str
) -> ParityDeclaration:
    return ParityDeclaration(
        declaration_id=declaration_id,
        kind=DeclarationKind.ACTION,
        summary="Do the thing.",
        parity_basis="Click the thing button.",
        context=LuaContext.IN_GAME,
        capability_id="demo.cap",
        availability_predicate="game.is_local_player_turn",
        verification_predicate=verification_predicate,
        introduced_in_version="2026.09.1",
    )


def _entry(declaration_id: str, value: Any) -> ObservationEntry:
    return ObservationEntry(
        declaration_id=declaration_id, key=declaration_id, value=value, context=LuaContext.IN_GAME
    )


def _observation(entries: list[ObservationEntry], observation_id: str = "obs") -> Observation:
    return Observation(
        observation_id=ObservationId(observation_id),
        decision_step_id=DecisionStepId("step-1"),
        assembled_at=VERIFIED_AT,
        catalog_version=CatalogVersionRef(version="2026.09.1", content_hash="deadbeef"),
        entries=entries,
        captures=[],
        screen_identity="world",
    )


def _unit_at(unit_id: int, x: int, y: int) -> Observation:
    unit = {"unit_id": unit_id, "plot": {"x": x, "y": y}}
    return _observation([_entry("units.state", {"units": [unit]})])


# --------------------------------------------------------------------------
# The core True/False derivation
# --------------------------------------------------------------------------


#: Deliberately does not reference bare ``target`` (see ``act.predicates``'s own documented
#: simplification: `target` is used to select a unit/city subject *and* would be bound as the
#: literal `target` value in the same breath, which only coincide for namespaces where the
#: catalog's own `target` genuinely *is* the subject id -- not units/cities). Comparing
#: `unit.<field>` against a literal keeps these tests focused on verification-outcome derivation
#: rather than on that separate, already-documented gap.
_MOVED_TO_3_4 = "unit.plot.x == 3 and unit.plot.y == 4"


def test_verification_true_derives_applied_and_changed_state() -> None:
    declaration = _action_declaration(verification_predicate=_MOVED_TO_3_4)
    post = _unit_at(1, 3, 4)
    pre = _unit_at(1, 0, 0)

    result = verify_execution(
        declaration=declaration,
        pre_observation=pre,
        post_observation=post,
        target=1,
        verified_at=VERIFIED_AT,
    )

    assert isinstance(result, ExecutionVerification)
    assert result.execution.outcome is ExecutionOutcome.APPLIED
    assert result.execution.rejection_reason is None
    assert result.progress is StepProgress.CHANGED_STATE
    assert result.execution.verified_at == VERIFIED_AT


def test_verification_false_derives_rejected_with_verification_failed_reason() -> None:
    # unit 1's plot never became the requested target -- the order was swallowed.
    declaration = _action_declaration(verification_predicate=_MOVED_TO_3_4)
    post = _unit_at(1, 0, 0)
    pre = _unit_at(1, 0, 0)

    result = verify_execution(
        declaration=declaration,
        pre_observation=pre,
        post_observation=post,
        target=1,
        verified_at=VERIFIED_AT,
    )

    assert result.execution.outcome is ExecutionOutcome.REJECTED
    assert result.execution.rejection_reason is RejectionReason.VERIFICATION_FAILED
    assert result.progress is StepProgress.REJECTED
    assert result.execution.verification["result"] is False


def test_verification_never_asserts_applied_from_the_executor() -> None:
    # Even though *this test* believes the action "worked", the outcome must come only from the
    # declared predicate re-evaluated against post_observation -- there is no parameter on
    # verify_execution that lets a caller assert an outcome directly.
    declaration = _action_declaration(verification_predicate=_MOVED_TO_3_4)
    post = _unit_at(1, 9, 9)  # the unit actually ended up somewhere else entirely
    pre = post

    result = verify_execution(
        declaration=declaration,
        pre_observation=pre,
        post_observation=post,
        target=1,
        verified_at=VERIFIED_AT,
    )

    assert result.execution.outcome is ExecutionOutcome.REJECTED
    assert result.progress is StepProgress.REJECTED


# --------------------------------------------------------------------------
# Unevaluable predicates: never a free pass to applied (fail closed)
# --------------------------------------------------------------------------


def test_unevaluable_verification_predicate_is_rejected_not_applied() -> None:
    declaration = _action_declaration(verification_predicate="player.gold > 0")
    empty = _observation([])  # no research/government/religion entries -> player.gold unbound

    result = verify_execution(
        declaration=declaration,
        pre_observation=empty,
        post_observation=empty,
        verified_at=VERIFIED_AT,
    )

    assert result.execution.outcome is ExecutionOutcome.REJECTED
    assert result.execution.rejection_reason is RejectionReason.VERIFICATION_FAILED
    assert result.progress is StepProgress.REJECTED
    assert "could not be evaluated" in result.execution.verification["reason"]


def test_disallowed_grammar_in_verification_predicate_is_rejected_not_applied() -> None:
    # A predicate the evaluator cannot handle must REJECT, never silently pass as applied --
    # a weak or unevaluable predicate would both mis-record the action and disable the
    # no-progress backstop (FR-011, FR-014, invariant I4).
    #
    # `+`/`-` on numeric operands ARE permitted (catalogs/README.md section 4); `*` is not.
    # The operands here are chosen so the arithmetic would evaluate TRUE if multiplication
    # were allowed (5 * 2 == 10), so this proves the construct is refused even when refusing
    # costs a passing result -- failing closed rather than failing convenient.
    declaration = _action_declaration(
        verification_predicate="game.turn_number == observed_turn_number * 2"
    )
    post = _observation([_entry("game.turn_state", {"turn_number": 10})])
    pre = _observation([_entry("game.turn_state", {"turn_number": 5})])

    result = verify_execution(
        declaration=declaration,
        pre_observation=pre,
        post_observation=post,
        verified_at=VERIFIED_AT,
    )

    assert result.execution.outcome is ExecutionOutcome.REJECTED
    assert result.execution.rejection_reason is RejectionReason.VERIFICATION_FAILED
    assert result.progress is StepProgress.REJECTED


def test_permitted_arithmetic_in_verification_predicate_applies() -> None:
    # The companion to the test above: `+ 1` is legal grammar, and the real
    # turn.end_turn predicate depends on it. If this ever starts rejecting, the harness
    # can no longer verify that a turn ended -- see tests/unit/test_predicates.py, which
    # binds the same property to the actual catalogs/actions/turn.yaml declaration.
    declaration = _action_declaration(
        verification_predicate="game.turn_number == observed_turn_number + 1"
    )
    post = _observation([_entry("game.turn_state", {"turn_number": 6})])
    pre = _observation([_entry("game.turn_state", {"turn_number": 5})])

    result = verify_execution(
        declaration=declaration,
        pre_observation=pre,
        post_observation=post,
        verified_at=VERIFIED_AT,
    )

    assert result.execution.outcome is ExecutionOutcome.APPLIED
    assert result.progress is StepProgress.CHANGED_STATE


# --------------------------------------------------------------------------
# observed_* snapshot plumbing
# --------------------------------------------------------------------------


def test_observed_snapshot_flattens_pre_observation_turn_number() -> None:
    pre = _observation([_entry("game.turn_state", {"turn_number": 5})])
    snapshot = observed_snapshot(pre)
    assert snapshot == {"observed_turn_number": 5}


def test_observed_snapshot_flattens_diplomatic_favor_from_congress_state() -> None:
    congress_state = {"is_in_session": True, "local_player_favor": 7, "active_resolutions": []}
    pre = _observation([_entry("congress.state", congress_state)])
    snapshot = observed_snapshot(pre)
    assert snapshot == {"observed_diplomatic_favor": 7}


def test_verification_predicate_can_reference_observed_snapshot() -> None:
    # A grammar-compliant, arithmetic-free stand-in for turn.end_turn's own intent: confirm the
    # turn number changed at all (rather than by exactly +1, which needs arithmetic).
    declaration = _action_declaration(
        verification_predicate="game.turn_number != observed_turn_number"
    )
    pre = _observation([_entry("game.turn_state", {"turn_number": 5})])
    post = _observation([_entry("game.turn_state", {"turn_number": 6})])

    result = verify_execution(
        declaration=declaration, pre_observation=pre, post_observation=post, verified_at=VERIFIED_AT
    )

    assert result.execution.outcome is ExecutionOutcome.APPLIED
    assert result.progress is StepProgress.CHANGED_STATE


# --------------------------------------------------------------------------
# FR-011 enforced rather than merely obeyed: the record refuses an unverified
# `applied`, and exactly one module in the harness may derive one.
# --------------------------------------------------------------------------


def test_an_applied_execution_cannot_be_recorded_without_a_verification() -> None:
    """FR-011, the model half: "applied" with an empty verification will not validate.

    The bypass this closes is concrete and this repo has shipped its twin before (defect #1 in
    `tests/contract/test_reachability.py`): a fast path that answers `applied` the moment the
    dispatch Lua returns `{ok=true}`, skipping the post-observation re-read entirely. Every
    behavioural test above would still pass under that shape -- they drive `verify_execution`,
    and the bypass would live beside it -- because "the outcome came from the predicate" is a
    property of *how the code is written*, not of any value those tests can inspect. Pushing the
    rule into `ActionExecution` makes the unverified record unconstructible instead.
    """
    with pytest.raises(ValidationError) as excinfo:
        ActionExecution(
            outcome=ExecutionOutcome.APPLIED, verification={}, verified_at=VERIFIED_AT
        )
    assert "empty verification" in str(excinfo.value)


def test_an_applied_execution_cannot_be_recorded_over_a_failed_verification() -> None:
    """The subtler half: a verification is *present* but did not confirm anything.

    Non-empty alone is too weak a gate -- a fast path could attach the dispatch answer (`{"ok":
    True}`) or the declaration id and satisfy it while never re-reading the board. `applied`
    must carry the predicate's own `True`.
    """
    for not_confirmed in ({"result": False}, {"result": None}, {"declaration_id": "demo.thing"}):
        with pytest.raises(ValidationError) as excinfo:
            ActionExecution(
                outcome=ExecutionOutcome.APPLIED,
                verification=dict(not_confirmed),
                verified_at=VERIFIED_AT,
            )
        assert "verification['result'] is True" in str(excinfo.value)


def test_a_rejected_execution_is_unaffected_by_the_applied_rule() -> None:
    """The rule is one-sided on purpose: a rejection records `result: False` and must stay legal."""
    execution = ActionExecution(
        outcome=ExecutionOutcome.REJECTED,
        rejection_reason=RejectionReason.VERIFICATION_FAILED,
        verification={"result": False, "reason": "the click was swallowed"},
        verified_at=VERIFIED_AT,
    )
    assert execution.outcome is ExecutionOutcome.REJECTED


#: The one module allowed to derive `ExecutionOutcome.APPLIED`, relative to `src/civsim_harness`.
#: `models/decision.py` defines the member and is excluded by the check itself (an enum member's
#: own `APPLIED = "applied"` assignment is not a call keyword).
_SOLE_APPLIED_PRODUCER = Path("act") / "verify.py"


def _references_applied(node: ast.expr) -> bool:
    return any(
        isinstance(sub, ast.Attribute)
        and sub.attr == "APPLIED"
        and isinstance(sub.value, ast.Name)
        and sub.value.id == "ExecutionOutcome"
        for sub in ast.walk(node)
    )


def _modules_producing_an_applied_outcome() -> list[str]:
    """Every harness module with a call passing ``outcome=<...ExecutionOutcome.APPLIED>``.

    The construction signature shared by `ActionExecution(...)` and any local builder wrapping
    it. An AST scan rather than a text scan, for the reason
    `tests/contract/test_read_only_boundary.py` and T231's check in
    `tests/contract/test_parity_redteam.py` both give: several modules *name* the member to
    document what they must not assert, and a text match would flag exactly the files whose job
    is to be explicit about the boundary. Comparing against it (`outcome is
    ExecutionOutcome.APPLIED`, as `run/turn_cycle.py` and `store/coverage.py` do) is reading,
    not producing, and is deliberately not counted -- the same loads-are-not-callers reasoning
    as `tests/contract/test_reachability.py`. Qualified by `ExecutionOutcome.` so
    `run/preparation.py`'s unrelated `LeaderSelectionWriteStatus.APPLIED` cannot register.
    """
    producers: list[str] = []
    for path in sorted(_HARNESS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if any(
                keyword.arg == "outcome" and _references_applied(keyword.value)
                for keyword in node.keywords
            ):
                producers.append(path.relative_to(_HARNESS_ROOT).as_posix())
                break
    return producers


def test_exactly_one_module_in_the_harness_derives_an_applied_outcome() -> None:
    """FR-011, the structural half: one place may say "applied", and it is the verifier.

    The model validator above can prove an `applied` record carries a confirming verification;
    it cannot prove that verification came from re-reading the board rather than from a
    plausible-looking dict an executor assembled. Only the shape of the code can, and the shape
    is: `act/verify.py` builds `applied` inside `if confirmed:`, where `confirmed` is
    `evaluate_predicate(declaration.verification_predicate, bindings)` against the *post*-action
    observation. `act/dispatch.py` -- the other `ActionExecution` producer -- only ever builds
    `rejected`.

    Asserted structurally rather than behaviourally for T231's reason: a second producer that
    happens to agree with the first on every input is invisible to any behavioural test, and the
    danger is precisely that a reviewer hardening FR-011 would harden whichever copy the real
    decisions do not travel through.
    """
    producers = _modules_producing_an_applied_outcome()
    assert producers == [_SOLE_APPLIED_PRODUCER.as_posix()], (
        "every applied ExecutionOutcome in the harness must be derived by act/verify.py, from "
        "the action's own declared predicate re-evaluated against the post-action observation; "
        "a second producer is how an action gets recorded as applied without verification "
        f"(FR-011). Producers found: {producers}"
    )
