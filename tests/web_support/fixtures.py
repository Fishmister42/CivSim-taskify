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
    replayed_turns: Sequence[int] = (),
    gap_turns: Sequence[int] = (),
    withheld_capture_turns: Sequence[int] = (),
    attempt_reader: bool = True,
) -> Any:
    """A fake store holding one run with `turns` recorded turns.

    `turns=0` produces the "opened before the first turn was recorded" empty
    state the spec's Edge Cases call out. `capture_status` drives the
    `CaptureView` fail-closed matrix (including a deliberately unrecognised
    value); `with_captures=False` seeds the turn records without any capture
    record at all, which is the `missing_record` case.

    `replayed_turns` names turns that were attempted, abandoned, and replayed --
    attempt 0 abandoned, attempt 1 authoritative. That is FR-009's subject: a
    reference naming attempt 0 must still return attempt 0. `attempt_reader=False`
    drops the fake's optional `TurnAttemptReader` capability so the same
    fixtures exercise the published-port fallback, which can address only the
    authoritative and the newest attempt (`store_client/port.py`).

    `gap_turns` (T046) names turns whose record is **genuinely absent**: no
    attempt of them is written at all, so the fake's own `turn_gaps()` computes
    the gap rather than being told about it. That is the difference from the
    `turn_gaps=` override, which asserts a gap the records do not show -- both
    are real cases (a trailing turn is only knowable from the override), but a
    replay test needs the turn to actually not be there. The turn's quicksave is
    still seeded: Principle IV takes one at the *start* of every turn, so a turn
    whose record was lost still has its save, and that is what makes it a gap
    inside the range rather than the end of the run.

    `withheld_capture_turns` withholds the captures of named turns only, leaving
    the rest `screened_clean` -- the mixed case a replay walks through, as
    opposed to `capture_status=` which sets every capture at once.
    """
    from civsim_web.store_client.fake import FakeMatchStore

    configuration = make_configuration()
    run = make_run(run_id, config_id=configuration.config_id, lifecycle_state=lifecycle_state)
    kwargs: dict[str, Any] = {"step_count": step_count, "capture_status": capture_status}
    if reasoning is not None:
        kwargs["reasoning"] = reasoning

    replayed = set(replayed_turns)
    gapped = set(gap_turns)
    withheld_turns = set(withheld_capture_turns)
    records = []
    for n in range(1, turns + 1):
        if n in gapped:
            # No attempt at all: the fake computes this turn as a gap.
            continue
        if n in replayed:
            records.append(
                make_turn_cycle(
                    n,
                    run_id=run_id,
                    attempt_index=0,
                    is_authoritative=False,
                    outcome="abandoned",
                    **kwargs,
                )
            )
            records.append(
                make_turn_cycle(n, run_id=run_id, attempt_index=1, **kwargs)
            )
        else:
            records.append(make_turn_cycle(n, run_id=run_id, **kwargs))

    captures = (
        [
            _capture_for(
                record,
                position,
                "withheld"
                if record.turn_cycle.turn_number in withheld_turns
                else capture_status,
                withheld_reason,
            )
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
        attempt_reader=attempt_reader,
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


def make_catalog_store(
    *,
    count: int = 5,
    turns: int = 8,
    seeds: Sequence[str] | None = None,
    civilizations: Sequence[str] | None = None,
    leaders: Sequence[str] | None = None,
    rulesets: Sequence[str] | None = None,
    models: Sequence[str] | None = None,
    lifecycle_states: Sequence[str] | None = None,
    science_base: Sequence[float] | None = None,
    science_step: Sequence[float] | None = None,
    culture_base: Sequence[float] | None = None,
    completeness: dict[str, str] | None = None,
    turn_gaps: dict[str, list[int]] | None = None,
    capture_status: str = "screened_clean",
    with_captures: bool = True,
    with_configuration: bool = True,
) -> Any:
    """A multi-run store for the catalog (T058) and comparison (T057) tests.

    Runs are `run-01` .. `run-NN` so string sort and numeric sort agree, which
    keeps a paging assertion about "the first five" unambiguous.

    The per-run metric shape is the point of most of these parameters: the
    comparison view's divergence detection is only testable against series whose
    leader is known in advance, so `science_base` and `science_step` let a test
    say "run-02 starts behind and overtakes at turn 5" rather than seeding noise
    and asserting whatever comes out.

    `completeness` and `turn_gaps` drive the Principle III quarantine: a run
    marked `has_gaps`, and -- separately -- a run the store calls `complete`
    while still listing gapped turns, must both be excluded from every trend
    line.
    """
    from civsim_web.store_client.fake import FakeMatchStore

    def pick(values: Sequence[Any] | None, default: Sequence[Any], index: int) -> Any:
        source = values if values else default
        return source[index % len(source)]

    completeness = completeness or {}
    turn_gaps = turn_gaps or {}

    runs: list[Any] = []
    configurations: list[Any] = []
    records: list[Any] = []
    save_points: list[Any] = []
    captures: list[Any] = []

    for index in range(count):
        run_id = f"run-{index + 1:02d}"
        config_id = f"cfg-{index + 1:02d}"
        configurations.append(
            make_configuration(
                config_id,
                map_seed=pick(seeds, ("SEED-0001",), index),
                civilization=pick(civilizations, ("GREECE", "ROME", "EGYPT", "NORWAY"), index),
                leader=pick(leaders, ("PERICLES", "TRAJAN", "CLEOPATRA", "HARALD"), index),
                ruleset=pick(rulesets, ("BBG",), index),
                model_primary=pick(
                    models, ("anthropic/claude-sonnet-4", "openai/gpt-5"), index
                ),
            )
        )
        runs.append(
            make_run(
                run_id,
                config_id=config_id,
                lifecycle_state=pick(lifecycle_states, ("finished",), index),
                record_completeness_status=completeness.get(run_id, "complete"),
                started_at=at(index),
                ended_at=at(index + turns),
            )
        )

        gaps = set(turn_gaps.get(run_id, ()))
        base = float(pick(science_base, (10.0,), index)) + index
        step = float(pick(science_step, (2.0,), index))
        culture = float(pick(culture_base, (5.0,), index)) + index

        for turn in range(1, turns + 1):
            if turn in gaps:
                continue
            record = make_turn_cycle(
                turn,
                run_id=run_id,
                capture_status=capture_status,
                yields={
                    "science_output": base + step * turn,
                    "culture_output": culture + turn,
                },
            )
            records.append(record)
            save_points.append(make_save_point(turn, run_id=run_id))
            if with_captures:
                captures.append(_capture_for(record, 0, capture_status, None))

    return FakeMatchStore(
        runs=runs,
        configurations=configurations if with_configuration else [],
        turn_cycles=records,
        save_points=save_points,
        captures=captures,
        turn_gaps={rid: list(values) for rid, values in turn_gaps.items()},
    )


class _PublishedPortOnly:
    """A store exposing *only* the reads `match-store-port.md` publishes.

    `FakeMatchStore` also offers the three optional capabilities this feature
    probes for (`get_run_configuration`, `get_capture_blob`, `list_runs`), which
    is what lets most tests exercise fully-populated pages. This wrapper is the
    other case, and it is the *realistic* one until deliverable 3 lands: a store
    that implements the published contract and nothing more.

    Forwarding by an explicit name list rather than `__getattr__` is the point --
    `getattr(store, "list_runs", None)` must genuinely find nothing, which a
    catch-all delegate would defeat.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        from civsim_web.store_client.port import READ_OPERATIONS

        if name in READ_OPERATIONS:
            return getattr(self._store, name)
        raise AttributeError(
            f"{name!r} is not a published MatchStore read; this store offers only "
            f"the operations in contracts/match-store-port.md"
        )


def published_port_only(store: Any) -> Any:
    """Hide every optional capability, leaving the published reads (plan C1)."""
    return _PublishedPortOnly(store)


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
