"""The populated `cities.state` value, put through the real parity guard.

MEASURED 2026-09-21, the live lane, minutes after `available_productions` was first filled: every
block on every provider paused at step 1 with `ParityViolation` ("assembled context violates the
human-parity boundary (FR-019, FR-020)", 12 findings, category `harness_telemetry`, locations
`cities.state.cities[0].production_options[*].cost`). `cost` on its own is a banned token --
`src/civsim_harness/parity/forbidden.py`'s ``_phrase(HARNESS_TELEMETRY, "call cost", "cost")`` --
and the phrase match is a *subset* test over a key's tokens, so `production_cost` would have
matched just as surely. The field is `production_required`.

Two tests, because the bug had two places it could have been caught:

1. the concrete value this observation now carries, scanned exactly as the decision loop scans it;
2. every property name the observation catalog declares, scanned the same way -- a load-time-ish
   guard that catches the *next* field somebody names `cost`, `model`, `latency` or `seed` before
   it ever reaches a live block.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    LuaContext,
    ObservationId,
)
from civsim_harness.models.turn import Observation, ObservationEntry
from civsim_harness.parity.forbidden import enforce_parity_boundary, scan_observation_entries

CATALOGS_ROOT = Path(__file__).resolve().parents[2] / "catalogs"

#: One owned city exactly as `lua/gamecore/cities.lua` reports it, with the production panel's
#: list filled -- the shape the live lane choked on.
_CITIES_STATE: dict[str, Any] = {
    "cities": [
        {
            "city_id": 65538,
            "name": "Pasargadae",
            "owner_player_id": 0,
            "owner_is_local_player": True,
            "plot": {"x": 10, "y": 12},
            "population": 3,
            "available_productions": ["UNIT_BUILDER", "UNIT_WARRIOR", "BUILDING_MONUMENT"],
            "production_options": [
                {
                    "type": "UNIT_BUILDER",
                    "name": "Builder",
                    "kind": "unit",
                    "production_required": 50,
                    "turns": 5,
                    "disabled": False,
                    "requires_placement": False,
                },
                {
                    "type": "DISTRICT_CAMPUS",
                    "name": "Campus",
                    "kind": "district",
                    "production_required": 54,
                    "turns": 7,
                    "disabled": False,
                    "requires_placement": True,
                },
            ],
            "production_queue": ["UNIT_BUILDER"],
            "gold_available": 182,
            "can_buy_with_gold": True,
            "can_buy_with_faith": False,
            "purchasable_with_gold": ["UNIT_BUILDER"],
            "purchasable_with_faith": [],
        },
        {
            "city_id": 70001,
            "name": "Akkad",
            "owner_player_id": 3,
            "owner_is_local_player": False,
            "plot": {"x": 30, "y": 8},
            "population": 2,
        },
    ]
}


def _observation(value: Any) -> Observation:
    return Observation(
        observation_id=ObservationId("obs-1"),
        decision_step_id=DecisionStepId("step-1"),
        assembled_at=datetime(2026, 9, 21),
        catalog_version=CatalogVersionRef(version="2026.09.7", content_hash="deadbeef"),
        entries=[
            ObservationEntry(
                declaration_id=DeclarationId("cities.state"),
                key="cities.state",
                value=value,
                context=LuaContext.IN_GAME,
            )
        ],
        captures=[],
        screen_identity="world",
    )


def test_a_populated_production_list_passes_the_real_parity_guard() -> None:
    """The test the live lane needed: the value the harness will actually send, through the guard
    the decision loop actually calls."""
    enforce_parity_boundary(observation=_observation(_CITIES_STATE))


def test_the_banned_key_this_regression_used_is_still_banned() -> None:
    """Proof the guard above is load-bearing rather than vacuous: the original field name fails,
    and so would `production_cost`, because the phrase match is a subset test over the key's
    tokens."""
    for banned in ("cost", "production_cost"):
        option = {"type": "UNIT_BUILDER", "kind": "unit", banned: 50}
        city = {**_CITIES_STATE["cities"][0], "production_options": [option]}
        violations = scan_observation_entries(_observation({"cities": [city]}))
        assert [v.location for v in violations] == [
            f"cities.state.cities[0].production_options[0].{banned}"
        ]
        assert violations[0].category == "harness_telemetry"


def _property_names(node: Any) -> list[str]:
    names: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                names.extend(str(name) for name in value)
            names.extend(_property_names(value))
    elif isinstance(node, list):
        for item in node:
            names.extend(_property_names(item))
    return names


def test_no_observation_declaration_names_a_field_the_parity_guard_bans() -> None:
    """The generalised guard. Every field name the real catalog declares is scanned with the same
    phrase list, so the next `cost`/`model`/`latency`/`seed` is caught here rather than by a live
    block pausing at step 1."""
    from civsim_harness.capability.loader import load_catalog

    catalog = load_catalog(CATALOGS_ROOT)
    offenders: dict[str, list[str]] = {}
    for declaration in catalog.declarations.values():
        if declaration.output_schema is None:
            continue
        names = _property_names(declaration.output_schema)
        if not names:
            continue
        probe = _observation({name: 0 for name in names})
        found = [v.description for v in scan_observation_entries(probe)]
        if found:
            offenders[str(declaration.declaration_id)] = found

    assert offenders == {}
