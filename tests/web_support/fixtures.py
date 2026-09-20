"""Seeded fixtures for the unified web interface's three test tiers (T004).

Every tier here runs against the `MatchStore` fake -- this feature never
touches the game client, only the store, so there is no `live` tier and no
test in this feature needs a running harness (tasks.md Notes).

The builders below are plain functions rather than pytest fixtures so they can
be called with arguments from a test that needs a specific shape, and wrapped
as fixtures in each tier's `conftest.py` for the common case.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PANELS_DIR = REPO_ROOT / "panels"

_T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def at(minutes: int = 0) -> datetime:
    """A deterministic timestamp, `minutes` after the fixture epoch."""
    return _T0 + timedelta(minutes=minutes)


def make_configuration(
    config_id: str = "cfg-1",
    *,
    map_seed: str = "SEED-0001",
    civilization: str = "GREECE",
    leader: str = "PERICLES",
    ruleset: str = "BBG",
    model_primary: str = "anthropic/claude-sonnet-4",
) -> Any:
    from civsim_web.store_client.fake import FakeModelConfig, FakeRunConfiguration

    return FakeRunConfiguration(
        config_id=config_id,
        map_seed=map_seed,
        civilization=civilization,
        leader=leader,
        ruleset=ruleset,
        model_config=FakeModelConfig(primary=model_primary),
    )


def make_run(
    run_id: str = "run-1",
    *,
    config_id: str = "cfg-1",
    lifecycle_state: str = "playing",
    record_completeness_status: str = "complete",
    comparability_status: str = "comparable",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    **extra: Any,
) -> Any:
    from civsim_web.store_client.fake import FakeRun

    return FakeRun(
        run_id=run_id,
        config_id=config_id,
        lifecycle_state=lifecycle_state,
        record_completeness_status=record_completeness_status,
        comparability_status=comparability_status,
        started_at=started_at if started_at is not None else at(0),
        ended_at=ended_at,
        **extra,
    )


def make_event(
    event_type: str,
    *,
    run_id: str = "run-1",
    event_id: str | None = None,
    minutes: int = 0,
    turn_number: int | None = None,
    detail: dict[str, Any] | None = None,
) -> Any:
    from civsim_web.store_client.fake import FakeRunEvent

    return FakeRunEvent(
        event_id=event_id or f"evt-{event_type}-{minutes}",
        run_id=run_id,
        event_type=event_type,
        occurred_at=at(minutes),
        turn_number=turn_number,
        detail=detail or {},
    )


def make_save_point(
    turn_number: int,
    *,
    run_id: str = "run-1",
    save_point_id: str | None = None,
    missing: bool = False,
) -> Any:
    from civsim_web.store_client.fake import FakeSavePoint

    return FakeSavePoint(
        save_point_id=save_point_id or f"save-{run_id}-t{turn_number:04d}",
        run_id=run_id,
        turn_number=turn_number,
        save_name=f"civsim__{run_id}__t{turn_number:04d}",
        taken_at=at(turn_number),
        missing=missing,
    )


def make_turn_cycle(
    turn_number: int,
    *,
    run_id: str = "run-1",
    attempt_index: int = 0,
    is_authoritative: bool = True,
    outcome: str = "ended_by_agent",
    step_count: int = 1,
    reasoning: str = "Settled the capital on the river for the housing and the aqueduct.",
    capture_status: str = "screened_clean",
    yields: dict[str, Any] | None = None,
    action_declaration_id: str = "units.found_city",
    observation_declaration_ids: Sequence[str] = ("game.turn_state", "cities.state"),
) -> Any:
    """One turn attempt, with `step_count` fully-populated decision steps.

    `observation_declaration_ids` are *real* declaration ids from 002's
    `catalogs/observations/`, so the Panel Registry can resolve them to a panel.
    A test that wants to exercise data-model.md SS6's drop rule ("an
    `ObservationEntry.declaration_id` that does not resolve to a Panel Registry
    entry is dropped") passes an id nobody registered.
    """
    from civsim_web.store_client.fake import (
        FakeDecision,
        FakeDecisionStep,
        FakeDecisionStepBundle,
        FakeModelCall,
        FakeObservation,
        FakeObservationEntry,
        FakeScreenCapture,
        FakeTurnCycle,
        FakeTurnCycleRecord,
    )

    cycle_id = f"tc-{run_id}-t{turn_number}-a{attempt_index}"
    bundles = []
    for step_index in range(1, step_count + 1):
        step_id = f"{cycle_id}-s{step_index}"
        capture = FakeScreenCapture(
            capture_id=f"cap-{step_id}",
            run_id=run_id,
            turn_number=turn_number,
            decision_step_id=step_id,
            screening_status=capture_status,
            captured_at=at(turn_number),
            blob=b"\x89PNG\r\n\x1a\n" if capture_status == "screened_clean" else None,
        )
        bundles.append(
            FakeDecisionStepBundle(
                step=FakeDecisionStep(
                    decision_step_id=step_id,
                    turn_cycle_id=cycle_id,
                    step_index=step_index,
                    observation_id=f"obs-{step_id}",
                    decision_id=f"dec-{step_id}",
                    model_call_id=f"mc-{step_id}",
                    started_at=at(turn_number),
                    ended_at=at(turn_number),
                ),
                observation=FakeObservation(
                    observation_id=f"obs-{step_id}",
                    decision_step_id=step_id,
                    entries=tuple(
                        FakeObservationEntry(
                            declaration_id=declaration_id,
                            key=declaration_id,
                            value={"turn_number": turn_number, "science_per_turn": 4},
                        )
                        for declaration_id in observation_declaration_ids
                    ),
                    captures=(capture.capture_id,),
                    assembled_at=at(turn_number),
                ),
                decision=FakeDecision(
                    decision_id=f"dec-{step_id}",
                    decision_step_id=step_id,
                    action_declaration_id=action_declaration_id,
                    model_call_id=f"mc-{step_id}",
                    reasoning=reasoning,
                ),
                model_call=FakeModelCall(
                    model_call_id=f"mc-{step_id}",
                    run_id=run_id,
                    turn_cycle_id=cycle_id,
                    decision_step_id=step_id,
                    model_requested="anthropic/claude-sonnet-4",
                    model_served="anthropic/claude-sonnet-4",
                    latency_ms=1200,
                    cost={"input_tokens": 900, "output_tokens": 120},
                ),
            )
        )
        # The capture record travels with the store, not the bundle; callers
        # that need it seed it explicitly via `captures=` on the store.

    return FakeTurnCycleRecord(
        turn_cycle=FakeTurnCycle(
            turn_cycle_id=cycle_id,
            run_id=run_id,
            turn_number=turn_number,
            save_point_id=f"save-{run_id}-t{turn_number:04d}",
            attempt_index=attempt_index,
            is_authoritative=is_authoritative,
            step_count=step_count,
            outcome=outcome,
            yields=yields or {"science_output": 4 + turn_number, "culture_output": 2 + turn_number},
            started_at=at(turn_number),
            ended_at=at(turn_number),
            persisted_at=at(turn_number),
        ),
        steps=tuple(bundles),
    )


def make_store(
    *,
    turns: int = 3,
    run_id: str = "run-1",
    lifecycle_state: str = "playing",
    events: Sequence[Any] = (),
    healthy: bool = True,
    capture_status: str = "screened_clean",
    withheld_reason: str | None = None,
    with_captures: bool = True,
    with_configuration: bool = True,
    step_count: int = 1,
    reasoning: str | None = None,
    turn_gaps: dict[str, list[int]] | None = None,
    extra_runs: Sequence[Any] = (),
) -> Any:
    """A fake store holding one run with `turns` recorded turns.

    `turns=0` produces the "opened before the first turn was recorded" empty
    state the spec's Edge Cases call out. `capture_status` drives the
    `CaptureView` fail-closed matrix (including a deliberately unrecognised
    value); `with_captures=False` seeds the turn records without any capture
    record at all, which is the `missing_record` case.
    """
    from civsim_web.store_client.fake import FakeMatchStore

    configuration = make_configuration()
    run = make_run(run_id, config_id=configuration.config_id, lifecycle_state=lifecycle_state)
    kwargs: dict[str, Any] = {"step_count": step_count, "capture_status": capture_status}
    if reasoning is not None:
        kwargs["reasoning"] = reasoning
    records = [make_turn_cycle(n, run_id=run_id, **kwargs) for n in range(1, turns + 1)]
    captures = (
        [
            _capture_for(record, position, capture_status, withheld_reason)
            for record in records
            for position in range(len(record.steps))
        ]
        if with_captures
        else []
    )
    return FakeMatchStore(
        runs=[run, *extra_runs],
        configurations=[configuration] if with_configuration else [],
        turn_cycles=records,
        events=list(events),
        save_points=[make_save_point(n, run_id=run_id) for n in range(1, turns + 1)],
        captures=captures,
        healthy=healthy,
        turn_gaps=turn_gaps,
    )


def _capture_for(
    record: Any,
    step_position: int,
    screening_status: str = "screened_clean",
    withheld_reason: str | None = None,
) -> Any:
    from civsim_web.store_client.fake import FakeScreenCapture

    bundle = record.steps[step_position]
    return FakeScreenCapture(
        capture_id=f"cap-{bundle.step.decision_step_id}",
        run_id=record.turn_cycle.run_id,
        turn_number=record.turn_cycle.turn_number,
        decision_step_id=bundle.step.decision_step_id,
        screening_status=screening_status,
        withheld_reason=withheld_reason,
        captured_at=bundle.step.started_at,
        blob=b"\x89PNG\r\n\x1a\n" if screening_status == "screened_clean" else None,
    )


def make_app(store: Any = None, *, panels_dir: Path | None = None) -> Any:
    """The FastAPI app, bound to loopback so tests never touch a real interface."""
    from civsim_web.app import create_app

    return create_app(
        store=store if store is not None else make_store(),
        bind_addresses=["127.0.0.1"],
        panels_dir=panels_dir or PANELS_DIR,
    )


def make_client(store: Any = None, *, panels_dir: Path | None = None) -> Any:
    """A `TestClient` over `make_app`. Closed by the caller or by a fixture."""
    from fastapi.testclient import TestClient

    return TestClient(make_app(store, panels_dir=panels_dir))
