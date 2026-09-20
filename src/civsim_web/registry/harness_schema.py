"""The published 002 data model, read as data (Panel Registry rule P3).

Rule P3 says every ``source_fields`` entry "must resolve to a field actually
present in 002's published ``data-model.md``". That is a check against a
*document*, deliberately: the registry's job is to stay honest about a schema
this feature does not own and cannot see the code of, and the document is what
002 publishes. Parsing it at load time catches drift between the registry and
002's schema when the registry loads, rather than at render time on a page
someone is looking at.

The parse is intentionally forgiving in one direction and strict in the other:
an entity or field this parser fails to recognise means a registry declaration
gets rejected (loud, at startup, fixable), never that an unregistered field
slips through (silent, at render time, invisible). Failing closed is the whole
point of the second gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ENTITY_SCOPES",
    "HarnessSchema",
    "SCOPE_ORDER",
    "load_harness_schema",
]

#: Panel scopes, coarsest first. A panel may declare fields at or coarser than
#: its own scope, never finer: a ``run``-scoped panel listing a
#: ``DecisionStep``-level field is exactly the aggregation P5 forbids, because
#: it would smuggle step detail into what reads as a run-level summary.
SCOPE_ORDER: tuple[str, ...] = ("run", "turn", "step")

#: Which scope each entity in 002's data model belongs to.
#:
#: ``RunEvent`` is run-scoped even though it carries optional ``turn_number``
#: and ``step_index``: the timeline is a property of the run, and its entries
#: are attributable to a turn rather than owned by one.
ENTITY_SCOPES: dict[str, str] = {
    # run-scoped
    "SeedSet": "run",
    "BuildAcceptance": "run",
    "RunConfiguration": "run",
    "StopCondition": "run",
    "ModelConfig": "run",
    "GuidanceSet": "run",
    "Run": "run",
    "SavePoint": "run",
    "RunEvent": "run",
    "ParityDeclaration": "run",
    "CatalogVersion": "run",
    "IntegrationCapability": "run",
    # turn-scoped
    "TurnCycle": "turn",
    # step-scoped
    "DecisionStep": "step",
    "Observation": "step",
    "ObservationEntry": "step",
    "ScreenCapture": "step",
    "Decision": "step",
    "ActionExecution": "step",
    "ModelCall": "step",
}

_NUMBERED_ENTITY = re.compile(r"^##\s+\d+\.\s+(\w+)\s*$")
_OTHER_H2 = re.compile(r"^##\s+")
_SUB_ENTITY = re.compile(r"^###\s+(\w+)\s*$")
_INLINE_ENTITY = re.compile(r"^\*\*(\w+)\*\*\s*[-—]")
_BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class HarnessSchema:
    """Entity -> field names, as published in 002's ``data-model.md``."""

    source: Path
    fields_by_entity: dict[str, frozenset[str]]

    def has_entity(self, entity: str) -> bool:
        return entity in self.fields_by_entity

    def has_field(self, entity: str, field: str) -> bool:
        return field in self.fields_by_entity.get(entity, frozenset())

    def scope_of(self, entity: str) -> str | None:
        """The scope this entity's fields belong to, or ``None`` if unmapped."""
        return ENTITY_SCOPES.get(entity)


def _first_cell(line: str) -> str | None:
    """The first cell of a markdown table row, or ``None`` if not a data row."""
    if not line.startswith("|"):
        return None
    cells = line.split("|")
    if len(cells) < 3:
        return None
    cell = cells[1].strip()
    if not cell or set(cell) <= {"-", ":", " "}:
        return None  # the header separator row
    return cell


def load_harness_schema(path: Path) -> HarnessSchema:
    """Parse 002's ``data-model.md`` into an entity -> field-name index.

    Three shapes carry fields in that document and all three are read:

    1. ``## 4. Run`` followed by a field table -- the main entities.
    2. ``### CatalogVersion`` / ``**ObservationEntry**`` on a line of their own,
       followed by a field table -- sub-entities.
    3. ``**ModelConfig** -- `primary: ModelRef`, `fallbacks: ...``` -- entities
       defined inline in a sentence rather than in a table of their own.

    A table cell may name two fields at once (``` `started_at` / `ended_at` ```);
    every backticked identifier in the first cell is taken as a field name.
    """
    text = path.read_text(encoding="utf-8")
    fields: dict[str, set[str]] = {}
    current: str | None = None

    for raw in text.splitlines():
        line = raw.strip()

        matched = _NUMBERED_ENTITY.match(line)
        if matched:
            current = matched.group(1)
            fields.setdefault(current, set())
            continue
        if _OTHER_H2.match(line):
            # A non-entity section (`## Entity overview`, `## Cross-cutting
            # invariants`). Drop the current entity so stray tables under it
            # are never mistaken for its fields.
            current = None
            continue

        matched = _SUB_ENTITY.match(line)
        if matched:
            current = matched.group(1)
            fields.setdefault(current, set())
            continue

        if line.startswith("**"):
            bold_only = re.match(r"^\*\*(\w+)\*\*\s*$", line)
            if bold_only:
                current = bold_only.group(1)
                fields.setdefault(current, set())
                continue
            inline = _INLINE_ENTITY.match(line)
            if inline:
                entity = inline.group(1)
                names = set(_BACKTICKED.findall(line[inline.end() :]))
                if names:
                    fields.setdefault(entity, set()).update(names)
                # An inline definition does not change the current table
                # context -- `**Validation**` notes follow entity tables.
                continue

        if current is None:
            continue
        cell = _first_cell(line)
        if cell is None:
            continue
        names = _BACKTICKED.findall(cell)
        if names:
            fields[current].update(names)

    return HarnessSchema(
        source=path,
        fields_by_entity={k: frozenset(v) for k, v in fields.items() if v},
    )
