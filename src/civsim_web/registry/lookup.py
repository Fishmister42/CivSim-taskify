"""Resolving a store record back to the panel permitted to render it.

``registry/loader.py`` builds the ``(entity, field) -> panels`` index. This
module answers the two questions a view-model constructor actually asks of it,
both of which need the *selector* half of a ``source_fields`` entry that the
index alone does not carry:

1. **Which panel may render this observation entry?**
   ``contracts/panel-registry.md``'s own worked example declares
   ``Observation.entries[declaration_id=cities.yields]`` -- the selector names
   which of 002's observation declarations the panel covers. data-model.md SS6
   then makes the closed-list rule concrete: *"an
   ``ObservationEntry.declaration_id`` that does not resolve to a Panel Registry
   entry is dropped, not passed through with a placeholder label."* Dropping is
   only possible if something can perform that resolution; this is it.

2. **What is this action's human-facing label?**
   Same mechanism, over ``Decision.action_declaration_id[declaration_id=...]``.

Both return ``None`` for an unregistered id, and ``None`` means *drop*, never
*render with a fallback*: the registry is a closed list, and a fallback label
would be precisely the "forgetting to apply the filter" failure UP-001 exists
to make impossible.
"""

from __future__ import annotations

import re

from civsim_web.registry.loader import PanelDeclaration, PanelRegistry, SourceField

__all__ = [
    "ACTION_DECLARATION_FIELD",
    "OBSERVATION_ENTRIES_FIELD",
    "panel_for_action",
    "panel_for_observation_entry",
    "selector_pairs",
]

#: ``(entity, field)`` of the source-field entry each lookup keys off.
OBSERVATION_ENTRIES_FIELD: tuple[str, str] = ("Observation", "entries")
ACTION_DECLARATION_FIELD: tuple[str, str] = ("Decision", "action_declaration_id")

_PAIR = re.compile(r"(?P<key>[A-Za-z_]\w*)\s*=\s*(?P<value>[^,\]]+)")


def selector_pairs(source: SourceField) -> dict[str, str]:
    """Parse ``[declaration_id=cities.state]`` into ``{"declaration_id": ...}``.

    An entry with no selector yields an empty mapping, which reads naturally as
    "this panel declares the whole field, unfiltered".
    """
    if not source.selector:
        return {}
    return {
        match.group("key"): match.group("value").strip()
        for match in _PAIR.finditer(source.selector)
    }


def _panel_by_selector(
    registry: PanelRegistry,
    field: tuple[str, str],
    key: str,
    value: str,
) -> PanelDeclaration | None:
    entity, name = field
    for panel in registry.panels_for_field(entity, name):
        for source in panel.parsed_source_fields:
            if (source.entity, source.field) != field:
                continue
            if selector_pairs(source).get(key) == value:
                return panel
    return None


def panel_for_observation_entry(
    registry: PanelRegistry, declaration_id: str
) -> PanelDeclaration | None:
    """The panel permitted to render entries of ``declaration_id``, or ``None``.

    ``None`` is the drop signal of data-model.md SS6. It is expected to be
    rare -- 002's own catalog already filters what it records -- but the rule
    holds regardless of how rare, since "rare" is not "never".
    """
    return _panel_by_selector(
        registry, OBSERVATION_ENTRIES_FIELD, "declaration_id", declaration_id
    )


def panel_for_action(
    registry: PanelRegistry, action_declaration_id: str
) -> PanelDeclaration | None:
    """The panel supplying a human-facing label for one action declaration.

    data-model.md SS8 wants ``DecisionView.action_label`` sourced from the
    registry rather than from the raw ``action_declaration_id``. Where no panel
    names the specific action, the caller falls back to the declaration id and
    says so -- see ``viewmodels/decision.py``, which records why that fallback
    exists rather than inventing a label.
    """
    return _panel_by_selector(
        registry, ACTION_DECLARATION_FIELD, "declaration_id", action_declaration_id
    )
