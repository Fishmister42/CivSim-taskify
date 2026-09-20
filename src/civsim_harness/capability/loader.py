"""The catalog YAML loader (T035).

Loads every file under a ``catalogs/`` root and enforces all seven load-time
validations from ``contracts/capability-catalog.md``:

1. Every declaration has a non-empty ``parity_basis``.
2. Every ``capability_id`` resolves; every ``path: bespoke`` has a non-empty
   ``firetuner_gap``.
3. ``declaration_id`` values are unique across all files.
4. Action entries have both ``availability_predicate`` and
   ``verification_predicate``.
5. Observation and view entries have a valid ``output_schema``.
6. Predicates reference only symbols the evaluator exposes.
7. ``catalogs/VERSION`` is present and the computed content hash is recorded.

Validations 1, half of 2 (the bespoke/``firetuner_gap`` rule), 4, and the
presence half of 5 are already enforced by the ``ParityDeclaration`` and
``IntegrationCapability`` pydantic models in
:mod:`civsim_harness.models.catalog` -- a malformed entry cannot even
construct as one of those types. This module is responsible for wrapping
that failure into :class:`~civsim_harness.errors.CatalogError`, and for
everything a single entry's own shape cannot check on its own: cross-file
``declaration_id`` uniqueness (3), ``capability_id`` cross-resolution (the
other half of 2), the deeper shape of ``output_schema`` beyond "present" (the
rest of 5), the predicate symbol-table gate (6), and ``VERSION`` (7).

**Every one of these failures aborts startup, not turn 1** -- a catalog
defect discovered mid-run is a defect discovered too late (contracts/
capability-catalog.md "Load-time validation"). Nothing in this module
recovers from a validation failure; it raises and lets the caller (run
preflight, in a later wave) fail the whole run before it starts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from civsim_harness.capability.predicates import unknown_predicate_symbols
from civsim_harness.capability.version import compute_content_hash
from civsim_harness.errors import CatalogError
from civsim_harness.models.catalog import (
    CatalogVersion,
    DeclarationKind,
    IntegrationCapability,
    ParityDeclaration,
)
from civsim_harness.models.common import CapabilityId, DeclarationId

VERSION_FILENAME = "VERSION"
CAPABILITIES_FILENAME = "capabilities.yaml"
CAPABILITIES_DIRNAME = "capabilities"
OBSERVATIONS_DIRNAME = "observations"
ACTIONS_DIRNAME = "actions"

# JSON Schema's core `type` vocabulary (2020-12). Used only to sanity-check
# the shape of a declared `output_schema` at load time -- this package has no
# `jsonschema` dependency (out of scope for this deliverable), so this is a
# structural check, not a meta-schema validator.
_JSON_SCHEMA_TYPES: frozenset[str] = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)
_JSON_SCHEMA_COMPOSITION_KEYWORDS: frozenset[str] = frozenset(
    {"$ref", "enum", "oneOf", "anyOf", "allOf"}
)


@dataclass(frozen=True)
class Catalog:
    """The fully loaded, validated capability catalog for one run.

    ``declarations`` and ``capabilities`` are read-only mappings
    (:class:`types.MappingProxyType`) keyed by their own id -- callers (the
    registry, T036; preflight, a later wave) look entries up rather than
    scanning a list.
    """

    root: Path
    version: CatalogVersion
    declarations: Mapping[DeclarationId, ParityDeclaration]
    capabilities: Mapping[CapabilityId, IntegrationCapability]


def load_catalog(root: Path | str) -> Catalog:
    """Load and fully validate the catalog rooted at *root*.

    Raises :class:`~civsim_harness.errors.CatalogError` on the first
    validation failure encountered. Callers that want every failure at once
    (e.g. an authoring lint pass) are not served by this function; it is
    written for the startup path, where the first failure is already enough
    to abort.
    """
    root = Path(root)

    version_string = _read_version_file(root)

    capability_files = _discover_capability_files(root)
    declaration_files = _discover_declaration_files(root)

    capabilities = _load_capabilities(capability_files)
    declarations = _load_declarations(declaration_files)

    _validate_capability_ids_resolve(declarations, capabilities)

    content_hash = compute_content_hash(
        root=root,
        files=[*capability_files, *declaration_files, root / VERSION_FILENAME],
        declaration_ids=(str(declaration_id) for declaration_id in declarations),
    )

    version = CatalogVersion(
        version=version_string,
        content_hash=content_hash,
        declaration_ids=sorted(declarations.keys()),
    )

    return Catalog(
        root=root,
        version=version,
        declarations=MappingProxyType(dict(declarations)),
        capabilities=MappingProxyType(dict(capabilities)),
    )


# --------------------------------------------------------------------------
# Validation 7 -- catalogs/VERSION
# --------------------------------------------------------------------------


def _read_version_file(root: Path) -> str:
    version_path = root / VERSION_FILENAME
    if not version_path.is_file():
        raise CatalogError(
            "catalogs/VERSION is missing",
            detail={"path": str(version_path)},
        )
    version = version_path.read_text(encoding="utf-8").strip()
    if not version:
        raise CatalogError(
            "catalogs/VERSION is present but empty",
            detail={"path": str(version_path)},
        )
    return version


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------


def _discover_capability_files(root: Path) -> list[Path]:
    files: list[Path] = []
    single = root / CAPABILITIES_FILENAME
    if single.is_file():
        files.append(single)
    directory = root / CAPABILITIES_DIRNAME
    if directory.is_dir():
        files.extend(sorted(directory.rglob("*.yaml")))
    return files


def _discover_declaration_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirname in (OBSERVATIONS_DIRNAME, ACTIONS_DIRNAME):
        directory = root / dirname
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.yaml")))
    return files


def _read_yaml_sequence(file: Path) -> list[dict[str, Any]]:
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CatalogError("catalog file is not valid YAML", detail={"file": str(file)}) from exc

    if data is None:
        return []
    if not isinstance(data, list):
        raise CatalogError(
            "catalog file must contain a YAML sequence of entries",
            detail={"file": str(file)},
        )

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise CatalogError(
                "catalog entry must be a YAML mapping",
                detail={"file": str(file), "index": index},
            )
        entries.append(item)
    return entries


# --------------------------------------------------------------------------
# IntegrationCapability loading (validation 2's firetuner_gap half is
# enforced by the model itself; capability_id uniqueness is enforced here
# as a load-bearing precondition for the cross-resolution check below)
# --------------------------------------------------------------------------


def _load_capabilities(files: Iterable[Path]) -> dict[CapabilityId, IntegrationCapability]:
    capabilities: dict[CapabilityId, IntegrationCapability] = {}
    first_seen: dict[CapabilityId, Path] = {}

    for file in files:
        for index, raw_entry in enumerate(_read_yaml_sequence(file)):
            try:
                capability = IntegrationCapability(**raw_entry)
            except PydanticValidationError as exc:
                raise CatalogError(
                    "catalog capability failed schema validation",
                    detail={"file": str(file), "index": index, "errors": _summarize(exc)},
                ) from exc

            if capability.capability_id in capabilities:
                raise CatalogError(
                    "duplicate capability_id across catalog files",
                    detail={
                        "capability_id": str(capability.capability_id),
                        "first_seen_in": str(first_seen[capability.capability_id]),
                        "duplicate_in": str(file),
                    },
                )
            capabilities[capability.capability_id] = capability
            first_seen[capability.capability_id] = file

    return capabilities


# --------------------------------------------------------------------------
# ParityDeclaration loading (validations 1, 3, 4, 5, 6)
# --------------------------------------------------------------------------


def _load_declarations(files: Iterable[Path]) -> dict[DeclarationId, ParityDeclaration]:
    declarations: dict[DeclarationId, ParityDeclaration] = {}
    first_seen: dict[DeclarationId, Path] = {}

    for file in files:
        for index, raw_entry in enumerate(_read_yaml_sequence(file)):
            try:
                declaration = ParityDeclaration(**raw_entry)
            except PydanticValidationError as exc:
                # Covers validation 1 (empty/missing parity_basis), the
                # presence half of validation 5 (missing output_schema), and
                # validation 4 (an action missing either predicate) -- all
                # enforced by ParityDeclaration's own field defaults and
                # model_validator.
                raise CatalogError(
                    "catalog declaration failed schema validation",
                    detail={"file": str(file), "index": index, "errors": _summarize(exc)},
                ) from exc

            if declaration.declaration_id in declarations:
                # Validation 3.
                raise CatalogError(
                    "duplicate declaration_id across catalog files",
                    detail={
                        "declaration_id": str(declaration.declaration_id),
                        "first_seen_in": str(first_seen[declaration.declaration_id]),
                        "duplicate_in": str(file),
                    },
                )
            declarations[declaration.declaration_id] = declaration
            first_seen[declaration.declaration_id] = file

            if declaration.kind is DeclarationKind.ACTION:
                # Validation 6. (Validation 4's presence check already
                # happened in the model; both predicates are non-None here.)
                assert declaration.availability_predicate is not None
                assert declaration.verification_predicate is not None
                _check_predicate_symbols(
                    declaration, "availability_predicate", declaration.availability_predicate
                )
                _check_predicate_symbols(
                    declaration, "verification_predicate", declaration.verification_predicate
                )
            else:
                # The rest of validation 5, beyond "output_schema is present".
                assert declaration.output_schema is not None
                _validate_schema_node(
                    declaration.output_schema,
                    declaration_id=str(declaration.declaration_id),
                    path="output_schema",
                )

    return declarations


def _check_predicate_symbols(declaration: ParityDeclaration, field: str, predicate: str) -> None:
    unknown = unknown_predicate_symbols(predicate)
    if unknown:
        raise CatalogError(
            "predicate references a symbol outside the exposed table",
            detail={
                "declaration_id": str(declaration.declaration_id),
                "field": field,
                "unknown_symbols": sorted(unknown),
            },
        )


# --------------------------------------------------------------------------
# Validation 2 -- capability_id cross-resolution
# --------------------------------------------------------------------------


def _validate_capability_ids_resolve(
    declarations: Mapping[DeclarationId, ParityDeclaration],
    capabilities: Mapping[CapabilityId, IntegrationCapability],
) -> None:
    for declaration in declarations.values():
        if declaration.capability_id not in capabilities:
            raise CatalogError(
                "capability_id does not resolve to a declared IntegrationCapability",
                detail={
                    "declaration_id": str(declaration.declaration_id),
                    "capability_id": str(declaration.capability_id),
                },
            )


# --------------------------------------------------------------------------
# Validation 5 -- output_schema shape
# --------------------------------------------------------------------------


def _validate_schema_node(node: Any, *, declaration_id: str, path: str) -> None:
    if not isinstance(node, Mapping):
        raise CatalogError(
            "output_schema node is not a JSON Schema object",
            detail={"declaration_id": declaration_id, "path": path},
        )

    schema_type = node.get("type")
    if schema_type is not None:
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        for one_type in types:
            if one_type not in _JSON_SCHEMA_TYPES:
                raise CatalogError(
                    "output_schema has an unrecognised JSON Schema type",
                    detail={"declaration_id": declaration_id, "path": path, "type": one_type},
                )
    elif not (_JSON_SCHEMA_COMPOSITION_KEYWORDS & node.keys()):
        raise CatalogError(
            "output_schema node has neither 'type' nor a recognised composition keyword",
            detail={"declaration_id": declaration_id, "path": path},
        )

    properties = node.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            raise CatalogError(
                "output_schema 'properties' must be a mapping",
                detail={"declaration_id": declaration_id, "path": path},
            )
        for name, sub_schema in properties.items():
            _validate_schema_node(
                sub_schema, declaration_id=declaration_id, path=f"{path}.properties.{name}"
            )

    required = node.get("required")
    if required is not None and not (
        isinstance(required, list) and all(isinstance(item, str) for item in required)
    ):
        raise CatalogError(
            "output_schema 'required' must be a list of strings",
            detail={"declaration_id": declaration_id, "path": path},
        )

    items = node.get("items")
    if items is not None:
        if isinstance(items, list):
            for item_index, sub_schema in enumerate(items):
                _validate_schema_node(
                    sub_schema, declaration_id=declaration_id, path=f"{path}.items[{item_index}]"
                )
        else:
            _validate_schema_node(items, declaration_id=declaration_id, path=f"{path}.items")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _summarize(exc: PydanticValidationError) -> list[dict[str, str]]:
    return [
        {"loc": ".".join(str(part) for part in error["loc"]), "msg": error["msg"]}
        for error in exc.errors(include_url=False)
    ]
