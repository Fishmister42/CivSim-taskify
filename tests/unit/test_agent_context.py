"""Unit tests for agent context assembly (T103).

Covers the two hard exclusions the task calls out explicitly: no image is
ever attached unless a caller passes one (the production caller is
``run/decision_loop.py``, which builds its ``images`` through
``select_screened_images`` -- T134/T238), and no harness telemetry (FR-020)
-- model identity, cost, latency, retry counts, save lineage, run
configuration, wall-clock timing, or the game build -- reaches the
assembled ``system``/``observation`` text.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from civsim_harness.agent.context import (
    ROLE_TEXT,
    assemble_context,
    assemble_observation_text,
    assemble_system_prompt,
)
from civsim_harness.models.common import (
    CaptureId,
    CatalogVersionRef,
    DeclarationId,
    GuidanceSetId,
    LuaContext,
    ModelRef,
    ObservationId,
)
from civsim_harness.models.config import GuidanceSet
from civsim_harness.models.turn import DecisionStepId, Observation, ObservationEntry
from civsim_harness.provider.port import Image


def _observation(entries: list[ObservationEntry] | None = None) -> Observation:
    return Observation(
        observation_id=ObservationId("obs-1"),
        decision_step_id=DecisionStepId("step-1"),
        assembled_at=datetime(2026, 9, 20, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="2026.09.1", content_hash="deadbeef"),
        entries=entries if entries is not None else [],
        captures=[],
        screen_identity="WorldScreen",
    )


def _entry(key: str, value: object, declaration_id: str = "map.tiles_visible") -> ObservationEntry:
    return ObservationEntry(
        declaration_id=DeclarationId(declaration_id),
        key=key,
        value=value,
        context=LuaContext.IN_GAME,
    )


def _guidance(content: str = "Prioritise early expansion.") -> GuidanceSet:
    return GuidanceSet(
        guidance_set_id=GuidanceSetId("guidance-1"),
        content_hash="abc123",
        content=content,
        source_ref=None,
    )


# --------------------------------------------------------------------------
# assemble_system_prompt
# --------------------------------------------------------------------------


def test_system_prompt_without_guidance_is_role_text_only() -> None:
    assert assemble_system_prompt(None) == ROLE_TEXT


def test_system_prompt_with_guidance_appends_content() -> None:
    guidance = _guidance("Found your first city on a river.")
    prompt = assemble_system_prompt(guidance)
    assert ROLE_TEXT in prompt
    assert "Found your first city on a river." in prompt


# --------------------------------------------------------------------------
# assemble_observation_text
# --------------------------------------------------------------------------


def test_observation_text_includes_screen_identity_and_entries() -> None:
    observation = _observation([_entry("gold", 42), _entry("science_per_turn", 5.5)])
    text = assemble_observation_text(observation)
    assert "WorldScreen" in text
    assert "gold: 42" in text
    assert "science_per_turn: 5.5" in text


def test_observation_text_handles_no_entries() -> None:
    text = assemble_observation_text(_observation([]))
    assert "(none)" in text


def test_observation_text_preserves_entry_order() -> None:
    entries = [_entry("a", 1), _entry("b", 2), _entry("c", 3)]
    text = assemble_observation_text(_observation(entries))
    assert text.index("a: 1") < text.index("b: 2") < text.index("c: 3")


def test_observation_text_omits_out_of_game_bookkeeping_fields() -> None:
    observation = _observation([_entry("gold", 42)])
    text = assemble_observation_text(observation)
    # observation_id / decision_step_id / catalog_version are harness record
    # identity, not game information -- they must never appear in the text.
    assert "obs-1" not in text
    assert "step-1" not in text
    assert "deadbeef" not in text
    assert "2026.09.1" not in text


# --------------------------------------------------------------------------
# assemble_context -- full DecisionRequest assembly
# --------------------------------------------------------------------------


def _model() -> ModelRef:
    return ModelRef(provider="openrouter", model="anthropic/claude-opus-5")


def test_assemble_context_builds_expected_request_shape() -> None:
    observation = _observation([_entry("gold", 100)])
    guidance = _guidance()
    schema = {"type": "object"}

    request = assemble_context(
        observation=observation,
        guidance=guidance,
        model=_model(),
        step_index=3,
        response_schema=schema,
    )

    assert request.model == _model()
    assert request.step_index == 3
    assert request.response_schema is schema
    assert "gold: 100" in request.observation
    assert ROLE_TEXT in request.system
    assert guidance.content in request.system


def test_assemble_context_attaches_no_image_by_default() -> None:
    request = assemble_context(
        observation=_observation(),
        guidance=None,
        model=_model(),
        step_index=1,
        response_schema={},
    )
    assert request.images == []


def test_assemble_context_attaches_no_image_even_with_captures_listed() -> None:
    """Observation.captures listing a CaptureId must never, by itself, attach an image.

    Images are only attached by a caller passing them explicitly (T134's
    ``select_screened_images`` is the one builder); this module never even
    reads ``Observation.captures``.
    """
    observation = Observation(
        observation_id=ObservationId("obs-2"),
        decision_step_id=DecisionStepId("step-2"),
        assembled_at=datetime(2026, 9, 20, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="2026.09.1", content_hash="deadbeef"),
        entries=[],
        captures=[CaptureId("cap-1")],
        screen_identity="WorldScreen",
    )
    request = assemble_context(
        observation=observation,
        guidance=None,
        model=_model(),
        step_index=1,
        response_schema={},
    )
    assert request.images == []


def test_assemble_context_forwards_explicit_images_from_future_caller() -> None:
    """The extension point works: a caller (the T238 loop wiring) can pass screened images
    through."""
    image = Image(media_type="image/png", data=b"fake-bytes")
    request = assemble_context(
        observation=_observation(),
        guidance=None,
        model=_model(),
        step_index=1,
        response_schema={},
        images=[image],
    )
    assert request.images == [image]


@pytest.mark.parametrize(
    "forbidden",
    [
        "anthropic/claude-opus-5",  # model identity
        "amount_usd",  # cost
        "latency_ms",
        "retry_count",
        "civsim__",  # save name prefix (save lineage)
        "wall_clock",
        "game_build",
        "win/1.0.12.9",  # a plausible game-build composite value
    ],
)
def test_assembled_context_never_contains_harness_telemetry(forbidden: str) -> None:
    """Red-team style check standing in for T126: none of FR-020's excluded fields
    (model identity, cost, latency, retries, save lineage, run configuration,
    wall-clock timing, game build) can appear in the assembled context text.
    """
    observation = _observation([_entry("gold", 100), _entry("turn_yields", {"science": 10})])
    guidance = _guidance("Never mention the model, cost, or save files.")
    request = assemble_context(
        observation=observation,
        guidance=guidance,
        model=_model(),
        step_index=7,
        response_schema={"type": "object"},
    )
    assert forbidden not in request.system
    assert forbidden not in request.observation
