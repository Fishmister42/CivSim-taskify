"""T256: each action declares what its single `parameters.target` names, and the agent is shown it.

MEASURED 2026-09-21 (attempts 4-5): the model sent the warrior's unit id as `units.move_to`'s
target sixteen times in a row after reading a convention paragraph that said otherwise. The
declaration now carries `target_kind` (+ an optional `target_hint`), the catalog text renders one
concrete example per action on the action's own line, and every real action declares a kind.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from civsim_harness.agent.context import assemble_action_catalog_text
from civsim_harness.capability.loader import load_catalog
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration, TargetKind

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = REPO_ROOT / "catalogs"


def _action(**overrides: object) -> ParityDeclaration:
    base: dict[str, object] = {
        "declaration_id": "units.move_to",
        "kind": "action",
        "summary": "Order the selected unit to move to a target plot.",
        "parity_basis": "Select the unit, right-click the destination.",
        "context": "InGame",
        "capability_id": "units.orders",
        "availability_predicate": "unit.is_selected",
        "verification_predicate": "unit.plot == target",
        "introduced_in_version": "2026.09.3",
    }
    return ParityDeclaration.model_validate({**base, **overrides})


def test_an_action_without_a_target_kind_renders_exactly_as_before() -> None:
    text = assemble_action_catalog_text([_action()])
    assert "- units.move_to: Order the selected unit to move to a target plot." in text
    assert "target:" not in text.split("\n")[-1]


def test_a_target_kind_renders_a_concrete_example_on_the_actions_own_line() -> None:
    declaration = _action(
        target_kind="plot",
        target_hint="a destination plot from the selected unit's reachable_plots -- the plot, "
        "never the unit's id",
    )
    line = assemble_action_catalog_text([declaration]).split("\n")[-1]
    assert line.startswith("- units.move_to: Order the selected unit to move to a target plot.")
    assert '-- target: a plot, {"target": {"x": 43, "y": 31}}; a destination plot' in line
    assert "never the unit's id" in line


@pytest.mark.parametrize("kind", list(TargetKind))
def test_every_target_kind_has_a_rendered_example(kind: TargetKind) -> None:
    line = assemble_action_catalog_text([_action(target_kind=kind)]).split("\n")[-1]
    assert " -- target: " in line
    if kind is TargetKind.NONE:
        assert "no target" in line
    else:
        assert '"target"' in line


def test_target_kind_is_only_meaningful_on_an_action() -> None:
    with pytest.raises(ValidationError, match="only meaningful on an action"):
        ParityDeclaration.model_validate(
            {
                "declaration_id": "units.state",
                "kind": "observation",
                "summary": "Units.",
                "parity_basis": "Look at the map.",
                "context": "InGame",
                "capability_id": "units.read",
                "output_schema": {"type": "object"},
                "introduced_in_version": "2026.09.3",
                "target_kind": "plot",
            }
        )


def test_a_hint_without_a_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="target_hint requires target_kind"):
        _action(target_hint="a plot")


def test_every_real_action_declares_its_target_kind_and_the_rendering_is_per_action() -> None:
    catalog = load_catalog(CATALOG_ROOT)
    actions = [d for d in catalog.declarations.values() if d.kind is DeclarationKind.ACTION]
    assert len(actions) >= 36
    missing = sorted(str(d.declaration_id) for d in actions if d.target_kind is None)
    assert missing == [], f"actions without target_kind: {missing}"

    text = assemble_action_catalog_text(catalog.declarations.values())
    lines = {line.split(":", 1)[0][2:]: line for line in text.split("\n") if line.startswith("- ")}
    assert '{"target": {"x": 43, "y": 31}}' in lines["units.move_to"]
    assert "never the unit's id" in lines["units.move_to"]
    assert "no target" in lines["units.found_city"]
    assert "no target" in lines["turn.end_turn"]
    assert '{"target": "continue"}' in lines["prompts.tech_civic_completed"]
    assert "TECH_POTTERY" in lines["research.set_tech"]
