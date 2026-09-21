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

#: The marker 002's ``data-model.md`` puts on the italic line under an entity
#: heading, and in a field's Notes cell, to say the data is not game
#: information. Read case-insensitively; ``Out-of-game`` and ``out-of-game``
#: both appear in that document.
OUT_OF_GAME_MARKER = "out-of-game"

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

#: An entity whose *whole* section is marked out-of-game. Deliberately anchored
#: on ``*Out-of-game.*`` closing immediately: 002 also writes
#: ``*Mixed -- in-game content, out-of-game timing.*``,
#: ``*In-game (the image) with out-of-game provenance.*`` and
#: ``*Out-of-game in provenance, in-context for the agent.*``, none of which is
#: a blanket exemption. Those fall through to the per-field Notes check, which
#: is the fail-closed direction: a field nobody marked is treated as game
#: information and must therefore be registered or provably unrendered.
_OUT_OF_GAME_ENTITY = re.compile(r"^\*Out-of-game\.\*")

#: ``## 4. Run``, and ``## 10. ParityDeclaration (catalog entry)``.
#:
#: The trailing group is deliberately **only** a parenthesised aside, not "any
#: trailing prose" (T069). Requiring the name to end the line was the original
#: rule, and it silently dropped one of 002's fourteen numbered entities: its
#: heading carries a parenthetical, so the entity never matched, ``_OTHER_H2``
#: closed the block, and ``ParityDeclaration``'s twelve fields sat outside the
#: SC-005 coverage scan -- an audit ``contracts/panel-registry.md`` calls
#: release-blocking, reporting a total that was short by a whole entity while
#: ``civsim-web doctor`` printed it as fact.
#:
#: Widening all the way to ``(.*)$`` would have been the easy fix and the wrong
#: one: it would also match a prose heading like ``## 15. How runs are
#: archived``, inventing an entity out of a section title and filling it with
#: whatever table followed. A parenthetical is a *qualifier on a name*, which is
#: what this document actually uses it for.
_NUMBERED_ENTITY = re.compile(r"^##\s+\d+\.\s+(\w+)(?:\s+\([^)]*\))?\s*$")
_OTHER_H2 = re.compile(r"^##\s+")
_SUB_ENTITY = re.compile(r"^###\s+(\w+)\s*$")
_INLINE_ENTITY = re.compile(r"^\*\*(\w+)\*\*\s*[-—]")
_BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class HarnessSchema:
    """Entity -> field names, as published in 002's ``data-model.md``."""

    source: Path
    fields_by_entity: dict[str, frozenset[str]]
    out_of_game_entities: frozenset[str] = frozenset()
    """Entities whose section opens ``*Out-of-game.*`` -- the blanket marking."""

    out_of_game_fields: frozenset[tuple[str, str]] = frozenset()
    """``(entity, field)`` pairs whose own Notes cell says out-of-game."""

    def has_entity(self, entity: str) -> bool:
        return entity in self.fields_by_entity

    def has_field(self, entity: str, field: str) -> bool:
        return field in self.fields_by_entity.get(entity, frozenset())

    def scope_of(self, entity: str) -> str | None:
        """The scope this entity's fields belong to, or ``None`` if unmapped."""
        return ENTITY_SCOPES.get(entity)

    def is_out_of_game(self, entity: str, field: str) -> bool:
        """Is this field *explicitly* marked out-of-game by 002's document?

        The coverage rule in ``contracts/panel-registry.md`` exempts exactly
        these from needing a panel. Anything not marked is treated as game
        information, which is the direction that fails closed: a field 002
        forgets to mark must still be either registered or provably unrendered.
        """
        return entity in self.out_of_game_entities or (entity, field) in self.out_of_game_fields


def _row_cells(line: str) -> list[str] | None:
    """A markdown table data row's cells, or ``None`` if not a data row."""
    if not line.startswith("|"):
        return None
    cells = [cell.strip() for cell in line.split("|")[1:-1]]
    if not cells or not cells[0] or set(cells[0]) <= {"-", ":", " "}:
        return None  # the header separator row
    return cells


def _first_cell(line: str) -> str | None:
    """The first cell of a markdown table row, or ``None`` if not a data row."""
    cells = _row_cells(line)
    return cells[0] if cells else None


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

    The out-of-game marking is read at the same time, from two places: the
    italic line under an entity heading, and a field's own Notes cell. It is
    what ``contracts/panel-registry.md``'s coverage rule exempts from needing a
    panel, so the coverage reflection test (T059) needs it as data rather than
    as a hand-maintained list that could drift from the document it describes.
    """
    text = path.read_text(encoding="utf-8")
    fields: dict[str, set[str]] = {}
    out_of_game_entities: set[str] = set()
    out_of_game_fields: set[tuple[str, str]] = set()
    current: str | None = None

    for raw in text.splitlines():
        line = raw.strip()

        matched = _NUMBERED_ENTITY.match(line)
        if matched:
            current = matched.group(1)
            fields.setdefault(current, set())
            continue

        if current is not None and _OUT_OF_GAME_ENTITY.match(line):
            out_of_game_entities.add(current)
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
        cells = _row_cells(line)
        if cells is None:
            continue
        row_names = _BACKTICKED.findall(cells[0])
        if not row_names:
            continue
        fields[current].update(row_names)
        notes = " ".join(cells[1:]).lower()
        if OUT_OF_GAME_MARKER in notes:
            out_of_game_fields.update((current, name) for name in row_names)

    return HarnessSchema(
        source=path,
        fields_by_entity={k: frozenset(v) for k, v in fields.items() if v},
        out_of_game_entities=frozenset(out_of_game_entities),
        out_of_game_fields=frozenset(out_of_game_fields),
    )
