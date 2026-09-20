"""Panel Registry loading and validation (T007).

The registry is this feature's own structural parity gate -- the mechanism
behind UP-001 and the plan's Constitution Check for Principle I. 002 already
parity-filters what it records; this is the second, independent gate, so that a
field 002 fails to filter correctly still cannot reach a screen here without
someone deliberately adding it to ``panels/*.yaml`` first.

**A load failure aborts startup.** Every rule below raises
``PanelRegistryLoadError``; nothing in this module has a "warn and continue"
path. That is the same discipline 002 applies to its capability catalog, for
the same reason: a partially-validated parity boundary is not a parity
boundary, and the failure must happen where an operator sees it (``doctor``,
process start) rather than on a page someone is reading.

Rules enforced, verbatim from ``contracts/panel-registry.md``:

- **P1** A panel with ``category: in_game`` and an empty/missing
  ``parity_basis`` fails to load.
- **P2** A panel with ``category: out_of_game_telemetry`` must have
  ``parity_basis: null``.
- **P3** Every entry in ``source_fields`` must resolve to a field actually
  present in 002's published ``data-model.md`` for that entity.
- **P4** ``panel_id`` is unique across all four YAML files.
- **P5** ``scope`` must be consistent with every listed ``source_fields``
  entry's own granularity.
- **P6** Declarations are immutable within a version; changing one is a new
  registry version.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from civsim_web.registry.harness_schema import (
    SCOPE_ORDER,
    HarnessSchema,
    load_harness_schema,
)

__all__ = [
    "PanelDeclaration",
    "PanelRegistry",
    "PanelRegistryLoadError",
    "REGISTRY_FILENAMES",
    "SourceField",
    "default_harness_data_model_path",
    "default_panels_dir",
    "load_panel_registry",
]

#: The four files that make up the registry, in ``contracts/panel-registry.md``
#: order. The set is closed: a fifth YAML dropped into ``panels/`` is not
#: loaded, so a declaration cannot arrive by being copied into the directory.
REGISTRY_FILENAMES: tuple[str, ...] = (
    "live.yaml",
    "history.yaml",
    "catalog.yaml",
    "shared.yaml",
)

_STORIES = ("US1", "US2", "US3", "US4")
_CATEGORIES = ("in_game", "out_of_game_telemetry")

_SOURCE_FIELD = re.compile(
    r"^(?P<entity>[A-Za-z_]\w*)\.(?P<field>[A-Za-z_]\w*)(?P<selector>\[[^\]]*\])?$"
)


class PanelRegistryLoadError(Exception):
    """A registry that failed validation. Startup must not continue past this.

    ``rule`` names the violated rule (``P1``..``P6``, or ``schema`` for a
    malformed declaration) so a test -- and an operator reading ``doctor``
    output -- can tell *which* gate rejected the registry rather than only
    that something did.
    """

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"[{rule}] {message}")
        self.rule = rule
        self.message = message


@dataclass(frozen=True)
class SourceField:
    """One parsed ``source_fields`` entry: ``Entity.field[selector]``."""

    entity: str
    field: str
    selector: str | None = None

    def __str__(self) -> str:
        return f"{self.entity}.{self.field}{self.selector or ''}"


class PanelDeclaration(BaseModel):
    """One panel, exactly as declared in ``panels/*.yaml``.

    Frozen and ``extra='forbid'``: a key nobody recognised is a typo or a
    field someone expected to matter, and either way silently ignoring it in a
    parity declaration is the wrong default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    panel_id: str
    title: str
    # Tuples, not lists: a frozen model with a list field is not hashable, and
    # P6's immutability check hashes declarations.
    story: tuple[str, ...] = Field(min_length=1)
    scope: str
    source_fields: tuple[str, ...] = ()
    parity_basis: str | None = None
    category: str
    introduced_in_version: str

    #: Populated by the loader, not by YAML.
    declared_in: str = ""

    @property
    def parsed_source_fields(self) -> tuple[SourceField, ...]:
        parsed: list[SourceField] = []
        for raw in self.source_fields:
            matched = _SOURCE_FIELD.match(raw.strip())
            if matched is None:
                raise PanelRegistryLoadError(
                    "P3",
                    f"panel {self.panel_id!r} source_fields entry {raw!r} is not of the "
                    f"form Entity.field or Entity.field[selector]",
                )
            parsed.append(
                SourceField(
                    entity=matched.group("entity"),
                    field=matched.group("field"),
                    selector=matched.group("selector"),
                )
            )
        return tuple(parsed)


@dataclass(frozen=True)
class PanelRegistry:
    """The validated registry in force, plus the indices view models read.

    ``by_source_field`` is the index that makes UP-001 structural: a view-model
    constructor asks "which panel may read ``Observation.entries``?" and gets
    nothing back for an unregistered field, so the field is never read at all
    rather than read and then filtered.
    """

    version: str
    content_hash: str
    declarations: tuple[PanelDeclaration, ...]
    by_id: dict[str, PanelDeclaration]
    by_source_field: dict[tuple[str, str], tuple[PanelDeclaration, ...]]

    @property
    def panel_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.by_id))

    def get(self, panel_id: str) -> PanelDeclaration | None:
        """The declaration for ``panel_id``, or ``None`` if unregistered.

        ``None`` is a client-side reference error at the route layer (a 404,
        per contracts/web-read-api.md), never a "field unavailable" render.
        """
        return self.by_id.get(panel_id)

    def panels_for_field(self, entity: str, field: str) -> tuple[PanelDeclaration, ...]:
        """Panels permitted to read ``entity.field``. Empty means: never read it."""
        return self.by_source_field.get((entity, field), ())


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def _repo_root() -> Path:
    # src/civsim_web/registry/loader.py -> registry -> civsim_web -> src -> root
    return Path(__file__).resolve().parents[3]


def default_panels_dir() -> Path:
    """``panels/`` at the repository root, overridable for tests and packaging."""
    override = os.environ.get("CIVSIM_WEB_PANELS_DIR")
    return Path(override) if override else _repo_root() / "panels"


def default_harness_data_model_path() -> Path:
    """002's published ``data-model.md`` -- rule P3's reference document."""
    override = os.environ.get("CIVSIM_WEB_HARNESS_DATA_MODEL")
    if override:
        return Path(override)
    return _repo_root() / "specs" / "002-civ-playing-harness" / "data-model.md"


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _version_key(version: str) -> tuple[Any, ...]:
    """A natural-order sort key, so ``"2" > "10"`` is not accidentally true."""
    return tuple(
        int(part) if part.isdigit() else part for part in re.split(r"[.\-_]", version)
    )


def _declaration_payload(declaration: PanelDeclaration) -> dict[str, Any]:
    """The hashable content of a declaration -- everything but where it lives.

    ``declared_in`` is excluded on purpose: moving a panel between
    ``live.yaml`` and ``shared.yaml`` does not change what it declares, and P6
    is about the declaration, not its filing.
    """
    payload = declaration.model_dump()
    payload.pop("declared_in", None)
    return payload


def _hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_version(panels_dir: Path) -> str:
    version_file = panels_dir / "VERSION"
    if not version_file.is_file():
        raise PanelRegistryLoadError(
            "schema", f"no VERSION file at {version_file} -- the registry has no version"
        )
    version = version_file.read_text(encoding="utf-8").strip()
    if not version:
        raise PanelRegistryLoadError("schema", f"{version_file} is empty")
    return version


def _read_declarations(panels_dir: Path) -> list[PanelDeclaration]:
    declarations: list[PanelDeclaration] = []
    for filename in REGISTRY_FILENAMES:
        path = panels_dir / filename
        if not path.is_file():
            raise PanelRegistryLoadError(
                "schema", f"registry file {path} is missing -- all four files are required"
            )
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is None:
            loaded = []
        if not isinstance(loaded, list):
            raise PanelRegistryLoadError(
                "schema", f"{path} must contain a list of declarations, got {type(loaded).__name__}"
            )
        for index, item in enumerate(loaded):
            if not isinstance(item, dict):
                raise PanelRegistryLoadError(
                    "schema", f"{path} entry {index} is not a mapping"
                )
            try:
                declaration = PanelDeclaration(**{**item, "declared_in": filename})
            except PanelRegistryLoadError:
                raise
            except Exception as exc:  # pydantic ValidationError, TypeError
                raise PanelRegistryLoadError(
                    "schema", f"{path} entry {index} is not a valid declaration: {exc}"
                ) from exc
            declarations.append(declaration)
    return declarations


def _validate_enums(declaration: PanelDeclaration) -> None:
    if declaration.category not in _CATEGORIES:
        raise PanelRegistryLoadError(
            "schema",
            f"panel {declaration.panel_id!r} has category {declaration.category!r}; "
            f"expected one of {_CATEGORIES}",
        )
    if declaration.scope not in SCOPE_ORDER:
        raise PanelRegistryLoadError(
            "schema",
            f"panel {declaration.panel_id!r} has scope {declaration.scope!r}; "
            f"expected one of {SCOPE_ORDER}",
        )
    unknown = [s for s in declaration.story if s not in _STORIES]
    if unknown:
        raise PanelRegistryLoadError(
            "schema",
            f"panel {declaration.panel_id!r} names unknown user stories {unknown}; "
            f"expected a subset of {list(_STORIES)}",
        )


def _validate_p1_p2(declaration: PanelDeclaration) -> None:
    basis = (declaration.parity_basis or "").strip()
    if declaration.category == "in_game" and not basis:
        raise PanelRegistryLoadError(
            "P1",
            f"panel {declaration.panel_id!r} is category in_game with no parity_basis. "
            f"parity_basis is the field SC-005's per-panel audit reads; a panel "
            f"without one is not auditable by construction",
        )
    if declaration.category == "out_of_game_telemetry" and declaration.parity_basis is not None:
        raise PanelRegistryLoadError(
            "P2",
            f"panel {declaration.panel_id!r} is category out_of_game_telemetry but "
            f"declares parity_basis {declaration.parity_basis!r}. Harness telemetry "
            f"must be treated as out-of-game (FR-013), not laundered into looking "
            f"like a parity-cleared observation",
        )


def _validate_p3_p5(declaration: PanelDeclaration, schema: HarnessSchema) -> None:
    panel_rank = SCOPE_ORDER.index(declaration.scope)
    for source in declaration.parsed_source_fields:
        if not schema.has_entity(source.entity):
            raise PanelRegistryLoadError(
                "P3",
                f"panel {declaration.panel_id!r} declares source field {source} but "
                f"entity {source.entity!r} is not present in {schema.source.name}",
            )
        if not schema.has_field(source.entity, source.field):
            raise PanelRegistryLoadError(
                "P3",
                f"panel {declaration.panel_id!r} declares source field {source} but "
                f"{source.entity!r} has no field {source.field!r} in {schema.source.name}",
            )
        field_scope = schema.scope_of(source.entity)
        if field_scope is None:
            raise PanelRegistryLoadError(
                "P5",
                f"panel {declaration.panel_id!r} declares source field {source} whose "
                f"entity {source.entity!r} has no known scope; add it to "
                f"harness_schema.ENTITY_SCOPES before declaring a panel over it",
            )
        if SCOPE_ORDER.index(field_scope) > panel_rank:
            raise PanelRegistryLoadError(
                "P5",
                f"panel {declaration.panel_id!r} is {declaration.scope}-scoped but "
                f"declares {source}, which is {field_scope}-scoped. A coarser panel "
                f"reading a finer field is exactly the aggregation that could smuggle "
                f"step-level detail into what looks like a run-level summary",
            )


def _validate_p4(declarations: list[PanelDeclaration]) -> None:
    seen: dict[str, str] = {}
    for declaration in declarations:
        previous = seen.get(declaration.panel_id)
        if previous is not None:
            raise PanelRegistryLoadError(
                "P4",
                f"panel_id {declaration.panel_id!r} is declared in both {previous} and "
                f"{declaration.declared_in}. panel_id is one namespace across all four "
                f"files; a duplicate is ambiguous for ViewReference resolution",
            )
        seen[declaration.panel_id] = declaration.declared_in


def _validate_p6(
    declarations: list[PanelDeclaration], version: str, panels_dir: Path
) -> None:
    """Declarations are immutable within a version.

    Two halves, both checkable at load time:

    1. No declaration may claim a version later than the registry's own -- a
       panel marked ``introduced_in_version: "3"`` in a version-1 registry is
       either a mistake or an attempt to pre-register something.
    2. If ``panels/VERSION.lock`` records a frozen snapshot for the current
       version, every declaration must still hash to what it hashed then, and
       the panel set must be unchanged. Editing a declaration in place then
       means bumping ``panels/VERSION``, which is precisely what P6 asks for.

    The lock file is optional: a version under authorship has no snapshot yet,
    and this feature never writes one itself (it is a read-only process). A
    missing lock leaves only check 1 in force, which is stated here so its
    absence reads as a recorded limit rather than an oversight.
    """
    current = _version_key(version)
    for declaration in declarations:
        if _version_key(declaration.introduced_in_version) > current:
            raise PanelRegistryLoadError(
                "P6",
                f"panel {declaration.panel_id!r} declares introduced_in_version "
                f"{declaration.introduced_in_version!r}, later than the registry's own "
                f"version {version!r}",
            )

    lock_path = panels_dir / "VERSION.lock"
    if not lock_path.is_file():
        return
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    frozen = lock.get(version) if isinstance(lock, dict) else None
    if not isinstance(frozen, dict):
        return
    recorded: dict[str, str] = dict(frozen.get("panels") or {})
    actual = {d.panel_id: _hash(_declaration_payload(d)) for d in declarations}
    if recorded and actual != recorded:
        added = sorted(set(actual) - set(recorded))
        removed = sorted(set(recorded) - set(actual))
        changed = sorted(
            panel_id
            for panel_id in set(actual) & set(recorded)
            if actual[panel_id] != recorded[panel_id]
        )
        raise PanelRegistryLoadError(
            "P6",
            f"registry version {version!r} is frozen in {lock_path.name} but the "
            f"declarations differ (added={added}, removed={removed}, changed={changed}). "
            f"Declarations are immutable within a version -- bump panels/VERSION",
        )


def load_panel_registry(
    panels_dir: Path | None = None,
    harness_data_model: Path | None = None,
) -> PanelRegistry:
    """Load, validate (P1-P6), and index the Panel Registry.

    Raises ``PanelRegistryLoadError`` on any violation. Callers must not catch
    it and continue -- ``app.py`` and ``cli.py`` both let it abort startup.
    """
    panels_dir = panels_dir or default_panels_dir()
    if not panels_dir.is_dir():
        raise PanelRegistryLoadError("schema", f"panels directory {panels_dir} does not exist")

    version = _read_version(panels_dir)
    declarations = _read_declarations(panels_dir)
    schema = load_harness_schema(harness_data_model or default_harness_data_model_path())

    for declaration in declarations:
        _validate_enums(declaration)
        _validate_p1_p2(declaration)
        _validate_p3_p5(declaration, schema)
    _validate_p4(declarations)
    _validate_p6(declarations, version, panels_dir)

    by_id = {d.panel_id: d for d in declarations}
    by_source_field: dict[tuple[str, str], list[PanelDeclaration]] = {}
    for declaration in declarations:
        for source in declaration.parsed_source_fields:
            by_source_field.setdefault((source.entity, source.field), []).append(declaration)

    content_hash = _hash(
        {
            "version": version,
            "panels": sorted(
                (_declaration_payload(d) for d in declarations),
                key=lambda p: str(p["panel_id"]),
            ),
        }
    )

    return PanelRegistry(
        version=version,
        content_hash=content_hash,
        declarations=tuple(declarations),
        by_id=by_id,
        by_source_field={k: tuple(v) for k, v in by_source_field.items()},
    )
