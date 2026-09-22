"""Unit tests for FR-020 telemetry exclusion (T126).

Builds a realistic decision step: a parity-filtered ``Observation`` (legitimate
game state only) plus a full set of harness telemetry records a run actually
carries -- ``ModelCall``, ``SavePoint``, ``RunConfiguration``, and ``Run``
itself -- each seeded with a distinctive, made-up value (a model name no real
provider uses, an unlikely cost figure, a specific save name, ...). The
context actually assembled for the agent (``civsim_harness.agent.context``'s
real ``assemble_system_prompt``/``assemble_observation_text``/``assemble_context``,
not a stand-in) is then checked for every one of those distinctive values,
using :mod:`civsim_harness.parity.forbidden`'s own literal-leak checker --
the same function the runtime guard (T128) uses -- so this test exercises the
guard's real code, not a reimplementation of it.

A negative control (``test_the_leak_checker_actually_detects_a_planted_leak``)
proves the methodology itself is capable of failing: it plants one of the
telemetry values directly into rendered text and asserts the checker catches
it, so a vacuously-passing checker cannot hide behind these tests.

``agent/context.py`` (T103) was importable and complete at the time this test
was written -- these tests exercise it directly rather than a representative
stand-in.
"""

from __future__ import annotations

import io

import pytest

from civsim_harness.agent.context import assemble_context
from civsim_harness.errors import ParityViolation
from civsim_harness.models.common import (
    CatalogVersionRef,
    Cost,
    DecisionStepId,
    LuaContext,
    ModelRef,
    ObservationId,
)
from civsim_harness.models.config import (
    GuidanceSet,
    ModelConfig,
    RunConfiguration,
    TurnReachedStopCondition,
)
from civsim_harness.models.records import CallOutcome, ModelCall, RetentionStatus, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
)
from civsim_harness.models.turn import Observation, ObservationEntry
from civsim_harness.parity.forbidden import (
    assert_no_literal_leaks,
    enforce_parity_boundary,
    find_literal_leaks,
    scan_observation_entries,
)

# --------------------------------------------------------------------------
# Fixtures: legitimate game-state observation + a full telemetry bundle,
# each telemetry field seeded with a distinctive, invented value.
# --------------------------------------------------------------------------

_DISTINCTIVE_MODEL_NAME = "anthropic/telemetry-marker-model-x9f2"
_DISTINCTIVE_PROVIDER = "openrouter-marker-9f2"
_DISTINCTIVE_SAVE_NAME = "civsim__runmarker9f2__t0042"
_DISTINCTIVE_CONFIG_ID = "cfg_marker_a1b2c3d4"
_DISTINCTIVE_GAME_BUILD = "win/9.9.99.marker9f2"
_DISTINCTIVE_TIMESTAMP = "2031-07-04T13:37:00+00:00"
_DISTINCTIVE_AMOUNT_USD = "483.17429"
_DISTINCTIVE_LATENCY = "918273"
_DISTINCTIVE_RETRY_COUNT = "743"
_DISTINCTIVE_RUN_ID = "run_telemetry_marker_9f2"


def _observation() -> Observation:
    """A plausible, parity-filtered decision-step observation -- game state only."""
    entries = [
        ObservationEntry(
            declaration_id="map.state",  # type: ignore[arg-type]
            key="map.state",
            value={
                "width": 44,
                "height": 44,
                "revealed_plots": [
                    {
                        "x": 12,
                        "y": 7,
                        "terrain": "TERRAIN_GRASS",
                        "feature": None,
                        "resource": None,
                        "improvement": None,
                        "is_currently_visible": True,
                        "owner_player_id": 0,
                    }
                ],
            },
            context=LuaContext.GAME_CORE_TUNER,
        ),
        ObservationEntry(
            declaration_id="units.state",  # type: ignore[arg-type]
            key="units.state",
            value={
                "units": [
                    {
                        "unit_id": 101,
                        "unit_type": "UNIT_WARRIOR",
                        "owner_player_id": 0,
                        "owner_is_local_player": True,
                        "plot": {"x": 12, "y": 7},
                        "movement_remaining": 2.0,
                        "max_movement": 2.0,
                    }
                ]
            },
            context=LuaContext.GAME_CORE_TUNER,
        ),
    ]
    return Observation(
        observation_id=ObservationId("obs_1"),
        decision_step_id=DecisionStepId("step_1"),
        assembled_at="2026-09-20T00:00:00Z",  # type: ignore[arg-type]
        catalog_version=CatalogVersionRef(version="2026.09.1", content_hash="deadbeef"),
        entries=entries,
        captures=[],
        screen_identity="world",
    )


def _model_call() -> ModelCall:
    return ModelCall(
        model_call_id="call_1",  # type: ignore[arg-type]
        run_id=_DISTINCTIVE_RUN_ID,  # type: ignore[arg-type]
        turn_cycle_id="turn_1",  # type: ignore[arg-type]
        decision_step_id="step_1",  # type: ignore[arg-type]
        model_requested=ModelRef(provider=_DISTINCTIVE_PROVIDER, model=_DISTINCTIVE_MODEL_NAME),
        model_served=ModelRef(provider=_DISTINCTIVE_PROVIDER, model=_DISTINCTIVE_MODEL_NAME),
        latency_ms=int(_DISTINCTIVE_LATENCY),
        cost=Cost(
            input_tokens=12345,
            output_tokens=678,
            total_tokens=13023,
            amount_usd=float(_DISTINCTIVE_AMOUNT_USD),
        ),
        retry_count=int(_DISTINCTIVE_RETRY_COUNT),
        fallback_occurred=True,
        image_count=0,
        outcome=CallOutcome.DECISION_RETURNED,
    )


def _save_point() -> SavePoint:
    return SavePoint(
        save_point_id="save_1",  # type: ignore[arg-type]
        run_id=_DISTINCTIVE_RUN_ID,  # type: ignore[arg-type]
        turn_number=42,
        save_name=_DISTINCTIVE_SAVE_NAME,
        taken_at=_DISTINCTIVE_TIMESTAMP,  # type: ignore[arg-type]
        verified=True,
        lineage={"parent_run_id": "run_parent_marker_9f2", "parent_turn": 17},
        retention_status=RetentionStatus.RETAINED,
    )


def _run_configuration() -> RunConfiguration:
    return RunConfiguration(
        config_id=_DISTINCTIVE_CONFIG_ID,  # type: ignore[arg-type]
        map_seed="seed-marker-9f2",
        civilization="CIVILIZATION_ROME",
        leader="LEADER_TRAJAN",
        ruleset="RULESET_STANDARD",
        difficulty="DIFFICULTY_PRINCE",
        stop_condition=TurnReachedStopCondition(turn=200),
        model_config=ModelConfig(
            primary=ModelRef(provider=_DISTINCTIVE_PROVIDER, model=_DISTINCTIVE_MODEL_NAME)
        ),
        no_progress_step_limit=25,
        recovery_attempt_limit=5,
        min_free_disk_gb=12.5,
        created_at=_DISTINCTIVE_TIMESTAMP,  # type: ignore[arg-type]
    )


def _run() -> Run:
    return Run(
        run_id=_DISTINCTIVE_RUN_ID,  # type: ignore[arg-type]
        config_id=_DISTINCTIVE_CONFIG_ID,  # type: ignore[arg-type]
        lifecycle_state=LifecycleState.PLAYING,
        started_at=_DISTINCTIVE_TIMESTAMP,  # type: ignore[arg-type]
        record_completeness_status=RecordCompletenessStatus.COMPLETE,
        comparability_status=ComparabilityStatus.COMPARABLE,
        observation_catalog_version=CatalogVersionRef(version="2026.09.1", content_hash="obs-hash"),
        action_catalog_version=CatalogVersionRef(version="2026.09.1", content_hash="act-hash"),
        client_identity={"pid": 55555},
        game_build=_DISTINCTIVE_GAME_BUILD,
        host_platform={"os": "windows"},
        host_support_tier=HostSupportTier.VALIDATED,
        capture_path="none",  # type: ignore[arg-type]
    )


def _telemetry_literal_values() -> list[str]:
    """The concrete, distinctive values FR-020 says must never reach the agent."""
    return [
        _DISTINCTIVE_MODEL_NAME,
        _DISTINCTIVE_PROVIDER,
        _DISTINCTIVE_SAVE_NAME,
        _DISTINCTIVE_CONFIG_ID,
        _DISTINCTIVE_GAME_BUILD,
        _DISTINCTIVE_TIMESTAMP,
        _DISTINCTIVE_AMOUNT_USD,
        _DISTINCTIVE_LATENCY,
        _DISTINCTIVE_RETRY_COUNT,
        _DISTINCTIVE_RUN_ID,
    ]


def _guidance() -> GuidanceSet:
    return GuidanceSet(
        guidance_set_id="guidance_1",  # type: ignore[arg-type]
        content_hash="guidance-hash",
        content="Prioritize early expansion and a strong early religion.",
    )


# --------------------------------------------------------------------------
# The real assembled context must not contain any telemetry value
# --------------------------------------------------------------------------


def test_assembled_context_excludes_every_telemetry_value() -> None:
    # Sanity: these fixtures exist and validate (proves the telemetry bundle
    # this test plants really is the shape FR-020 names).
    _model_call()
    _save_point()
    _run_configuration()
    _run()

    request = assemble_context(
        observation=_observation(),
        guidance=_guidance(),
        model=ModelRef(provider=_DISTINCTIVE_PROVIDER, model=_DISTINCTIVE_MODEL_NAME),
        step_index=1,
        response_schema={"type": "object"},
    )

    rendered = f"{request.system}\n{request.observation}"
    hits = find_literal_leaks(rendered, _telemetry_literal_values())

    assert hits == [], f"telemetry value(s) leaked into the assembled context: {hits}"
    # Also exercise the exact function the runtime guard (T128) calls.
    assert_no_literal_leaks(rendered, _telemetry_literal_values(), context_label="decision request")
    enforce_parity_boundary(
        observation=_observation(),
        decision_request=request,
        extra_forbidden_values=_telemetry_literal_values(),
    )


def test_the_leak_checker_actually_detects_a_planted_leak() -> None:
    """Negative control: prove find_literal_leaks is not vacuously passing above."""
    planted = f"Model served: {_DISTINCTIVE_MODEL_NAME}, cost ${_DISTINCTIVE_AMOUNT_USD}"

    hits = find_literal_leaks(planted, _telemetry_literal_values())

    assert _DISTINCTIVE_MODEL_NAME in hits
    assert _DISTINCTIVE_AMOUNT_USD in hits
    with pytest.raises(ParityViolation):
        assert_no_literal_leaks(planted, _telemetry_literal_values(), context_label="planted text")


def test_enforce_parity_boundary_raises_when_decision_request_leaks_telemetry() -> None:
    request = assemble_context(
        observation=_observation(),
        guidance=_guidance(),
        model=ModelRef(provider=_DISTINCTIVE_PROVIDER, model=_DISTINCTIVE_MODEL_NAME),
        step_index=1,
        response_schema={"type": "object"},
    )
    # Simulate a leak a future regression might introduce: the model name
    # ends up appended to the rendered observation text.
    from dataclasses import replace

    leaky_request = replace(
        request, observation=request.observation + f"\nServed by {_DISTINCTIVE_MODEL_NAME}"
    )

    with pytest.raises(ParityViolation):
        enforce_parity_boundary(
            observation=_observation(),
            decision_request=leaky_request,
            extra_forbidden_values=_telemetry_literal_values(),
        )


# --------------------------------------------------------------------------
# Structural (key-name) scan: telemetry-shaped keys must never appear on an
# ObservationEntry, either at the top or nested inside its value.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leaked_key",
    [
        "model_served",
        "model_requested",
        "latency_ms",
        "retry_count",
        "fallback_occurred",
        "save_lineage",
        "run_configuration",
        "wall_clock",
        "game_build",
        # T280: `Run.debug_menu_state` is provenance of the same kind as `game_build` above, and
        # must be caught by the same key-name net rather than relying on nothing ever putting it
        # in an observation.
        "debug_menu_state",
    ],
)
def test_structural_scan_flags_a_telemetry_shaped_key_nested_in_observation_value(
    leaked_key: str,
) -> None:
    leaky_observation = Observation(
        observation_id=ObservationId("obs_2"),
        decision_step_id=DecisionStepId("step_2"),
        assembled_at="2026-09-20T00:00:00Z",  # type: ignore[arg-type]
        catalog_version=CatalogVersionRef(version="2026.09.1", content_hash="deadbeef"),
        entries=[
            ObservationEntry(
                declaration_id="units.state",  # type: ignore[arg-type]
                key="units.state",
                value={"units": [{"unit_id": 1, leaked_key: "should never be here"}]},
                context=LuaContext.GAME_CORE_TUNER,
            )
        ],
        captures=[],
        screen_identity="world",
    )

    violations = scan_observation_entries(leaky_observation)

    assert violations, f"expected a structural finding for leaked key {leaked_key!r}"
    with pytest.raises(ParityViolation):
        enforce_parity_boundary(observation=leaky_observation)


def test_structural_scan_is_clean_for_a_legitimate_observation() -> None:
    violations = scan_observation_entries(_observation())

    assert violations == []
    enforce_parity_boundary(observation=_observation())  # must not raise


def test_running_any_cli_command_wires_the_redacting_log_handler() -> None:
    """T228, FR-020: the redaction filter reaches the loggers that actually emit.

    ``telemetry/logging.py`` is written so that redaction cannot be forgotten -- both of its
    handler-wiring functions force the formatter *and* the filter on regardless of what the
    caller asked for. That guarantee was inert: nothing in ``src/`` called either function, so
    ``civsim_harness.nexus.client`` and ``civsim_harness.nexus.sentinels`` (which do log)
    propagated to whatever root handler the host process had, un-redacted.

    Asserted end to end through a real CLI invocation and a real child logger, because the claim
    is about what a ``civsim`` process does -- not about what ``configure_logging`` does when
    called directly, which was already true and already tested.
    """
    import logging

    from typer.testing import CliRunner

    from civsim_harness.operator import cli
    from civsim_harness.telemetry.logging import (
        HARNESS_LOGGER_NAME,
        JsonRedactingFormatter,
        RedactingFilter,
    )

    package_logger = logging.getLogger(HARNESS_LOGGER_NAME)
    saved_handlers = list(package_logger.handlers)
    saved_propagate = package_logger.propagate
    try:
        for handler in saved_handlers:
            package_logger.removeHandler(handler)

        result = CliRunner().invoke(cli.app, ["version"])
        assert result.exit_code == 0

        assert len(package_logger.handlers) == 1
        handler = package_logger.handlers[0]
        assert isinstance(handler.formatter, JsonRedactingFormatter)
        assert any(isinstance(f, RedactingFilter) for f in handler.filters)

        # A child logger -- the shape `nexus/client.py` and `nexus/sentinels.py` use -- reaches
        # that handler by propagation, and a credential-shaped value in its message does not
        # survive the trip.
        # Assigned rather than `setStream`, which flushes the outgoing stream first -- and the
        # outgoing one here is `CliRunner`'s stderr capture, already closed when `invoke`
        # returned. What is under test is the handler's wiring, not which file it points at.
        stream = io.StringIO()
        handler.stream = stream  # type: ignore[attr-defined]
        logging.getLogger(f"{HARNESS_LOGGER_NAME}.nexus.client").info(
            "connecting with api_key=sk-or-v1-000111222333444555666777888999aaa"
        )
        emitted = stream.getvalue()
        assert emitted, "the child logger's record never reached the configured handler"
        assert "sk-or-v1-000111222333444555666777888999aaa" not in emitted
    finally:
        for handler in list(package_logger.handlers):
            package_logger.removeHandler(handler)
        for handler in saved_handlers:
            package_logger.addHandler(handler)
        package_logger.propagate = saved_propagate
