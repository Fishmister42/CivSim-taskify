"""``DecisionView`` and ``ModelCallView`` (T023, data-model.md SS8).

Two models on one page with two very different standings, which is the reason
they live in one file rather than being separated by tidiness:

- the **decision** is in-game -- it names an action a human player issues with
  mouse and keyboard, and its result is visible on the map and in the HUD;
- the **model call** is not game information at all. Model identity, latency,
  and cost are harness telemetry (FR-013): visible to the user and the directing
  session, never presented to the playing agent as game information, and
  rendered in a visually distinct "cost & latency" sub-panel so the two
  categories are never visually ambiguous even though both appear on the same
  page (data-model.md SS8 Validation, UP-001).

``ModelCallView.category`` carries that distinction into *both* readers rather
than leaving it to a CSS class the JSON body cannot see.

**Reasoning**, per data-model.md SS8's Validation clause, verbatim:

    an empty ``reasoning`` renders as an explicit "no reasoning recorded"
    label ... a very long ``reasoning`` is truncated in the collapsed panel view
    with an expand affordance

so this model carries the full text *and* the collapsed preview *and* the flag
saying they differ. The template shows the preview with an expand affordance and
the JSON body carries the whole thing; neither reader is missing anything the
other has, which is the only way that clause and FR-007 can both hold.
"""

from __future__ import annotations

from typing import Any

from civsim_web.registry.loader import PanelRegistry
from civsim_web.registry.lookup import panel_for_action
from civsim_web.viewmodels.base import UnavailableField, ViewModel
from civsim_web.viewmodels.gate import OUT_OF_GAME_BASIS, GatedReader

__all__ = [
    "NO_REASONING_LABEL",
    "REASONING_COLLAPSE_LIMIT",
    "DecisionView",
    "ModelCallView",
    "build_decision_view",
    "build_model_call_view",
]

#: Characters shown in the collapsed panel before the expand affordance takes
#: over. Generous on purpose: truncation exists so one enormous reasoning block
#: cannot push the rest of the glance view off screen (UP-003), not to ration
#: what a reader may see -- the full text is always in the same response.
REASONING_COLLAPSE_LIMIT = 600

#: data-model.md SS8, verbatim. Never a blank space, which a reader cannot
#: distinguish from a loading state (UP-005).
NO_REASONING_LABEL = "no reasoning recorded"


class ModelCallView(ViewModel):
    """Out-of-game telemetry for one model call (data-model.md SS8, FR-005)."""

    model_served: str | None = None
    latency_ms: int | None = None
    cost: dict[str, Any] = {}
    fallback_occurred: bool | None = None
    outcome: str | None = None

    category: str = OUT_OF_GAME_BASIS
    """Carried in the body, not just in a CSS class, so a machine caller can
    tell harness telemetry from game observation without consulting the
    registry separately (FR-013, data-model.md SS8 Validation)."""

    unavailable: tuple[UnavailableField, ...] = ()


class DecisionView(ViewModel):
    """One agent decision, with the evidence it is never shown without (UP-004)."""

    action_label: str
    action_label_is_declaration_id: bool = False
    """``True`` when no registry panel names this specific action and the raw
    declaration id is standing in. Said out loud rather than passed off as a
    label, because data-model.md SS8 asks for a registry-sourced label and this
    is the honest account of not having one."""

    action_declaration_id: str | None = None
    parameters: dict[str, Any] = {}
    is_end_turn: bool = False
    execution_outcome: str | None = None
    rejection_reason: str | None = None

    reasoning: str | None = None
    reasoning_label: str = NO_REASONING_LABEL
    reasoning_is_empty: bool = True
    reasoning_preview: str = ""
    reasoning_is_truncated: bool = False

    parity_basis: str | None = None
    model_call: ModelCallView = ModelCallView()
    unavailable: tuple[UnavailableField, ...] = ()


def build_model_call_view(record: Any | None, *, registry: PanelRegistry) -> ModelCallView:
    """Project 002's ``ModelCall`` into the cost & latency sub-panel."""
    if record is None:
        return ModelCallView()
    gate = GatedReader(registry, "ModelCall", record)
    return ModelCallView(
        model_served=gate.text("model_served"),
        latency_ms=gate.get("latency_ms"),
        cost=gate.mapping("cost"),
        fallback_occurred=gate.get("fallback_occurred"),
        outcome=gate.text("outcome"),
        unavailable=gate.missing,
    )


def build_decision_view(
    decision: Any | None,
    *,
    registry: PanelRegistry,
    model_call: Any | None = None,
) -> DecisionView | None:
    """Project 002's ``Decision`` + ``ActionExecution`` + ``ModelCall``.

    Returns ``None`` for a step with no decision recorded. That is distinct from
    a decision with empty reasoning: the first means nothing was decided, the
    second means something was decided and no reason was given, and UP-005
    forbids rendering either as the other.
    """
    if decision is None:
        return None

    gate = GatedReader(registry, "Decision", decision)
    declaration_id = gate.text("action_declaration_id")
    label, from_registry = _action_label(registry, declaration_id)

    reasoning = gate.text("reasoning")
    execution = gate.get("execution")
    execution_gate = GatedReader(registry, "ActionExecution", execution)

    return DecisionView(
        action_label=label,
        action_label_is_declaration_id=not from_registry,
        action_declaration_id=declaration_id,
        parameters=gate.mapping("parameters"),
        is_end_turn=bool(gate.get("is_end_turn", default=False)),
        execution_outcome=execution_gate.text("outcome"),
        rejection_reason=execution_gate.text("rejection_reason"),
        reasoning=reasoning,
        reasoning_label=_reasoning_label(reasoning),
        reasoning_is_empty=not (reasoning or "").strip(),
        reasoning_preview=_preview(reasoning),
        reasoning_is_truncated=_is_truncated(reasoning),
        parity_basis=gate.basis("action_declaration_id"),
        model_call=build_model_call_view(model_call, registry=registry),
        unavailable=tuple(gate.missing) + tuple(execution_gate.missing),
    )


# --------------------------------------------------------------------------
# Labels and reasoning presentation
# --------------------------------------------------------------------------


def _action_label(registry: PanelRegistry, declaration_id: str | None) -> tuple[str, bool]:
    """``(label, came_from_the_registry)``.

    **Recorded gap**: data-model.md SS8 sources ``action_label`` from "the Panel
    Registry / underlying ``ParityDeclaration.summary``", but the registry
    schema (contracts/panel-registry.md) has no per-action label field, and this
    feature deliberately does not read 002's action catalog directly. So a panel
    may name a specific action via a ``[declaration_id=...]`` selector and
    supply its title; where none does, the declaration id itself is shown and
    flagged. Showing the id is not a parity leak -- the id names an action a
    human player can issue, and the action panel already declares its basis --
    but it is not the label the data model asked for, so it says so.
    """
    if not declaration_id:
        return ("no action recorded", False)
    panel = panel_for_action(registry, declaration_id)
    if panel is not None:
        return (panel.title, True)
    return (declaration_id, False)


def _reasoning_label(reasoning: str | None) -> str:
    """The explicit label for empty reasoning; the first line otherwise."""
    text = (reasoning or "").strip()
    if not text:
        return NO_REASONING_LABEL
    return text.splitlines()[0][:120]


def _preview(reasoning: str | None) -> str:
    text = (reasoning or "").strip()
    if not text:
        return ""
    if len(text) <= REASONING_COLLAPSE_LIMIT:
        return text
    return text[:REASONING_COLLAPSE_LIMIT].rstrip()


def _is_truncated(reasoning: str | None) -> bool:
    return len((reasoning or "").strip()) > REASONING_COLLAPSE_LIMIT
