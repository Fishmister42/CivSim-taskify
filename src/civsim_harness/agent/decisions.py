"""Decision response schema and parsing (T104).

FR-008 / FR-012 / P4 / P10: one call to the model provider yields *exactly
one* decision carrying its stated reasoning, or a failed call -- never a
defaulted no-op recorded as if the agent had chosen it. That distinction is
load-bearing: a synthesized no-op recorded as an agent decision would corrupt
SC-012 (chain exhaustion has no fabricate/skip/default-move path) and SC-022
(a stuck agent must be visible in the record as stuck, not as having chosen
to do nothing). This module owns two things: the JSON-schema shape a
provider is asked to conform to (``RESPONSE_SCHEMA``, handed through as
``DecisionRequest.response_schema``), and the parser that turns a provider's
raw text content into ``RawDecision | None``.

Malformed output and decision-free output are folded into the *same*
outcome deliberately. P4 says a successful response containing no usable
decision is ``CallOutcome.EMPTY_RESPONSE``, full stop -- there is no separate
carve-out in the contract for "well-formed-but-empty" versus "malformed";
both are simply "no usable decision", so :func:`parse_decision` returns
``None`` for both, and it is the caller (the OpenRouter adapter's
``complete()``, T105) that maps ``None`` to ``CallOutcome.EMPTY_RESPONSE``.

Multi-decision output is different in kind, not degree: it is a contract
violation (P10), so :func:`parse_decision` raises
:class:`MultipleDecisionsError` instead of returning anything, and callers
MUST let that propagate rather than catching it to synthesize any
``DecisionResponse`` at all -- silently dropping or queuing the extra
decisions would be as wrong as executing them (contract "Conformance tests").
"""

from __future__ import annotations

import json
from typing import Any, Final

from civsim_harness.errors import HarnessError
from civsim_harness.models.common import DeclarationId
from civsim_harness.provider.port import RawDecision

# --------------------------------------------------------------------------
# Response schema -- the shape a provider is asked to conform to
# --------------------------------------------------------------------------

RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action_declaration_id", "reasoning"],
    "properties": {
        "action_declaration_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The catalog declaration id of the single action being taken this step "
                "(e.g. 'units.move_to', 'turn.end_turn')."
            ),
        },
        "reasoning": {
            "type": "string",
            "minLength": 1,
            "description": "The full reasoning behind this single decision.",
        },
        "parameters": {
            "type": "object",
            "description": "Parameters for the declared action, per its catalog schema.",
        },
        "is_end_turn": {
            "type": "boolean",
            "description": (
                "True only when action_declaration_id is the declared end-turn action."
            ),
        },
        "prompt_type": {
            "type": ["string", "null"],
            "description": "Set only when this decision answers a game-initiated prompt.",
        },
    },
}
"""Describes exactly one decision object -- never a list, never a wrapper key.

Handed through untouched as ``DecisionRequest.response_schema`` so a provider
adapter (T105) can request structured output conforming to this shape.
"""


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class MultipleDecisionsError(HarnessError):
    """A provider returned more than one decision for a single call.

    P10 / FR-008 / invariant I13: this is a contract violation the adapter
    must surface, not reconcile. Callers MUST let this propagate -- catching
    it to drop or queue the extra decisions is exactly as wrong as executing
    them (contract "Conformance tests").
    """


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _as_decision(obj: Any) -> RawDecision | None:
    """Validate one candidate decision object; ``None`` on any shape mismatch (P4)."""
    if not isinstance(obj, dict):
        return None

    action_declaration_id = obj.get("action_declaration_id")
    if not isinstance(action_declaration_id, str) or not action_declaration_id.strip():
        return None

    reasoning = obj.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        return None

    parameters = obj.get("parameters", {})
    if not isinstance(parameters, dict):
        return None

    is_end_turn = obj.get("is_end_turn", False)
    if not isinstance(is_end_turn, bool):
        return None

    prompt_type = obj.get("prompt_type")
    if prompt_type is not None and not isinstance(prompt_type, str):
        return None

    return RawDecision(
        action_declaration_id=DeclarationId(action_declaration_id),
        reasoning=reasoning,
        parameters=dict(parameters),
        is_end_turn=is_end_turn,
        prompt_type=prompt_type,
    )


def parse_decision(content: str | None) -> RawDecision | None:
    """Parse one provider call's raw text content into exactly one decision, or ``None``.

    ``None`` covers both decision-free output (empty/blank/``null``/``{}``/
    ``[]`` content) and malformed output (invalid JSON, or JSON that does not
    match the single-decision shape) alike -- per P4, both are "a successful
    response with no usable decision", i.e. ``CallOutcome.EMPTY_RESPONSE``,
    never a decision to do nothing.

    Only genuinely multi-decision output is treated differently: a top-level
    JSON array carrying more than one element raises
    :class:`MultipleDecisionsError`, which callers must not catch-and-convert
    (P10). A single-element array is accepted as that one decision -- the
    contract violation is about *count*, not about tolerating a model that
    wrapped its one answer in a list.
    """
    if content is None or not content.strip():
        return None

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None

    if parsed is None:
        return None

    if isinstance(parsed, list):
        if len(parsed) == 0:
            return None
        if len(parsed) > 1:
            raise MultipleDecisionsError(
                f"provider returned {len(parsed)} decisions for one decision step (P10)",
                detail={"decision_count": len(parsed)},
            )
        parsed = parsed[0]

    return _as_decision(parsed)
