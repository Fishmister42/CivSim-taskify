"""JSON Schema export for every record model (T027).

Writes one JSON Schema file per record model into
``specs/002-civ-playing-harness/contracts/schemas/``, stamped with the
schema version from contracts/match-store-port.md ("Schema version: 1").

Schema evolution is additive-only within a major version (that contract's
"Schema evolution" section): this module is where that rule is actually
enforced, not just documented. :func:`export_all` refuses to overwrite an
existing published schema with one that has removed or retyped a field --
that refusal *is* the "fails the build" behaviour T028 exercises, not a
test-only simulation of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from civsim_harness.errors import CatalogError
from civsim_harness.models.catalog import (
    CatalogVersion,
    IntegrationCapability,
    ParityDeclaration,
)
from civsim_harness.models.config import (
    GuidanceSet,
    ModelConfig,
    RunConfiguration,
    SeedSet,
    StopCondition,
)
from civsim_harness.models.decision import ActionExecution, Decision
from civsim_harness.models.records import ModelCall, RunEvent, SavePoint
from civsim_harness.models.run import Run
from civsim_harness.models.turn import (
    DecisionStep,
    Observation,
    ObservationEntry,
    ScreenCapture,
    TurnCycle,
)

SCHEMA_VERSION = 1

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = _REPO_ROOT / "specs" / "002-civ-playing-harness" / "contracts" / "schemas"

# Every record model in data-model.md SS1-14, plus the named sub-types the
# tables call out individually. Nested value types (ModRef, Cost, ...) are
# not listed separately here -- they already appear as `$defs` inside
# whichever of these schemas reference them.
MODEL_REGISTRY: dict[str, type[BaseModel]] = {
    "SeedSet": SeedSet,
    "RunConfiguration": RunConfiguration,
    "GuidanceSet": GuidanceSet,
    "ModelConfig": ModelConfig,
    "Run": Run,
    "TurnCycle": TurnCycle,
    "DecisionStep": DecisionStep,
    "Observation": Observation,
    "ObservationEntry": ObservationEntry,
    "ScreenCapture": ScreenCapture,
    "Decision": Decision,
    "ActionExecution": ActionExecution,
    "ParityDeclaration": ParityDeclaration,
    "CatalogVersion": CatalogVersion,
    "IntegrationCapability": IntegrationCapability,
    "ModelCall": ModelCall,
    "SavePoint": SavePoint,
    "RunEvent": RunEvent,
}

# StopCondition is a discriminated Union, not a BaseModel subclass, so it is
# schema'd via TypeAdapter rather than through MODEL_REGISTRY.
UNION_REGISTRY: dict[str, Any] = {
    "StopCondition": StopCondition,
}


def build_schema(name: str, model: type[BaseModel]) -> dict[str, Any]:
    """Build one model's published schema document."""
    schema = model.model_json_schema()
    schema["title"] = name
    schema["schemaVersion"] = SCHEMA_VERSION
    return schema


def build_union_schema(name: str, union_type: Any) -> dict[str, Any]:
    """Build a published schema document for a non-BaseModel type (e.g. a Union alias)."""
    schema = TypeAdapter(union_type).json_schema()
    schema["title"] = name
    schema["schemaVersion"] = SCHEMA_VERSION
    return schema


def build_all_schemas() -> dict[str, dict[str, Any]]:
    """Build every published schema document, keyed by record name."""
    schemas = {name: build_schema(name, model) for name, model in MODEL_REGISTRY.items()}
    schemas.update(
        {name: build_union_schema(name, union) for name, union in UNION_REGISTRY.items()}
    )
    return schemas


def _schema_path(output_dir: Path, name: str) -> Path:
    return output_dir / f"{name}.schema.json"


def check_additive_only(old_schema: dict[str, Any], new_schema: dict[str, Any]) -> list[str]:
    """Return a list of additive-only violations found going from *old* to *new*.

    Only two things count as a violation, matching the contract's "Schema
    evolution" rule literally: a field present in *old* that is missing
    from *new* ("removes a field"), and a field present in both whose shape
    changed ("retypes a field"). Adding a new field, or relaxing/loosening
    a field's requirement, is never flagged -- that is exactly what
    "additive-only" permits.
    """
    violations: list[str] = []
    old_properties: dict[str, Any] = old_schema.get("properties", {})
    new_properties: dict[str, Any] = new_schema.get("properties", {})

    for field_name, old_field_schema in old_properties.items():
        if field_name not in new_properties:
            violations.append(f"field {field_name!r} was removed")
            continue
        new_field_schema = new_properties[field_name]
        old_shape = json.dumps(old_field_schema, sort_keys=True)
        new_shape = json.dumps(new_field_schema, sort_keys=True)
        if old_shape != new_shape:
            violations.append(
                f"field {field_name!r} changed shape "
                f"(was {old_shape}, now {new_shape})"
            )
    return violations


def export_all(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Path]:
    """Write every record schema to *output_dir*, one file per record.

    Raises :class:`CatalogError` -- aborting the export entirely, writing
    nothing -- if any schema already published there would be changed in a
    non-additive way. This is the "fails the build" behaviour: a caller
    that runs this as part of a build step gets a hard failure instead of a
    silently overwritten, backward-incompatible schema.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    new_schemas = build_all_schemas()

    violations_by_name: dict[str, list[str]] = {}
    for name, new_schema in new_schemas.items():
        path = _schema_path(output_dir, name)
        if not path.exists():
            continue
        old_schema = json.loads(path.read_text(encoding="utf-8"))
        violations = check_additive_only(old_schema, new_schema)
        if violations:
            violations_by_name[name] = violations

    if violations_by_name:
        detail = {name: violations for name, violations in violations_by_name.items()}
        raise CatalogError(
            "schema export aborted: one or more record schemas would change "
            "non-additively (contracts/match-store-port.md 'Schema evolution')",
            detail=detail,
        )

    written: dict[str, Path] = {}
    for name, new_schema in new_schemas.items():
        path = _schema_path(output_dir, name)
        path.write_text(json.dumps(new_schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written[name] = path
    return written


if __name__ == "__main__":
    written_paths = export_all()
    for record_name, written_path in sorted(written_paths.items()):
        print(f"wrote {record_name} -> {written_path}")
