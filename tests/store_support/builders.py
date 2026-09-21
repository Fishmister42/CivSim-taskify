"""Record builders for the 003 store suites -- richer than 002's, same shape.

Beyond what ``tests/contract/test_match_store_port.py`` builds, these can attach ``yields`` to a
turn, ``cities.state`` / ``units.state`` entries to an observation, an action id and parameters to
a decision, a priced ``cost`` to a model call, and kept / withheld captures -- everything the
trend, divergence, model-call and bundle reads need to be exercised end to end.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.decision import Decision
from civsim_harness.models.records import ModelCall, RunEvent, SavePoint
from civsim_harness.models.run import Run
from civsim_harness.models.turn import DecisionStep, Observation, ScreenCapture, TurnCycle
from civsim_harness.store.port import DecisionStepBundle, TurnCycleRecord

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def at(minutes: int = 0) -> datetime:
    return NOW + timedelta(minutes=minutes)


def catalog_ref() -> dict[str, str]:
    return {"version": "2026.09.1", "content_hash": "abc123"}


def make_config(
    config_id: str, *, seed_set_id: str | None = None, map_seed: str = "123"
) -> RunConfiguration:
    return RunConfiguration.model_validate(
        {
            "config_id": config_id,
            "seed_set_id": seed_set_id,
            "map_seed": map_seed,
            "civilization": "CIVILIZATION_PERSIA",
            "leader": "LEADER_CYRUS",
            "ruleset": "RULESET_EXPANSION_2",
            "difficulty": "DIFFICULTY_EMPEROR",
            "stop_condition": {"type": "turn_reached", "turn": 50},
            "model_config": {"primary": {"provider": "openrouter", "model": "x"}},
            "no_progress_step_limit": 8,
            "recovery_attempt_limit": 3,
            "min_free_disk_gb": 25,
            "created_at": NOW,
        }
    )


def make_run(
    run_id: str,
    config_id: str = "cfg1",
    *,
    lifecycle_state: str = "playing",
    stop_resolution: str | None = None,
    parent_run_id: str | None = None,
    parent_turn: int | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    record_completeness_status: str = "unknown",
    comparability_status: str = "comparable",
    archived_at: datetime | None = None,
) -> Run:
    return Run.model_validate(
        {
            "run_id": run_id,
            "config_id": config_id,
            "lifecycle_state": lifecycle_state,
            "started_at": started_at,
            "ended_at": ended_at,
            "stop_resolution": stop_resolution,
            "record_completeness_status": record_completeness_status,
            "comparability_status": comparability_status,
            "observation_catalog_version": catalog_ref(),
            "action_catalog_version": catalog_ref(),
            "parent_run_id": parent_run_id,
            "parent_turn": parent_turn,
            "game_build": "linux/1.0.12.9",
            "host_support_tier": "supported",
            "capture_path": "xcomposite",
            "archived_at": archived_at,
        }
    )


def make_turn_cycle(
    turn_cycle_id: str,
    run_id: str,
    turn_number: int,
    attempt_index: int,
    *,
    is_authoritative: bool,
    step_count: int,
    save_point_id: str,
    outcome: str = "ended_by_agent",
    yields: dict[str, Any] | None = None,
) -> TurnCycle:
    return TurnCycle.model_validate(
        {
            "turn_cycle_id": turn_cycle_id,
            "run_id": run_id,
            "turn_number": turn_number,
            "attempt_index": attempt_index,
            "is_authoritative": is_authoritative,
            "save_point_id": save_point_id,
            "step_count": step_count,
            "outcome": outcome,
            "final_no_progress_streak": 0,
            "visually_degraded": False,
            "yields": yields or {},
            "started_at": at(turn_number),
            "ended_at": at(turn_number),
            "persisted_at": at(turn_number),
        }
    )


def observation_entries(
    *, cities: int | None = None, units: int | None = None
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if cities is not None:
        entries.append(
            {
                "declaration_id": "cities.state",
                "key": "cities",
                "value": {"cities": [{"id": i} for i in range(cities)]},
                "context": "GameCore_Tuner",
            }
        )
    if units is not None:
        entries.append(
            {
                "declaration_id": "units.state",
                "key": "units",
                "value": {"units": [{"id": i} for i in range(units)]},
                "context": "InGame",
            }
        )
    return entries


def make_step_bundle(
    run_id: str,
    turn_cycle_id: str,
    step_index: int,
    *,
    action: str = "units.move_to",
    parameters: dict[str, Any] | None = None,
    entries: Sequence[dict[str, Any]] = (),
    captures: Sequence[str] = (),
    cost_usd: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    fallback: bool = False,
    retry_count: int = 0,
) -> DecisionStepBundle:
    step_id = f"{turn_cycle_id}-step{step_index}"
    obs_id = f"{step_id}-obs"
    dec_id = f"{step_id}-dec"
    call_id = f"{step_id}-call"
    step = DecisionStep.model_validate(
        {
            "decision_step_id": step_id,
            "turn_cycle_id": turn_cycle_id,
            "step_index": step_index,
            "observation_id": obs_id,
            "decision_id": dec_id,
            "model_call_id": call_id,
            "progress": "changed_state",
            "no_progress_streak_after": 0,
            "visually_degraded": False,
            "started_at": NOW,
            "ended_at": NOW,
        }
    )
    observation = Observation.model_validate(
        {
            "observation_id": obs_id,
            "decision_step_id": step_id,
            "assembled_at": NOW,
            "catalog_version": catalog_ref(),
            "entries": list(entries),
            "captures": list(captures),
            "screen_identity": "world",
        }
    )
    decision = Decision.model_validate(
        {
            "decision_id": dec_id,
            "decision_step_id": step_id,
            "action_declaration_id": action,
            "parameters": parameters or {},
            "reasoning": "test reasoning",
            "trigger": "proactive",
            "model_call_id": call_id,
            "execution": {"outcome": "applied", "verified_at": NOW},
        }
    )
    cost: dict[str, Any] = {}
    if cost_usd is not None:
        cost["amount_usd"] = cost_usd
    if input_tokens is not None:
        cost["input_tokens"] = input_tokens
    if output_tokens is not None:
        cost["output_tokens"] = output_tokens
    if input_tokens is not None and output_tokens is not None:
        cost["total_tokens"] = input_tokens + output_tokens
    model_call = ModelCall.model_validate(
        {
            "model_call_id": call_id,
            "run_id": run_id,
            "turn_cycle_id": turn_cycle_id,
            "decision_step_id": step_id,
            "model_requested": {"provider": "openrouter", "model": "x"},
            "model_served": {"provider": "openrouter", "model": "y" if fallback else "x"},
            "latency_ms": 100,
            "cost": cost,
            "retry_count": retry_count,
            "fallback_occurred": fallback,
            "image_count": 0,
            "outcome": "decision_returned",
        }
    )
    return DecisionStepBundle(
        step=step, observation=observation, decision=decision, model_call=model_call
    )


def make_turn_cycle_record(
    run_id: str,
    turn_number: int,
    attempt_index: int = 0,
    *,
    num_steps: int = 1,
    step_indices: list[int] | None = None,
    is_authoritative: bool = True,
    outcome: str = "ended_by_agent",
    yields: dict[str, Any] | None = None,
    cities: int | None = None,
    units: int | None = None,
    actions: Sequence[tuple[str, dict[str, Any]]] | None = None,
    cost_usd: float | None = None,
    captures_by_step: dict[int, Sequence[str]] | None = None,
) -> TurnCycleRecord:
    turn_cycle_id = f"{run_id}-t{turn_number}-a{attempt_index}"
    indices = step_indices if step_indices is not None else list(range(1, num_steps + 1))
    bundles = []
    for position, index in enumerate(indices):
        action, parameters = ("units.move_to", {})
        if actions is not None and position < len(actions):
            action, parameters = actions[position]
        bundles.append(
            make_step_bundle(
                run_id,
                turn_cycle_id,
                index,
                action=action,
                parameters=parameters,
                entries=observation_entries(cities=cities, units=units),
                captures=(captures_by_step or {}).get(index, ()),
                cost_usd=cost_usd,
            )
        )
    tc = make_turn_cycle(
        turn_cycle_id,
        run_id,
        turn_number,
        attempt_index,
        is_authoritative=is_authoritative,
        step_count=len(bundles),
        save_point_id=f"{run_id}-sp{turn_number}-{attempt_index}",
        outcome=outcome,
        yields=yields,
    )
    return TurnCycleRecord(turn_cycle=tc, steps=bundles)


def make_save_point(
    save_point_id: str,
    run_id: str,
    turn_number: int,
    *,
    retention_status: str = "retained",
    missing: bool = False,
    taken_at: datetime | None = None,
) -> SavePoint:
    return SavePoint.model_validate(
        {
            "save_point_id": save_point_id,
            "run_id": run_id,
            "turn_number": turn_number,
            "save_name": f"civsim__{run_id}__t{turn_number:04d}",
            "taken_at": taken_at or at(turn_number),
            "verified": True,
            "retention_status": retention_status,
            "missing": missing,
        }
    )


def make_event(
    event_id: str, run_id: str, event_type: str, *, turn_number: int | None = None, minutes: int = 0
) -> RunEvent:
    return RunEvent.model_validate(
        {
            "event_id": event_id,
            "run_id": run_id,
            "turn_number": turn_number,
            "event_type": event_type,
            "occurred_at": at(minutes),
            "detail": {},
        }
    )


def make_capture(
    capture_id: str,
    run_id: str,
    turn_number: int,
    decision_step_id: str,
    *,
    blob: bytes | None,
    withheld_reason: str | None = None,
) -> tuple[ScreenCapture, bytes | None]:
    """A kept capture (``blob`` given) or a withheld one (``blob=None`` + reason)."""
    kept = blob is not None
    capture = ScreenCapture.model_validate(
        {
            "capture_id": capture_id,
            "run_id": run_id,
            "turn_number": turn_number,
            "decision_step_id": decision_step_id,
            "captured_at": at(turn_number),
            "camera_state": {"zoom": 1.0},
            "view_declaration_id": "views.world",
            "screening_status": "screened_clean" if kept else "withheld",
            "withheld_reason": None if kept else (withheld_reason or "non_player_ui"),
            "shown_to_agent": kept,
            "retained_as_evidence": kept,
            "blob_ref": hashlib.sha256(blob).hexdigest() if blob is not None else None,
            "capture_path": "xcomposite",
        }
    )
    return capture, blob


def make_failed_call(run_id: str, turn_cycle_id: str, step_id: str, call_id: str) -> ModelCall:
    """A call that produced no decision -- recorded through ``write_model_call`` alone."""
    return ModelCall.model_validate(
        {
            "model_call_id": call_id,
            "run_id": run_id,
            "turn_cycle_id": turn_cycle_id,
            "decision_step_id": step_id,
            "model_requested": {"provider": "openrouter", "model": "x"},
            "model_served": {"provider": "openrouter", "model": "x"},
            "latency_ms": 50,
            "cost": {"amount_usd": 0.001},
            "retry_count": 1,
            "fallback_occurred": False,
            "image_count": 0,
            "outcome": "failed",
        }
    )


def record_run(
    store: Any,
    run_id: str,
    *,
    turns: int,
    config_id: str | None = None,
    seed_set_id: str | None = None,
    lifecycle_state: str = "playing",
    stop_resolution: str | None = None,
    started_minutes: int = 0,
    yields_for: Any = None,
    cities: int | None = None,
    units: int | None = None,
    actions_for: Any = None,
    num_steps: int = 1,
    cost_usd: float | None = None,
    comparability_status: str = "comparable",
    skip_turns: Sequence[int] = (),
    save_point_only_turns: Sequence[int] = (),
) -> Run:
    """Create a run and write ``turns`` turns (each with its quicksave first, per FR-007).

    ``yields_for(turn) -> dict`` and ``actions_for(turn) -> [(action, params), …]`` shape each
    turn; ``skip_turns`` are attempted (quicksave written) but never recorded -- a gap once the
    run stops; ``save_point_only_turns`` likewise but always trailing.
    """
    cfg_id = config_id or f"{run_id}-cfg"
    run = make_run(
        run_id,
        cfg_id,
        lifecycle_state="playing",
        started_at=at(started_minutes),
        comparability_status=comparability_status,
    )
    store.create_run(run, make_config(cfg_id, seed_set_id=seed_set_id))
    for turn in range(1, turns + 1):
        store.write_save_point(make_save_point(f"{run_id}-sp{turn}-0", run_id, turn))
        if turn in skip_turns or turn in save_point_only_turns:
            continue
        store.write_turn_cycle(
            make_turn_cycle_record(
                run_id,
                turn,
                num_steps=num_steps,
                yields=yields_for(turn) if yields_for else None,
                cities=cities,
                units=units,
                actions=actions_for(turn) if actions_for else None,
                cost_usd=cost_usd,
            )
        )
    if lifecycle_state != "playing":
        fields: dict[str, Any] = {"lifecycle_state": lifecycle_state}
        if lifecycle_state in {"finished", "failed"}:
            fields["stop_resolution"] = stop_resolution or "turn_reached"
            fields["ended_at"] = at(started_minutes + turns)
        store.update_run(run_id, **fields)
    found = store.get_run(run_id)
    assert found is not None
    return found
