"""Unit tests for decision response schema and parsing (T104).

P4 / P10: malformed output, multi-decision output, and decision-free output
are each a failed call, never a decision to do nothing. Only multi-decision
output raises -- everything else that carries no usable decision returns
``None`` so the caller can map it to ``CallOutcome.EMPTY_RESPONSE``.
"""

from __future__ import annotations

import json

import pytest

from civsim_harness.agent.decisions import RESPONSE_SCHEMA, MultipleDecisionsError, parse_decision
from civsim_harness.provider.port import RawDecision

_VALID_DECISION = {
    "action_declaration_id": "units.move_to",
    "reasoning": "Scout the nearby hills for a second city site.",
    "parameters": {"unit_id": "unit-1", "x": 4, "y": 7},
}


# --------------------------------------------------------------------------
# The happy path: exactly one decision, carrying its stated reasoning
# --------------------------------------------------------------------------


def test_parses_single_valid_decision() -> None:
    decision = parse_decision(json.dumps(_VALID_DECISION))
    assert decision == RawDecision(
        action_declaration_id="units.move_to",
        reasoning="Scout the nearby hills for a second city site.",
        parameters={"unit_id": "unit-1", "x": 4, "y": 7},
        is_end_turn=False,
        prompt_type=None,
    )


def test_parses_end_turn_decision_with_prompt_type() -> None:
    content = json.dumps(
        {
            "action_declaration_id": "turn.end_turn",
            "reasoning": "Nothing more to do this turn.",
            "is_end_turn": True,
            "prompt_type": None,
        }
    )
    decision = parse_decision(content)
    assert decision is not None
    assert decision.is_end_turn is True
    assert decision.action_declaration_id == "turn.end_turn"


def test_single_element_array_is_accepted_as_one_decision() -> None:
    """A model that wraps its one answer in a list is still exactly one decision."""
    decision = parse_decision(json.dumps([_VALID_DECISION]))
    assert decision is not None
    assert decision.action_declaration_id == "units.move_to"


def test_defaults_are_applied_for_optional_fields() -> None:
    minimal = {"action_declaration_id": "units.move_to", "reasoning": "Move north."}
    decision = parse_decision(json.dumps(minimal))
    assert decision is not None
    assert decision.parameters == {}
    assert decision.is_end_turn is False
    assert decision.prompt_type is None


# --------------------------------------------------------------------------
# Decision-free and malformed output -> None (a failed call, per P4), never
# a synthesized no-op decision.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   ",
        None,
        "null",
        "{}",
        "[]",
        "not json at all",
        "{",
        json.dumps({"action_declaration_id": "units.move_to"}),  # missing reasoning
        json.dumps({"reasoning": "no action named"}),  # missing action_declaration_id
        json.dumps({"action_declaration_id": "", "reasoning": "blank id"}),
        json.dumps({"action_declaration_id": "units.move_to", "reasoning": ""}),
        json.dumps({"action_declaration_id": 5, "reasoning": "wrong type"}),
        json.dumps(
            {
                "action_declaration_id": "units.move_to",
                "reasoning": "ok",
                "parameters": "not-an-object",
            }
        ),
        json.dumps(
            {
                "action_declaration_id": "units.move_to",
                "reasoning": "ok",
                "is_end_turn": "yes",
            }
        ),
        json.dumps(
            {
                "action_declaration_id": "units.move_to",
                "reasoning": "ok",
                "prompt_type": 7,
            }
        ),
        json.dumps("just a string"),
        json.dumps(42),
    ],
)
def test_malformed_and_decision_free_output_returns_none(content: str | None) -> None:
    assert parse_decision(content) is None


# --------------------------------------------------------------------------
# Multi-decision output: a contract violation the caller must not reconcile
# --------------------------------------------------------------------------


def test_multiple_decisions_raises_and_does_not_return_a_value() -> None:
    content = json.dumps([_VALID_DECISION, {**_VALID_DECISION, "reasoning": "second one"}])
    with pytest.raises(MultipleDecisionsError):
        parse_decision(content)


def test_multiple_decisions_error_reports_count_in_detail() -> None:
    content = json.dumps([_VALID_DECISION] * 3)
    with pytest.raises(MultipleDecisionsError) as excinfo:
        parse_decision(content)
    assert excinfo.value.detail.get("decision_count") == 3


def test_multiple_decisions_are_neither_dropped_nor_merged() -> None:
    """Guards against a 'helpful' refactor that catches the array and picks the first entry."""
    content = json.dumps(
        [
            {**_VALID_DECISION, "action_declaration_id": "units.move_to"},
            {**_VALID_DECISION, "action_declaration_id": "cities.produce"},
        ]
    )
    with pytest.raises(MultipleDecisionsError):
        parse_decision(content)


# --------------------------------------------------------------------------
# RESPONSE_SCHEMA sanity
# --------------------------------------------------------------------------


def test_response_schema_describes_a_single_object_not_a_list() -> None:
    assert RESPONSE_SCHEMA["type"] == "object"
    assert "action_declaration_id" in RESPONSE_SCHEMA["properties"]
    assert "reasoning" in RESPONSE_SCHEMA["properties"]
    assert set(RESPONSE_SCHEMA["required"]) == {"action_declaration_id", "reasoning"}
