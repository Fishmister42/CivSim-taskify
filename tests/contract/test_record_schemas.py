"""Contract test: every record model round-trips its published JSON Schema,
and a non-additive schema change fails the build (T028).

contracts/match-store-port.md "Schema evolution": record schemas are
versioned and additive-only within a major version. This suite asserts
three things:

1. Every schema checked in under ``specs/002-civ-playing-harness/contracts/
   schemas/`` is exactly what regenerating from the current model produces
   (no drift between a model and its published schema).
2. A concrete example instance of every record model dumps to a dict whose
   keys line up with that schema's declared properties and required set
   (the "round trip").
3. :func:`export_all` -- the actual build-time exporter, not a simulation
   of it -- refuses to overwrite a published schema with one that removed
   or retyped a field, and raises rather than silently corrupting the
   published contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from civsim_harness.errors import CatalogError
from civsim_harness.models.config import StopCondition
from civsim_harness.models.export_schemas import (
    DEFAULT_OUTPUT_DIR,
    MODEL_REGISTRY,
    UNION_REGISTRY,
    build_all_schemas,
    check_additive_only,
    export_all,
)

NOW = datetime.now(UTC)

# --------------------------------------------------------------------------
# One valid example instance per record model, used for the round-trip check.
# --------------------------------------------------------------------------

_CATALOG_VERSION_REF = {"version": "2026.09.1", "content_hash": "abc123"}

EXAMPLES: dict[str, BaseModel] = {}


def _register(record_name: str, model_cls: type[BaseModel], **fields: Any) -> None:
    EXAMPLES[record_name] = model_cls.model_validate(fields)


_register(
    "SeedSet",
    MODEL_REGISTRY["SeedSet"],
    seed_set_id="ss1",
    name="shuffle-classic-2026q3",
    seeds=["1849275663"],
    civilization="CIVILIZATION_ROME",
    leader="LEADER_TRAJAN",
    ruleset="RULESET_EXPANSION_2",
    mod_set=[{"id": "bbg", "version": "4.2.1"}],
    game_build="win/1.0.12.9",
    created_at=NOW,
)

_register(
    "RunConfiguration",
    MODEL_REGISTRY["RunConfiguration"],
    config_id="cfg1",
    map_seed="123",
    civilization="CIVILIZATION_ROME",
    leader="LEADER_TRAJAN",
    ruleset="RULESET_EXPANSION_2",
    difficulty="DIFFICULTY_PRINCE",
    stop_condition={"type": "turn_reached", "turn": 50},
    model_config={
        "primary": {"provider": "openrouter", "model": "x"},
        "request_params": {"temperature": 0.7},
    },
    no_progress_step_limit=8,
    recovery_attempt_limit=3,
    min_free_disk_gb=25,
    created_at=NOW,
)

_register(
    "GuidanceSet",
    MODEL_REGISTRY["GuidanceSet"],
    guidance_set_id="gs1",
    content_hash="abc",
    content="Play tall.",
)

_register(
    "ModelConfig",
    MODEL_REGISTRY["ModelConfig"],
    primary={"provider": "openrouter", "model": "x"},
)

_register(
    "Run",
    MODEL_REGISTRY["Run"],
    run_id="run1",
    config_id="cfg1",
    lifecycle_state="finished",
    started_at=NOW,
    ended_at=NOW,
    stop_resolution="turn_reached",
    record_completeness_status="complete",
    comparability_status="comparable",
    observation_catalog_version=_CATALOG_VERSION_REF,
    action_catalog_version=_CATALOG_VERSION_REF,
    game_build="win/1.0.12.9",
    host_support_tier="validated",
    capture_path="windows_graphics_capture",
)

_register(
    "TurnCycle",
    MODEL_REGISTRY["TurnCycle"],
    turn_cycle_id="tc1",
    run_id="run1",
    turn_number=1,
    attempt_index=0,
    is_authoritative=True,
    save_point_id="sp1",
    step_count=3,
    outcome="ended_by_agent",
    final_no_progress_streak=0,
    visually_degraded=False,
    started_at=NOW,
    ended_at=NOW,
    persisted_at=NOW,
)

_register(
    "DecisionStep",
    MODEL_REGISTRY["DecisionStep"],
    decision_step_id="ds1",
    turn_cycle_id="tc1",
    step_index=1,
    observation_id="obs1",
    decision_id="dec1",
    model_call_id="mc1",
    progress="changed_state",
    no_progress_streak_after=0,
    visually_degraded=False,
    started_at=NOW,
    ended_at=NOW,
)

_register(
    "ObservationEntry",
    MODEL_REGISTRY["ObservationEntry"],
    declaration_id="units.list",
    key="units",
    value=[],
    context="InGame",
)

_register(
    "Observation",
    MODEL_REGISTRY["Observation"],
    observation_id="obs1",
    decision_step_id="ds1",
    assembled_at=NOW,
    catalog_version=_CATALOG_VERSION_REF,
    entries=[{"declaration_id": "units.list", "key": "units", "value": [], "context": "InGame"}],
    captures=["cap1"],
    screen_identity="world",
)

_register(
    "ScreenCapture",
    MODEL_REGISTRY["ScreenCapture"],
    capture_id="cap1",
    run_id="run1",
    turn_number=1,
    decision_step_id="ds1",
    captured_at=NOW,
    view_declaration_id="views.world",
    screening_status="screened_clean",
    shown_to_agent=True,
    retained_as_evidence=True,
    blob_ref="blob123",
    capture_path="windows_graphics_capture",
)

_register(
    "ActionExecution",
    MODEL_REGISTRY["ActionExecution"],
    outcome="applied",
    # FR-011: `applied` is only constructible with the predicate verdict that confirmed it.
    verification={"declaration_id": "turn.end_turn", "predicate": "game.turn_number > 0",
                  "result": True},
    verified_at=NOW,
)

_register(
    "Decision",
    MODEL_REGISTRY["Decision"],
    decision_id="dec1",
    decision_step_id="ds1",
    action_declaration_id="turn.end_turn",
    reasoning="done",
    trigger="proactive",
    is_end_turn=True,
    model_call_id="mc1",
    execution={
        "outcome": "applied",
        "verification": {"declaration_id": "turn.end_turn", "result": True},
        "verified_at": NOW.isoformat(),
    },
)

_register(
    "ParityDeclaration",
    MODEL_REGISTRY["ParityDeclaration"],
    declaration_id="turn.end_turn",
    kind="action",
    summary="End the current turn.",
    parity_basis="Click the end-turn button.",
    context="InGame",
    capability_id="turn.control",
    availability_predicate="game.is_local_player_turn",
    verification_predicate="game.turn_number == observed_turn_number + 1",
    introduced_in_version="2026.09.1",
)

_register(
    "CatalogVersion",
    MODEL_REGISTRY["CatalogVersion"],
    version="2026.09.1",
    content_hash="abc123",
    declaration_ids=["turn.end_turn"],
)

_register(
    "IntegrationCapability",
    MODEL_REGISTRY["IntegrationCapability"],
    capability_id="saves.save_game",
    path="bespoke",
    implementation_ref="src/civsim_harness/saves/dialog_driver.py",
    firetuner_gap="No save-to-named-file call is reachable from Lua.",
    parity_basis="Use the in-client Save Game dialog.",
)

_register(
    "ModelCall",
    MODEL_REGISTRY["ModelCall"],
    model_call_id="mc1",
    run_id="run1",
    turn_cycle_id="tc1",
    decision_step_id="ds1",
    model_requested={"provider": "openrouter", "model": "x"},
    model_served={"provider": "openrouter", "model": "x"},
    latency_ms=100,
    cost={},
    retry_count=0,
    fallback_occurred=False,
    image_count=1,
    outcome="decision_returned",
)

_register(
    "SavePoint",
    MODEL_REGISTRY["SavePoint"],
    save_point_id="sp1",
    run_id="run1",
    turn_number=1,
    save_name="civsim__run1__t0001",
    taken_at=NOW,
    verified=True,
    retention_status="retained",
)

_register(
    "RunEvent",
    MODEL_REGISTRY["RunEvent"],
    event_id="evt1",
    run_id="run1",
    event_type="provider_failure",
    occurred_at=NOW,
)


# --------------------------------------------------------------------------
# 1 & 2: published schemas match the models, and examples round-trip them.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(MODEL_REGISTRY) + sorted(UNION_REGISTRY))
def test_published_schema_matches_current_model(name: str) -> None:
    published_path = DEFAULT_OUTPUT_DIR / f"{name}.schema.json"
    assert published_path.exists(), f"no published schema for {name}; run export_schemas"

    published = json.loads(published_path.read_text(encoding="utf-8"))
    current = build_all_schemas()[name]
    assert published == current, (
        f"{name}.schema.json is stale relative to the model; re-run "
        "civsim_harness.models.export_schemas"
    )
    assert published["schemaVersion"] == 1


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_example_instance_round_trips_its_schema(name: str) -> None:
    schema = build_all_schemas()[name]
    dumped = EXAMPLES[name].model_dump(mode="json", by_alias=True)

    properties = set(schema.get("properties", {}))
    required = set(schema.get("required", []))

    assert set(dumped) <= properties, f"{name} dump has keys the schema does not declare"
    assert required <= set(dumped), f"{name} dump is missing a required field"


def test_stop_condition_union_round_trips_its_schema() -> None:
    adapter = TypeAdapter(StopCondition)
    instance = adapter.validate_python({"type": "turn_reached", "turn": 50})
    dumped = adapter.dump_python(instance, mode="json")

    schema = build_all_schemas()["StopCondition"]
    # A discriminated union publishes its variants under $defs and the top
    # level carries oneOf/discriminator rather than its own "properties".
    assert "oneOf" in schema
    assert dumped["type"] == "turn_reached"
    assert dumped["turn"] == 50


# --------------------------------------------------------------------------
# 3: additive-only enforcement, both the pure diff function and export_all.
# --------------------------------------------------------------------------


def test_check_additive_only_flags_a_removed_field() -> None:
    old = {"properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}
    new = {"properties": {"a": {"type": "string"}}}

    violations = check_additive_only(old, new)

    assert any("b" in v and "removed" in v for v in violations)


def test_check_additive_only_flags_a_retyped_field() -> None:
    old = {"properties": {"a": {"type": "string"}}}
    new = {"properties": {"a": {"type": "integer"}}}

    violations = check_additive_only(old, new)

    assert any("a" in v for v in violations)


def test_check_additive_only_permits_a_pure_addition() -> None:
    old = {"properties": {"a": {"type": "string"}}}
    new = {"properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}

    assert check_additive_only(old, new) == []


def test_export_all_aborts_and_writes_nothing_on_a_non_additive_change(
    tmp_path: Path,
) -> None:
    # Plant an "old" published schema for a real model that the current
    # model can no longer satisfy additively (its type field is retyped).
    current = build_all_schemas()["SavePoint"]
    poisoned = json.loads(json.dumps(current))
    poisoned["properties"]["verified"] = {"type": "string"}  # was boolean
    poisoned_bytes = json.dumps(poisoned, indent=2, sort_keys=True) + "\n"
    (tmp_path / "SavePoint.schema.json").write_text(poisoned_bytes, encoding="utf-8")

    with pytest.raises(CatalogError):
        export_all(tmp_path)

    # Nothing was written: the poisoned file is untouched, and no other
    # schema (which would otherwise have exported cleanly) was written either.
    assert (tmp_path / "SavePoint.schema.json").read_text(encoding="utf-8") == poisoned_bytes
    assert not (tmp_path / "Run.schema.json").exists()


def test_export_all_succeeds_into_an_empty_directory(tmp_path: Path) -> None:
    written = export_all(tmp_path)

    assert set(written) == set(build_all_schemas())
    for name, path in written.items():
        assert path.exists()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk == build_all_schemas()[name]


def test_export_all_is_a_no_op_conflict_on_reexporting_the_same_schemas(
    tmp_path: Path,
) -> None:
    export_all(tmp_path)
    # Re-running against the schemas it just wrote is definitionally
    # additive (nothing changed at all), so it must succeed again rather
    # than treating "already exists, identical" as a violation.
    written_again = export_all(tmp_path)
    assert set(written_again) == set(build_all_schemas())
