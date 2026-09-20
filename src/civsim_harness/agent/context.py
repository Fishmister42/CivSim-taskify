"""Agent context assembly (T103) -- Principle I's structural enforcement point.

``DecisionRequest.system`` is role plus out-of-game guidance (FR-021);
``DecisionRequest.observation`` is this step's parity-filtered structured state
(FR-024). This module builds both, and the two hard exclusions the task
carries are enforced structurally rather than by convention:

- **No image is attached here.** ``images`` is always ``[]`` unless a caller
  explicitly passes one -- and no caller should until T134 exists, once US2's
  screening gate is in place. The signature below accepts an explicit
  ``images: list[Image] | None`` parameter precisely so that extension is
  additive: T134 will pass a ``list[Image]`` built from captures it has
  *itself* verified as ``ScreeningStatus.SCREENED_CLEAN``. This module never
  reads ``Observation.captures`` (the list of ``CaptureId`` shown at this
  step) at all, so wiring a capture id through here by itself can never
  attach an image -- the only path to a non-empty ``images`` list is a caller
  explicitly supplying one.
- **Harness telemetry (FR-020) cannot reach the assembled text**, because the
  only inputs this module accepts are ``Observation`` (already parity-filtered
  upstream by ``observe/assemble.py``) and ``GuidanceSet`` (run-independent
  strategic guidance). Neither type has a field for model identity, cost,
  latency, retry counts, save lineage, run configuration, wall-clock timing,
  or the game build -- there is no attribute here to reach for. A future
  change that wanted to leak one of those fields into the agent's context
  would have to change the accepted parameter types first, which is exactly
  the friction FR-020's red-team test (T126) is meant to rely on.
"""

from __future__ import annotations

import json
from typing import Any, Final

from civsim_harness.models.common import ModelRef
from civsim_harness.models.config import GuidanceSet
from civsim_harness.models.turn import Observation, ObservationEntry
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


def assemble_observation_text(observation: Observation) -> str:
    """Build ``DecisionRequest.observation``: this step's parity-filtered state (FR-024).

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
    return "\n".join(lines)


def assemble_context(
    *,
    observation: Observation,
    guidance: GuidanceSet | None,
    model: ModelRef,
    step_index: int,
    response_schema: dict[str, Any],
    images: list[Image] | None = None,
) -> DecisionRequest:
    """Assemble one decision step's complete ``DecisionRequest`` (T103).

    ``images`` defaults to none attached: US1 ships before screening exists
    (US2), so today this is always ``[]`` regardless of what
    ``observation.captures`` lists (this function never inspects that field).
    T134 is the only caller that should ever pass a non-empty ``images``
    list, and only with captures it has already verified as
    ``ScreeningStatus.SCREENED_CLEAN``.

    ``model`` is used only to populate ``DecisionRequest.model`` (which
    routes the call) -- it is never rendered into ``system`` or
    ``observation``, so model identity never becomes part of what the agent
    reads as game information (FR-020).
    """
    return DecisionRequest(
        model=model,
        system=assemble_system_prompt(guidance),
        observation=assemble_observation_text(observation),
        images=list(images) if images else [],
        step_index=step_index,
        response_schema=response_schema,
    )
