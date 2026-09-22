"""Agent context assembly (T103) -- Principle I's structural enforcement point.

``DecisionRequest.system`` is role plus out-of-game guidance (FR-021);
``DecisionRequest.observation`` is this step's parity-filtered structured state
(FR-024). This module builds both, and the two hard exclusions the task
carries are enforced structurally rather than by convention:

- **No image is attached here.** ``images`` is always ``[]`` unless a caller
  explicitly passes one. The signature below accepts an explicit
  ``images: list[Image] | None`` parameter precisely so that extension is
  additive: :func:`select_screened_images` (T134) is the one function in this
  module permitted to turn a step's captures into that ``list[Image]`` --
  callers build it there, then pass the result straight through unmodified.
  ``assemble_context`` itself still never reads ``Observation.captures`` (the
  list of ``CaptureId`` shown at this step) at all, so wiring a capture id
  through here by itself can never attach an image -- the only path to a
  non-empty ``images`` list is a caller explicitly supplying one, built by
  the one function whose whole job is verifying it is safe to show.
- **Harness telemetry (FR-020) cannot reach the assembled text**, because the
  only inputs this module accepts are ``Observation`` (already parity-filtered
  upstream by ``observe/assemble.py``) and ``GuidanceSet`` (run-independent
  strategic guidance). Neither type has a field for model identity, cost,
  latency, retry counts, save lineage, run configuration, wall-clock timing,
  or the game build -- there is no attribute here to reach for. A future
  change that wanted to leak one of those fields into the agent's context
  would have to change the accepted parameter types first, which is exactly
  the friction FR-020's red-team test (T126) is meant to rely on.

**T134 -- the only way an image reaches the agent.** :func:`select_screened_images` is a pure
filter, not a data source: it never reads a store, never decodes bytes, and never invents an
``Image`` on its own. A caller (the run loop) hands it *candidates* -- each a
``(ScreenCapture, Image)`` pair it already has in hand, the ``Image`` already built from whatever
raw bytes it read back (via ``StepCapture.blob`` fresh off ``observe.capture.capture_for_step``, or
read back through the store) -- and gets back only the ``Image`` half of the candidates that pass
*all three* of FR-024/FR-025/FR-015's conditions:

1. ``capture.screening_status == ScreeningStatus.SCREENED_CLEAN`` -- an unscreened or withheld
   capture is never shown, full stop (research R7, SC-019).
2. ``capture.view_declaration_id`` resolves in the run's own catalog (via the ``registry`` the
   caller passes -- the same registry bound to the run's catalog version everywhere else in this
   codebase; this function trusts that binding rather than re-deriving it, matching
   ``act.dispatch.dispatch_action``'s and ``observe.assemble.assemble_observation``'s own trust of
   their own ``registry`` parameter).
3. ``capture.decision_step_id == observation.decision_step_id`` -- a prior step's capture, however
   clean, has no path into *this* step's context (FR-015): the board shown must reflect what the
   agent has already done this turn, never a stale frame.

A capture failing any one of the three is silently skipped, never raised on -- a caller iterating
every capture a run has ever produced and handing all of them to this function is expected usage,
not a caller error, so most candidates on a long-lived run are supposed to be filtered out here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from civsim_harness.act.availability import availability_by_action
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError
from civsim_harness.models.catalog import (
    LUA_ARGUMENT_TARGET,
    DeclarationKind,
    ParityDeclaration,
    TargetKind,
)
from civsim_harness.models.common import ModelRef
from civsim_harness.models.config import GuidanceSet
from civsim_harness.models.turn import (
    Observation,
    ObservationEntry,
    ScreenCapture,
    ScreeningStatus,
)
from civsim_harness.provider.port import DecisionRequest, Image

ROLE_TEXT: Final[str] = (
    "You are the sole player of a single human-controlled civilization in Sid Meier's "
    "Civilization VI. Everything shown to you below is exactly what a human player could see "
    "on their own screen at this moment, and every action available to you is one a human "
    "player could take with mouse and keyboard through the game's standard interface. You act "
    "for one civilization only, and you have no access to information or actions beyond what "
    "this context and the game's own user interface provide. At every decision step, choose "
    "exactly one action from those available to you now, and state your reasoning for it."
)
"""The fixed system role (FR-021's "role" half of ``system``).

Carries no run-specific, telemetry, or provenance content by construction --
it is a module-level constant, not something built from a run's records.
"""


def assemble_system_prompt(guidance: GuidanceSet | None) -> str:
    """Build ``DecisionRequest.system``: role plus out-of-game guidance (FR-021).

    ``guidance`` is optional per FR-021 ("MAY be supplied"); when present, its
    ``content`` is appended verbatim below the fixed role text. ``GuidanceSet``
    carries no run state and no hidden state by construction (only
    ``guidance_set_id``, ``content_hash``, ``content``, ``source_ref``), so
    nothing reachable from this function's parameters is telemetry or
    run-specific.
    """
    if guidance is None:
        return ROLE_TEXT
    return f"{ROLE_TEXT}\n\n---\n\nStrategic guidance:\n\n{guidance.content}"


def _render_entry(entry: ObservationEntry) -> str:
    rendered_value = json.dumps(entry.value, ensure_ascii=False, default=str)
    return f"- [{entry.context.value}] {entry.key}: {rendered_value}"


#: T262: the header above the commands whose availability predicate holds against *this* step's
#: observation. What a human sees in-client: a lit, clickable button or command on the panel that
#: is up right now -- "Found City" on a selected settler, "Next Turn" when nothing is blocking.
AVAILABLE_GROUP_HEADER: Final[str] = (
    "Available now -- the commands the game is offering you on this screen at this moment:"
)

#: T262: the header above the commands whose predicate does not hold. What a human sees
#: in-client: the same command greyed out, with a tooltip saying why -- "Promote" with no
#: promotable unit, "Make Peace" with nobody at war, an era-card chooser with no popup up. Left
#: listed, and choosable, because the refusal path is a real recorded behaviour (FR-017).
UNAVAILABLE_GROUP_HEADER: Final[str] = (
    "Not available now -- these are on the board but greyed out, and why. You may still choose "
    "one: it will be refused before it reaches the game, and the refusal is recorded."
)

#: T262: separates an action's rendered line from the reason it is greyed out. Rendered *after*
#: the T256 target tail so a reason can never be mistaken for part of the summary or the hint.
UNAVAILABLE_REASON_MARKER: Final[str] = " -- not available: "

#: What is rendered in place of a group's bullets when that group is empty. Deliberately not a
#: ``- `` bullet: nothing downstream that reads action lines may mistake it for one.
_EMPTY_GROUP: Final[str] = "(none)"


def assemble_action_catalog_text(
    declarations: Iterable[ParityDeclaration], *, observation: Observation | None = None
) -> str:
    """Render the actions the agent may choose from: every ``kind: action`` declaration's id and
    summary, plus the one convention it must follow to act on a subject.

    MEASURED (2026-09-21, the first model-driven runs on Linux): the model was never shown the
    catalog -- ``DecisionRequest`` carried the role and the observation only -- so it guessed
    ``units.found_city`` (correctly) and sent no ``target``, and the availability predicate
    (``unit.is_selected and ...``) bound ``unit`` to nothing: 24 of 24 decisions were refused as
    ``unavailable_to_human_now`` without ever reaching the game. What is listed here is what a
    human sees as the commands on screen, one line each -- ids and their summaries, no
    provenance, no predicate text (FR-020/FR-024).

    **T262 -- availability, the greyed-out button.** When *observation* is given, every action's
    own ``availability_predicate`` is evaluated against it (``act/availability.py``, the same
    predicate machinery ``act/dispatch.py`` uses at dispatch) and the catalog is rendered in the
    two groups a human's screen already shows: :data:`AVAILABLE_GROUP_HEADER` for the commands
    the game is offering, and :data:`UNAVAILABLE_GROUP_HEADER` for the ones that are greyed out,
    each carrying the plain-language reason a tooltip would give. The predicate's own source text
    is never rendered -- only its consequence, phrased in the same field vocabulary the observed
    state above already uses.

    MEASURED (2026-09-21, ``civsim store coverage``): of 38 catalog actions, 23 had ever been
    attempted and 9 ever applied; nine of the fourteen never-applied actions were draws for a
    situation that was not on screen, refused at dispatch as ``unavailable_to_human_now``. The
    agent was never told, because this function had no observation to tell it from.

    Without *observation* the rendering is exactly what it was before T262 -- one flat list --
    so a caller that has no observation in hand loses nothing and claims nothing.
    """
    lines = [
        "Actions you may take (choose exactly one per step by its id):",
        "Every action that acts on something names that thing in parameters.target, and nothing "
        "else unless its own line below names further parameters (a few orders take more than "
        "one value -- send exactly the keys that line lists, or the order is refused): a "
        "technology, civic, policy, belief, government or production item by its name "
        'exactly as the observed state lists it (e.g. {"target": "TECH_MINING"}); a destination '
        'plot as {"target": {"x": 43, "y": 31}}; another civilization, a congress resolution, a '
        "great person or a spy by its id. Unit and city orders act on the unit or city the game "
        "currently shows as selected (is_selected: true), so a unit order's target is the plot "
        "or promotion, not the unit; to act on a different unit, name its unit_id as the target "
        "of units.found_city or units.promote only when no target of another kind is needed. "
        "To change which unit or city is selected, first issue units.select or cities.select "
        "with that unit_id or city_id as the target (as a click on it would), then its orders.",
    ]
    actions = [
        declaration
        for declaration in sorted(declarations, key=lambda d: str(d.declaration_id))
        if declaration.kind is DeclarationKind.ACTION
    ]
    if observation is None:
        lines.extend(_render_action(declaration) for declaration in actions)
        return "\n".join(lines)

    status = availability_by_action(actions, observation)
    available = [row for row in actions if status[str(row.declaration_id)].available]
    unavailable = [row for row in actions if not status[str(row.declaration_id)].available]

    lines.append("")
    lines.append(AVAILABLE_GROUP_HEADER)
    lines.extend(_render_action(row) for row in available)
    if not available:
        lines.append(_EMPTY_GROUP)
    lines.append("")
    lines.append(UNAVAILABLE_GROUP_HEADER)
    lines.extend(
        _render_action(row)
        + UNAVAILABLE_REASON_MARKER
        + status[str(row.declaration_id)].reason
        for row in unavailable
    )
    if not unavailable:
        lines.append(_EMPTY_GROUP)
    return "\n".join(lines)


def _render_action(declaration: ParityDeclaration) -> str:
    summary = " ".join(str(declaration.summary or "").split())
    return (
        f"- {declaration.declaration_id}: {summary}"
        f"{_render_target(declaration)}{_render_extra_parameters(declaration)}"
    )


def _render_extra_parameters(declaration: ParityDeclaration) -> str:
    """`` -- also send: parameters.slot_index`` for an action whose Lua takes more than a target.

    Derived from the declaration's own ``lua_arguments``, never written out by hand: the model is
    the only caller that can supply these, so a hand-maintained sentence drifting away from the
    declaration would make the declared argument unreachable and every such action would refuse
    for a missing parameter. MEASURED (2026-09-22): across all 1600 action records in the store
    the parameter key-set is ``('target',)`` or ``()`` and never anything else -- because the
    paragraph above used to end "and nothing else".
    """
    if not declaration.lua_arguments:
        return ""
    extra = [entry for entry in declaration.lua_arguments if entry != LUA_ARGUMENT_TARGET]
    if not extra:
        return ""
    return " -- also send: " + ", ".join(extra)


#: T256: one concrete example per `TargetKind`, rendered after each action's summary so the
#: shape of the command is on the same line as the command -- the measured failure was a model
#: that read the convention paragraph above and still sent the unit's id where a plot was wanted.
_TARGET_KIND_EXAMPLES: Mapping[TargetKind, str] = {
    TargetKind.NONE: "no target (parameters: {})",
    TargetKind.PLOT: 'a plot, {"target": {"x": 43, "y": 31}}',
    TargetKind.UNIT_ID: 'a unit_id from units.state, {"target": 65536}',
    TargetKind.CITY_ID: 'a city_id from cities.state, {"target": 65537}',
    TargetKind.PLAYER_ID: 'another civilization\'s player_id from diplomacy.state, {"target": 3}',
    TargetKind.RESOLUTION_ID: 'a resolution_id from congress.state, {"target": 2}',
    TargetKind.INDIVIDUAL_ID: 'an individual_id from great_people.state, {"target": 12}',
    TargetKind.SPY_ID: 'a spy\'s unit_id from espionage.state, {"target": 131074}',
    TargetKind.NAME: 'a name exactly as the observed state lists it, {"target": "TECH_POTTERY"}',
    TargetKind.OPTION: 'one of the offered options, {"target": "continue"}',
    TargetKind.NUMBER: 'a number, {"target": 0.5}',
}


def _render_target(declaration: ParityDeclaration) -> str:
    """`` -- target: <example>; <hint>`` for a declaration that says its target kind; ``""``
    otherwise (an action authored before T256 renders exactly as it did)."""
    if declaration.target_kind is None:
        return ""
    rendered = f" -- target: {_TARGET_KIND_EXAMPLES[declaration.target_kind]}"
    hint = " ".join(str(declaration.target_hint or "").split())
    if hint:
        rendered += f"; {hint}"
    return rendered


def assemble_observation_text(
    observation: Observation, *, actions: Iterable[ParityDeclaration] | None = None
) -> str:
    """Build ``DecisionRequest.observation``: this step's parity-filtered state (FR-024).

    *actions*, when given, appends :func:`assemble_action_catalog_text` -- the commands a human
    would see -- after the observed state, split into the ones the game is offering right now
    and the ones it is showing greyed out (T262), evaluated against *this* observation.

    Only ``Observation.screen_identity`` and ``Observation.entries`` are
    rendered. Deliberately absent: ``observation_id``, ``decision_step_id``,
    ``assembled_at``, and ``catalog_version`` -- every one of those is
    out-of-game record-keeping (harness identity, timing, versioning), not
    game information a human player perceives, so none of them is written
    into text a model reads (FR-020). ``captures`` (the ``CaptureId`` list) is
    also absent: attaching images themselves is the ``images`` parameter's
    job (see module docstring), and a bare list of opaque capture ids would
    be pure harness provenance with no game-information content at all.
    """
    lines = [f"Current screen: {observation.screen_identity}", "", "Observed state:"]
    if not observation.entries:
        lines.append("(none)")
    else:
        lines.extend(_render_entry(entry) for entry in observation.entries)
    if actions is not None:
        lines.extend(["", assemble_action_catalog_text(actions, observation=observation)])
    return "\n".join(lines)


def assemble_context(
    *,
    observation: Observation,
    guidance: GuidanceSet | None,
    model: ModelRef,
    step_index: int,
    response_schema: dict[str, Any],
    images: list[Image] | None = None,
    actions: Iterable[ParityDeclaration] | None = None,
) -> DecisionRequest:
    """Assemble one decision step's complete ``DecisionRequest`` (T103).

    ``images`` defaults to none attached, regardless of what
    ``observation.captures`` lists (this function never inspects that field).
    The caller is expected to build ``images`` via :func:`select_screened_images`
    (T134) -- the one function that verifies each candidate capture is
    ``ScreeningStatus.SCREENED_CLEAN``, resolves in the run's own catalog, and
    belongs to *this* decision step -- and pass its result straight through.

    ``model`` is used only to populate ``DecisionRequest.model`` (which
    routes the call) -- it is never rendered into ``system`` or
    ``observation``, so model identity never becomes part of what the agent
    reads as game information (FR-020).
    """
    return DecisionRequest(
        model=model,
        system=assemble_system_prompt(guidance),
        observation=assemble_observation_text(observation, actions=actions),
        images=list(images) if images else [],
        step_index=step_index,
        response_schema=response_schema,
    )


def select_screened_images(
    *,
    observation: Observation,
    registry: CapabilityRegistry,
    candidates: Sequence[tuple[ScreenCapture, Image]],
) -> list[Image]:
    """T134: the only function permitted to turn a step's captures into agent-visible images.

    *candidates* is whatever the caller already has in hand -- typically every
    :class:`~civsim_harness.models.turn.ScreenCapture` on record for this run (or just this
    step), each paired with its already-decoded :class:`~civsim_harness.provider.port.Image`
    (built from ``StepCapture.blob`` fresh off
    :func:`~civsim_harness.observe.capture.capture_for_step`, or read back through the store).
    This function never reads a store and never decodes bytes itself -- it is a pure filter,
    nothing more.

    A candidate's ``Image`` is included in the result only when **all three** hold (FR-024,
    FR-025, FR-015); see the module docstring for the full rationale behind each:

    1. ``capture.screening_status is ScreeningStatus.SCREENED_CLEAN``.
    2. ``capture.view_declaration_id`` resolves in *registry* (the caller's own trust that
       *registry* is bound to this run's catalog version, matching how every other module in this
       codebase trusts its own ``registry`` parameter).
    3. ``capture.decision_step_id == observation.decision_step_id`` -- the capture belongs to the
       *current* decision step, never a prior one.

    A capture failing any one of the three is silently skipped, not raised on: iterating every
    capture a run has ever produced and handing all of them to this function is expected usage,
    and only the handful belonging to the current step could ever pass check 3 regardless. Order
    is preserved from *candidates*.
    """
    images: list[Image] = []
    for capture, image in candidates:
        if capture.screening_status is not ScreeningStatus.SCREENED_CLEAN:
            continue
        if capture.decision_step_id != observation.decision_step_id:
            continue
        try:
            declaration = registry.resolve(capture.view_declaration_id)
        except CatalogError:
            continue
        if declaration.kind is not DeclarationKind.VIEW:
            continue
        images.append(image)
    return images
