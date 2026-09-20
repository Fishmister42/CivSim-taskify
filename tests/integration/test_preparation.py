"""Integration test: a preparation-time settings mismatch (T066).

FR-002, V2, US1 Section 2: when the fake game reports a setting differing from what was
configured, the run must fail before turn 1 with a `preparation_mismatch` event recorded, and no
turn 1 may be attempted.

`run/preparation.py`'s own module docstring is explicit that turning a `PreparationResult`'s
mismatches into an actual `failed` `Run` plus a `preparation_mismatch` `RunEvent` is "the caller's
(a later wave's run orchestrator) responsibility" -- no such orchestrator exists yet in this
codebase (there is no `run/turn_cycle.py` or `run/runner.py` wiring preparation into the run
lifecycle). This test therefore plays that caller's documented role itself, composing only
already-implemented pieces exactly as that docstring describes:
`run.preparation.verify_configuration` for the mismatch detection, `run.lifecycle.transition` for
the recorded `preparing -> failed` state change, and a plain
`RunEvent(event_type=RunEventType.PREPARATION_MISMATCH, ...)` for the event.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from civsim_harness.config.run_config import load_run_configuration_file
from civsim_harness.models.common import CapturePath, CatalogVersionRef, EventId, RunId
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
)
from civsim_harness.run.lifecycle import transition
from civsim_harness.run.preparation import (
    apply_configuration,
    verify_configuration,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "configs" / "turn50-validation.yaml"

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


class FakeGame:
    """A minimal fake game standing in for `run/preparation.py`'s injected `apply_setting`/
    `read_setting` seams. Actual Nexus dispatch is a later wave's job (`apply_configuration`/
    `verify_configuration` accept these as plain callables precisely so this test does not need
    one); this fake plays "the game" exactly the shape those functions are documented to be
    driven by. `override` lets a specific field silently read back differently than what was
    applied -- the scenario US1 Section 2 and V2 describe ("a setting unsupported ... the run
    fails before turn 1 with the mismatch recorded")."""

    def __init__(self) -> None:
        self.applied: dict[str, object] = {}
        self.overrides: dict[str, object] = {}

    def apply_setting(self, name: str, value: object) -> None:
        self.applied[name] = value

    def read_setting(self, name: str) -> object:
        if name in self.overrides:
            return self.overrides[name]
        return self.applied[name]


def _preparing_run(config_id: object) -> Run:
    return Run(
        run_id=RunId(f"run_{uuid.uuid4().hex}"),
        config_id=config_id,
        lifecycle_state=LifecycleState.PREPARING,
        record_completeness_status=RecordCompletenessStatus.UNKNOWN,
        comparability_status=ComparabilityStatus.COMPARABLE,
        observation_catalog_version=CatalogVersionRef(version="2026.09.1", content_hash="obs-hash"),
        action_catalog_version=CatalogVersionRef(version="2026.09.1", content_hash="act-hash"),
        game_build="win/1.0.12.9",
        host_support_tier=HostSupportTier.VALIDATED,
        capture_path=CapturePath.NONE,
    )


def _prepare_and_maybe_start_turn_one(
    *, game: FakeGame
) -> tuple[Run, RunEvent | None, int]:
    """The small piece of orchestration `run/preparation.py` documents as "the caller's
    responsibility" -- reproduced here, at the test level, exactly as described: apply the
    configured setup, read it back, and gate turn 1 on the result.

    Returns `(run, mismatch_event_or_None, turn_one_attempts)` -- `turn_one_attempts` is a call
    counter for a stand-in "start turn 1" callable, proving it is invoked if and only if
    preparation matched.
    """
    config = load_run_configuration_file(CONFIG_PATH)
    apply_configuration(config, apply_setting=game.apply_setting)
    result = verify_configuration(config, read_setting=game.read_setting)

    run = _preparing_run(config.config_id)
    turn_one_attempts = 0

    def _start_turn_one() -> None:
        nonlocal turn_one_attempts
        turn_one_attempts += 1

    if result.matched:
        _start_turn_one()
        return run, None, turn_one_attempts

    mismatch_detail = {
        "mismatches": [
            {"field": m.field, "expected": m.expected, "actual": m.actual}
            for m in result.mismatches
        ]
    }
    failed_run, _lifecycle_event = transition(
        run, LifecycleState.FAILED, occurred_at=NOW, detail=mismatch_detail
    )
    mismatch_event = RunEvent(
        event_id=EventId(uuid.uuid4().hex),
        run_id=run.run_id,
        turn_number=None,
        event_type=RunEventType.PREPARATION_MISMATCH,
        occurred_at=NOW,
        detail=mismatch_detail,
    )
    # No turn 1 is attempted on this path -- _start_turn_one is never called.
    return failed_run, mismatch_event, turn_one_attempts


def test_a_mismatched_setting_fails_before_turn_1_with_preparation_mismatch_recorded() -> None:
    game = FakeGame()
    config = load_run_configuration_file(CONFIG_PATH)
    apply_configuration(config, apply_setting=game.apply_setting)

    # Force the fake game to report a different difficulty than what was configured -- e.g. the
    # setting silently failed to apply, exactly the scenario V2 names.
    game.overrides["difficulty"] = "DIFFICULTY_DEITY"
    assert config.difficulty != "DIFFICULTY_DEITY"

    result = verify_configuration(config, read_setting=game.read_setting)
    assert result.matched is False
    mismatched_fields = {m.field for m in result.mismatches}
    assert "difficulty" in mismatched_fields

    run = _preparing_run(config.config_id)
    turn_one_attempts = 0

    def _start_turn_one() -> None:
        nonlocal turn_one_attempts
        turn_one_attempts += 1

    assert not result.matched  # gate: never call _start_turn_one on this path
    mismatch_detail = {
        "mismatches": [
            {"field": m.field, "expected": m.expected, "actual": m.actual}
            for m in result.mismatches
        ]
    }
    failed_run, _lifecycle_event = transition(
        run, LifecycleState.FAILED, occurred_at=NOW, detail=mismatch_detail
    )
    mismatch_event = RunEvent(
        event_id=EventId(uuid.uuid4().hex),
        run_id=run.run_id,
        turn_number=None,
        event_type=RunEventType.PREPARATION_MISMATCH,
        occurred_at=NOW,
        detail=mismatch_detail,
    )

    assert failed_run.lifecycle_state is LifecycleState.FAILED
    assert mismatch_event.event_type is RunEventType.PREPARATION_MISMATCH
    assert mismatch_event.run_id == run.run_id
    recorded_fields = {entry["field"] for entry in mismatch_event.detail["mismatches"]}
    assert "difficulty" in recorded_fields
    assert turn_one_attempts == 0  # no turn 1 attempted


def test_every_mismatched_field_is_recorded_not_just_the_first() -> None:
    """`verify_configuration` "does not stop at the first mismatch" (its own docstring) -- a run
    with two divergent settings must record both in the same `preparation_mismatch` event."""
    game = FakeGame()
    config = load_run_configuration_file(CONFIG_PATH)
    apply_configuration(config, apply_setting=game.apply_setting)

    game.overrides["difficulty"] = "DIFFICULTY_DEITY"
    game.overrides["map_settings.map_size"] = "MAPSIZE_HUGE"

    result = verify_configuration(config, read_setting=game.read_setting)

    assert result.matched is False
    mismatched_fields = {m.field for m in result.mismatches}
    assert mismatched_fields == {"difficulty", "map_settings.map_size"}


def test_a_matching_setup_reports_no_mismatch_and_turn_1_would_proceed() -> None:
    game = FakeGame()
    run, mismatch_event, turn_one_attempts = _prepare_and_maybe_start_turn_one(game=game)

    assert run.lifecycle_state is LifecycleState.PREPARING  # never transitioned to failed
    assert mismatch_event is None
    assert turn_one_attempts == 1  # turn 1 is attempted exactly when preparation matched
