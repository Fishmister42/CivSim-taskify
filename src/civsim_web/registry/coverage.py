"""Registry coverage: what 002 records, and what this feature can render (T059).

``contracts/panel-registry.md``'s Conformance section states the property:

    every field enumerated in 002's ``data-model.md`` that is not explicitly
    marked out-of-game in that document either has a corresponding panel here
    or is confirmed absent from every view model by a reflection-based test --
    so a newly added store field defaults to *invisible* until someone
    deliberately registers it, never to *visible-by-omission*.

``viewmodels/gate.py`` is what makes that true at runtime: an unregistered
field returns before the record is touched. This module is the check that the
mechanism has no bypass -- that nothing renders a field the registry never
permitted, by some other path.

**The reflection is static and structural, not a name grep.** For each 002
entity, ``viewmodels/panel.py::ENTITY_PATHS`` already says where that entity
sits inside each enclosing view (``ModelCall`` at ``steps.*.decision.model_call``
in a ``TurnCycleView``, and so on). Walking that path through the pydantic
models' own annotations yields the exact set of keys the response can carry at
the place that entity lives. An unregistered field whose name -- or one of its
``FIELD_ALIASES`` renames -- appears in that set is reachable and is the defect;
one that does not is invisible, which is the default the rule asks for.

Two directions are deliberately *not* checked here, and saying so is the point
of writing this down:

- A view-model field that is not a store field at all (``reference``,
  ``provenance``, ``completeness``) is not examined. It carries nothing 002
  recorded, so it cannot leak a store field.
- An entity with no ``ENTITY_PATHS`` placement renders nowhere, so all of its
  fields are invisible. That is a true answer, not an unchecked one:
  ``tests/contract/test_web_parity_boundary.py`` separately asserts that every
  *declared* panel's entity is placeable, so an entity can be unplaced only
  while no panel declares it. The one exception is ``ObservationEntry``, which
  does render but is reached only through another entity's registered field --
  see ``REACHED_THROUGH``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, get_args

from pydantic import BaseModel

from civsim_web.registry.harness_schema import HarnessSchema
from civsim_web.registry.loader import PanelRegistry

__all__ = [
    "CoverageReport",
    "REACHED_THROUGH",
    "ReachedThrough",
    "RenderableField",
    "assess_registry_coverage",
    "rendered_field_names",
]


@dataclass(frozen=True)
class ReachedThrough:
    """An entity this feature renders only *inside* another entity's field."""

    owner: tuple[str, str]
    """The ``(entity, field)`` a panel must declare to reach it."""

    paths: dict[str, tuple[str, ...]]
    """Where it sits, per enclosing-view scope."""


#: ``ObservationEntry`` is the whole of this list, and it is here because the
#: registry deliberately declares entries at the *collection* grain:
#: ``Observation.entries[declaration_id=cities.yields]``. There is no
#: ``ObservationEntry.value`` declaration anywhere and there should not be --
#: data-model.md SS6's drop rule already refuses any entry whose
#: ``declaration_id`` does not resolve to a panel, so an entry that renders at
#: all is one the registry named. Treating its fields as covered by
#: ``Observation.entries`` states that; classing them "invisible" because the
#: entity has no ``ENTITY_PATHS`` row would have been quietly false, since
#: ``ObservationEntryView.value`` plainly renders.
REACHED_THROUGH: dict[str, ReachedThrough] = {
    "ObservationEntry": ReachedThrough(
        owner=("Observation", "entries"),
        paths={
            "run": ("current_turn.steps.*.observation.*",),
            "turn": ("steps.*.observation.*",),
            "step": ("observation.*",),
        },
    ),
}


@dataclass(frozen=True)
class RenderableField:
    """One 002 field that a view model can render, with where it surfaces."""

    entity: str
    field: str
    rendered_as: tuple[str, ...]
    """The key(s) on the view model -- the store name, or its ``FIELD_ALIASES``
    rename(s)."""

    views: tuple[str, ...]
    """Which enclosing view scopes (``run``/``turn``/``step``) carry it."""

    def __str__(self) -> str:
        return f"{self.entity}.{self.field}"


@dataclass(frozen=True)
class CoverageReport:
    """The coverage rule's verdict over one registry and one 002 schema."""

    scanned: int
    """Total ``Entity.field`` pairs enumerated in 002's ``data-model.md``."""

    out_of_game: tuple[str, ...]
    """Explicitly marked out-of-game by 002, so exempt from needing a panel."""

    registered: tuple[str, ...]
    """In-game and declared by at least one panel."""

    invisible: tuple[str, ...]
    """In-game, unregistered, and absent from every view model -- the default
    the rule asks a new store field to land in."""

    reachable_but_unregistered: tuple[RenderableField, ...]
    """The violations: renderable without a panel permitting it. Must be
    empty."""

    @property
    def ok(self) -> bool:
        return not self.reachable_but_unregistered

    def summary(self) -> str:
        """One line, in the shape ``civsim-web doctor`` prints."""
        return (
            f"{self.scanned} fields scanned, "
            f"{len(self.out_of_game)} marked out-of-game, "
            f"{len(self.registered)} registered, "
            f"{len(self.invisible)} unregistered and unrendered, "
            f"{len(self.reachable_but_unregistered)} unregistered fields "
            f"reachable from a view model"
        )


# --------------------------------------------------------------------------
# Static reflection over the view models
# --------------------------------------------------------------------------


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    """Every ``BaseModel`` reachable from one annotation.

    Unwraps the containers this feature's models actually use --
    ``X | None``, ``tuple[X, ...]``, ``list[X]``, ``dict[str, list[X]]`` -- by
    recursing into type arguments rather than special-casing each one.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for argument in get_args(annotation):
        found.extend(_models_in(argument))
    return found


def _fields_at(root: type[BaseModel], pattern: str) -> frozenset[str]:
    """Field names of the model(s) reached by walking ``pattern`` from ``root``.

    ``""`` is the root itself. ``*`` is a no-op: descending into a
    ``tuple[Step, ...]`` annotation has already dropped the container, so the
    index segment has nothing left to do.
    """
    current: list[type[BaseModel]] = [root]
    if pattern:
        for part in pattern.split("."):
            if part == "*":
                continue
            nxt: list[type[BaseModel]] = []
            for model in current:
                info = model.model_fields.get(part)
                if info is not None:
                    nxt.extend(_models_in(info.annotation))
            current = nxt
    names: set[str] = set()
    for model in current:
        names.update(model.model_fields)
    return frozenset(names)


def rendered_field_names() -> dict[str, dict[str, frozenset[str]]]:
    """``{view scope: {002 entity: the keys that entity can carry there}}``.

    Built from ``ENTITY_PATHS`` and the view models' own annotations, so it
    tracks a renamed or removed view-model field with no edit here.
    """
    # Imported lazily: `viewmodels.panel` imports the registry, and this module
    # is imported by `cli.py` before an app exists.
    from civsim_web.viewmodels.panel import ENTITY_PATHS
    from civsim_web.viewmodels.run_detail import RunDetailView
    from civsim_web.viewmodels.step import DecisionStepView
    from civsim_web.viewmodels.turn import TurnCycleView

    roots: dict[str, type[BaseModel]] = {
        "run": RunDetailView,
        "turn": TurnCycleView,
        "step": DecisionStepView,
    }
    rendered: dict[str, dict[str, frozenset[str]]] = {}
    for scope, entity_paths in ENTITY_PATHS.items():
        root = roots[scope]
        placed = {
            entity: frozenset().union(*(_fields_at(root, p) for p in paths))
            for entity, paths in entity_paths.items()
        }
        for entity, reached in REACHED_THROUGH.items():
            paths = reached.paths.get(scope, ())
            if paths:
                placed[entity] = frozenset().union(*(_fields_at(root, p) for p in paths))
        rendered[scope] = placed
    return rendered


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def assess_registry_coverage(
    registry: PanelRegistry, schema: HarnessSchema
) -> CoverageReport:
    """Classify every 002 field as out-of-game, registered, invisible, or leaked."""
    from civsim_web.viewmodels.panel import FIELD_ALIASES

    rendered = rendered_field_names()

    out_of_game: list[str] = []
    registered: list[str] = []
    invisible: list[str] = []
    leaked: list[RenderableField] = []
    scanned = 0

    for entity in sorted(schema.fields_by_entity):
        for field in sorted(schema.fields_by_entity[entity]):
            scanned += 1
            name = f"{entity}.{field}"
            if schema.is_out_of_game(entity, field):
                out_of_game.append(name)
                continue
            if registry.panels_for_field(entity, field):
                registered.append(name)
                continue
            reached = REACHED_THROUGH.get(entity)
            if reached is not None and registry.panels_for_field(*reached.owner):
                registered.append(name)
                continue

            keys = FIELD_ALIASES.get((entity, field), (field,))
            hits = {
                scope: sorted(set(keys) & rendered[scope].get(entity, frozenset()))
                for scope in rendered
            }
            surfaced = {scope: found for scope, found in hits.items() if found}
            if surfaced:
                leaked.append(
                    RenderableField(
                        entity=entity,
                        field=field,
                        rendered_as=tuple(sorted({k for v in surfaced.values() for k in v})),
                        views=tuple(sorted(surfaced)),
                    )
                )
            else:
                invisible.append(name)

    return CoverageReport(
        scanned=scanned,
        out_of_game=tuple(out_of_game),
        registered=tuple(registered),
        invisible=tuple(invisible),
        reachable_but_unregistered=tuple(leaked),
    )
