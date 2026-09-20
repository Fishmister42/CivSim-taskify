"""``PanelView`` -- one named panel, cut out of the page it appears on (T035/T038).

A panel reference (``/runs/{run}/turns/{n}/panels/{panel_id}``) is the finest
grain of FR-008's "stable reference that identifies run, turn, and panel". This
module is what a route hands that reference to.

**The one design decision worth reading.** A panel response is *never* built by
reading the store a second time. The route builds the enclosing view -- the
``RunDetailView``, ``TurnCycleView`` or ``DecisionStepView`` the panel appears
on -- serializes it once, and this module **selects** from that serialized form.
So the panel endpoint cannot show a value the page does not show, and cannot
omit one the page does: they are literally the same bytes, cut differently.
That is Principle VI at the panel grain, and it is a property of the code path
rather than a promise to keep two readers in sync (FR-007, UP-002, invariant V8).

The selection itself is *derived from the registry*, not from a hand-written
table of panel ids:

- ``ENTITY_PATHS`` says where each of 002's entities lives inside each enclosing
  view (``TurnCycle`` is the turn view's root, ``ModelCall`` sits at
  ``steps.*.decision.model_call``, and so on). One map per view shape.
- a panel's own ``source_fields`` say which of that entity's fields it may read.
  The field name *is* the key on the view model in almost every case, so the
  projection needs no per-panel entry.
- ``FIELD_ALIASES`` records the handful of places a view model deliberately
  renamed a store field (``RunConfiguration.map_seed`` became
  ``RunSummaryView.seed``; ``SavePoint.save_point_id`` became
  ``InterventionInfo.last_known_good_save_id``). Nine entries, each of which is
  a rename someone can read, rather than a mapping nobody can audit.

Adding a panel therefore needs no change here. Renaming a field on a view model
does -- and ``tests/contract/test_web_parity_boundary.py`` fails when a shipped
panel declares an entity this module cannot place, so the drift is caught on the
build rather than discovered as an empty panel.

**Absent is a value, not a 404.** A panel that is registered but has nothing to
show on this particular turn (no World Congress entry on turn 3) renders with
``is_present = false`` and a stated ``absent_reason``. Only an *unregistered*
``panel_id`` is a 404, because that is a client-side reference error rather than
a fact about the run (contracts/web-read-api.md, error table).
"""

from __future__ import annotations

from typing import Any

from civsim_web.registry.loader import PanelDeclaration
from civsim_web.registry.lookup import selector_pairs
from civsim_web.viewmodels.base import ViewModel
from civsim_web.viewmodels.gate import panel_basis_of
from civsim_web.viewmodels.provenance import Provenance

__all__ = [
    "ENTITY_PATHS",
    "FIELD_ALIASES",
    "PanelValue",
    "PanelView",
    "RUN_ENTITY_PATHS",
    "STEP_ENTITY_PATHS",
    "TURN_ENTITY_PATHS",
    "build_panel_view",
    "project_panel",
    "resolve_paths",
]


# --------------------------------------------------------------------------
# Where 002's entities live inside each enclosing view
# --------------------------------------------------------------------------

#: Within a ``TurnCycleView``. ``""`` means the view's own root; ``*`` matches
#: every index of a list.
TURN_ENTITY_PATHS: dict[str, tuple[str, ...]] = {
    "TurnCycle": ("",),
    "DecisionStep": ("steps.*",),
    "Observation": ("steps.*",),
    "Decision": ("steps.*.decision",),
    "ActionExecution": ("steps.*.decision",),
    "ModelCall": ("steps.*.decision.model_call",),
    "ScreenCapture": ("steps.*.capture",),
}

#: Within a ``DecisionStepView`` -- the same entities, one step in.
STEP_ENTITY_PATHS: dict[str, tuple[str, ...]] = {
    "DecisionStep": ("",),
    "Observation": ("",),
    "Decision": ("decision",),
    "ActionExecution": ("decision",),
    "ModelCall": ("decision.model_call",),
    "ScreenCapture": ("capture",),
}


def _prefixed(prefix: str, paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{prefix}.{path}" if path else prefix for path in paths)


#: Within a ``RunDetailView``. The turn entities are reachable too, through the
#: view's own ``current_turn`` -- which is why a run-scoped reference to a
#: turn-scoped panel resolves rather than 404ing: data-model.md SS12 says a
#: reference with no turn "resolves to the whole turn or run", and the run view
#: genuinely carries the current turn.
RUN_ENTITY_PATHS: dict[str, tuple[str, ...]] = {
    "Run": ("summary",),
    "RunConfiguration": ("summary",),
    "ModelConfig": ("summary",),
    "SavePoint": ("intervention_info",),
    "RunEvent": ("recent_events.*",),
    **{
        entity: _prefixed("current_turn", paths)
        for entity, paths in TURN_ENTITY_PATHS.items()
    },
}

#: Every map above, for the coverage check in
#: ``tests/contract/test_web_parity_boundary.py``.
ENTITY_PATHS: dict[str, dict[str, tuple[str, ...]]] = {
    "run": RUN_ENTITY_PATHS,
    "turn": TURN_ENTITY_PATHS,
    "step": STEP_ENTITY_PATHS,
}


#: The places a view model deliberately renamed a store field. Every entry is a
#: rename a reader can check against the model it names; a field absent from
#: this map is assumed to keep its store name, which is true of every other
#: field in 002's data model that this feature renders.
FIELD_ALIASES: dict[tuple[str, str], tuple[str, ...]] = {
    # RunSummaryView flattens RunConfiguration's columns (data-model.md SS1).
    ("RunConfiguration", "map_seed"): ("seed",),
    ("RunConfiguration", "model_config"): ("model_primary",),
    ("ModelConfig", "primary"): ("model_primary",),
    # InterventionInfo names the *last known good* save, not a save in general
    # (data-model.md SS4) -- the rename is the point of the model.
    ("SavePoint", "save_point_id"): ("last_known_good_save_id",),
    ("SavePoint", "turn_number"): ("last_known_good_turn",),
    ("SavePoint", "save_name"): ("last_known_good_save_name",),
    # DecisionView flattens ActionExecution onto itself (data-model.md SS8).
    ("Decision", "execution"): ("execution_outcome", "rejection_reason"),
    ("ActionExecution", "outcome"): ("execution_outcome",),
    # CaptureView resolves withheld_reason into its own displayed vocabulary
    # (data-model.md SS7, plus the `unrecognized_status` value US1 added).
    ("ScreenCapture", "withheld_reason"): ("unavailable_reason",),
}


# --------------------------------------------------------------------------
# The models
# --------------------------------------------------------------------------


class PanelValue(ViewModel):
    """One value this panel accounts for, and where it sits on the page."""

    path: str
    """The JSON path of this value *in the enclosing view* -- the same dotted
    path the page emits as ``data-field``, so a reader can point at the panel
    endpoint and at the spot on the page interchangeably."""

    source: str
    """The 002 ``Entity.field`` declaration that permitted this value to be
    read, verbatim from the panel's ``source_fields`` (SC-005 auditability)."""

    value: Any = None


class PanelView(ViewModel):
    """One registered panel, resolved against one run, turn, or step.

    Carries the panel's declaration in full -- title, scope, category, and its
    restated ``parity_basis`` -- because a reference handed to the directing
    session should explain what it is looking at and on what parity grounds,
    without a second lookup against the registry (FR-012, SC-005).
    """

    run_id: str
    turn_number: int | None = None
    step_index: int | None = None

    panel_id: str
    reference: str
    """This panel's own canonical ``ViewReference`` path."""

    context_reference: str
    """The enclosing view this panel was cut from. Both readers can open it and
    see the same values in place (FR-007)."""

    title: str
    scope: str
    category: str
    panel_basis: str
    """The restated ``parity_basis`` for an ``in_game`` panel, or the explicit
    ``out_of_game_telemetry`` marker for a telemetry one (rule P2)."""

    story: tuple[str, ...] = ()
    source_fields: tuple[str, ...] = ()
    introduced_in_version: str = ""
    declared_in: str = ""

    values: tuple[PanelValue, ...] = ()
    is_present: bool = False
    absent_reason: str | None = None
    """Why this panel has no values here -- never left blank, since an empty
    panel a reader cannot explain is the absence-as-fact UP-005 forbids."""

    provenance: Provenance


# --------------------------------------------------------------------------
# Path resolution over a serialized view model
# --------------------------------------------------------------------------


def resolve_paths(data: Any, pattern: str) -> list[tuple[str, Any]]:
    """Every ``(concrete path, value)`` in ``data`` matching a wildcard pattern.

    ``""`` is the root; ``*`` matches every index of a list. A pattern that
    walks through a missing key or a ``null`` simply yields nothing -- a run
    with no current turn has no turn-scoped panels resolvable at run scope, and
    that is an absence to report, not an error.
    """
    frontier: list[tuple[str, Any]] = [("", data)]
    if not pattern:
        return frontier
    for part in pattern.split("."):
        nxt: list[tuple[str, Any]] = []
        for path, value in frontier:
            if part == "*":
                if isinstance(value, list):
                    nxt.extend(
                        (f"{path}.{index}" if path else str(index), item)
                        for index, item in enumerate(value)
                    )
                continue
            if isinstance(value, dict) and part in value:
                nxt.append((f"{path}.{part}" if path else part, value[part]))
        frontier = nxt
    return frontier


def _join(base: str, key: str) -> str:
    return f"{base}.{key}" if base else key


def _observation_entries(
    data: Any, bases: tuple[str, ...], panel_id: str, source: str
) -> list[PanelValue]:
    """Entries this panel is the registered owner of (data-model.md SS6).

    ``ObservationEntryView`` already carries the ``panel_id`` the registry
    resolved it to, so this needs no second selector match -- it reads the
    decision the view model already made, which is what keeps the panel endpoint
    from being able to disagree with the page.
    """
    found: list[PanelValue] = []
    for base in bases:
        for path, node in resolve_paths(data, _join(base, "observation")):
            if not isinstance(node, list):
                continue
            for index, entry in enumerate(node):
                if isinstance(entry, dict) and entry.get("panel_id") == panel_id:
                    found.append(
                        PanelValue(path=f"{path}.{index}", source=source, value=entry)
                    )
    return found


def _action_values(
    data: Any, bases: tuple[str, ...], declaration_id: str, source: str
) -> list[PanelValue]:
    """The decisions that issued one specific action declaration.

    The mechanism behind ``DecisionView.action_label``: a panel naming an action
    through ``[declaration_id=...]`` owns the decisions that used it.
    """
    found: list[PanelValue] = []
    for base in bases:
        for path, node in resolve_paths(data, base):
            if not isinstance(node, dict):
                continue
            if node.get("action_declaration_id") != declaration_id:
                continue
            for key in ("action_declaration_id", "action_label"):
                if key in node:
                    found.append(
                        PanelValue(path=_join(path, key), source=source, value=node[key])
                    )
    return found


def project_panel(
    panel: PanelDeclaration,
    data: Any,
    entity_paths: dict[str, tuple[str, ...]],
) -> tuple[tuple[PanelValue, ...], tuple[str, ...]]:
    """``(values, unplaceable entities)`` for one panel over one serialized view.

    The second half of the pair is the honest report: an entity this view has no
    place for means the panel is declared but nothing on this page reads it, and
    saying so beats rendering an empty panel that looks like a recording gap.
    """
    found: list[PanelValue] = []
    unplaceable: list[str] = []

    for source in panel.parsed_source_fields:
        bases = entity_paths.get(source.entity)
        if bases is None:
            if source.entity not in unplaceable:
                unplaceable.append(source.entity)
            continue

        selector = selector_pairs(source)
        declaration_id = selector.get("declaration_id")
        label = str(source)

        if declaration_id and (source.entity, source.field) == ("Observation", "entries"):
            found.extend(_observation_entries(data, bases, panel.panel_id, label))
            continue
        if declaration_id and (source.entity, source.field) == (
            "Decision",
            "action_declaration_id",
        ):
            found.extend(_action_values(data, bases, declaration_id, label))
            continue

        for key in FIELD_ALIASES.get((source.entity, source.field), (source.field,)):
            for base in bases:
                for path, node in resolve_paths(data, base):
                    if isinstance(node, dict) and key in node:
                        found.append(
                            PanelValue(path=_join(path, key), source=label, value=node[key])
                        )

    # A value reachable by two declarations (``RunConfiguration.model_config``
    # and ``ModelConfig.primary`` both land on ``summary.model_primary``) is one
    # value on the page and must be one value here.
    deduped: dict[str, PanelValue] = {}
    for item in found:
        deduped.setdefault(item.path, item)
    return (tuple(deduped.values()), tuple(unplaceable))


# --------------------------------------------------------------------------
# Building the response
# --------------------------------------------------------------------------

NOT_RECORDED = (
    "This panel is registered but {context} carries no value for it. That is an "
    "absence in the record, not a missing panel -- the declaration below is in "
    "force either way."
)

NOT_ON_THIS_VIEW = (
    "This panel reads {entities}, which {context} does not carry. Open the view "
    "whose scope matches the panel's own ({scope}) to see it."
)


def build_panel_view(
    panel: PanelDeclaration,
    *,
    data: Any,
    entity_paths: dict[str, tuple[str, ...]],
    reference: str,
    context_reference: str,
    run_id: str,
    provenance: Provenance,
    turn_number: int | None = None,
    step_index: int | None = None,
) -> PanelView:
    """Cut one panel out of an already-built enclosing view.

    ``data`` is that view's ``model_dump(mode="json")`` -- the same object the
    JSON body and the HTML template are built from. Nothing here reads the
    store.
    """
    values, unplaceable = project_panel(panel, data, entity_paths)

    absent_reason: str | None = None
    if not values:
        if unplaceable:
            absent_reason = NOT_ON_THIS_VIEW.format(
                entities=", ".join(unplaceable),
                context=context_reference,
                scope=panel.scope,
            )
        else:
            absent_reason = NOT_RECORDED.format(context=context_reference)

    return PanelView(
        run_id=run_id,
        turn_number=turn_number,
        step_index=step_index,
        panel_id=panel.panel_id,
        reference=reference,
        context_reference=context_reference,
        title=panel.title,
        scope=panel.scope,
        category=panel.category,
        panel_basis=panel_basis_of(panel),
        story=panel.story,
        source_fields=panel.source_fields,
        introduced_in_version=panel.introduced_in_version,
        declared_in=panel.declared_in,
        values=values,
        is_present=bool(values),
        absent_reason=absent_reason,
        provenance=provenance,
    )
