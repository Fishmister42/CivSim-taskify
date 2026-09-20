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

from datetime import datetime
from typing import Any

from civsim_harness.act.verify import ExecutionVerification, observed_snapshot, verify_execution
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.decision import ExecutionOutcome, RejectionReason
from civsim_harness.models.turn import Observation, ObservationEntry, StepProgress

VERIFIED_AT = datetime(2026, 1, 1, 12, 0, 0)


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
    # Mirrors the real turn.end_turn finding (tests/unit/test_predicates.py): a predicate using
    # arithmetic must never silently pass evaluation.
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

    assert result.execution.outcome is ExecutionOutcome.REJECTED
    assert result.execution.rejection_reason is RejectionReason.VERIFICATION_FAILED
    assert result.progress is StepProgress.REJECTED


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
