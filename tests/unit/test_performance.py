"""T204 -- the performance pass (plan.md "Performance Goals").

Two real-timing measurements, against the actual implementations rather than fakes standing in for
them, each guarding one of plan.md's two performance commitments:

1. :func:`test_store_write_turn_cycle_stays_well_inside_the_5s_currency_window` -- "A completed
   turn's record durably persisted before the turn is ended, and visible to deliverable 1 within
   its 5 s currency window" (plan.md line 161-162). Writes a real, several-hundred-step
   ``TurnCycleRecord`` through a real ``SqliteMatchStore`` and times the wall-clock cost of
   ``write_turn_cycle`` itself -- the one call that must return well before deliverable 1's 5 s
   polling window could ever be at risk.
2. :func:`test_detection_check_once_stays_well_inside_the_60s_budget_under_late_game_load` --
   "Crash, hang, or unresponsiveness detected and recorded within 60 s (SC-010)" (plan.md line
   158). Runs ``DetectionAggregator.check_once`` once per decision step across a late-game-sized
   turn (500 steps, matching the step count ``tests/unit/test_no_truncation.py`` already uses for
   the decision loop itself) and times the cumulative wall-clock cost.

Both thresholds below are deliberately generous -- an order of magnitude or more above the actual
measured cost on ordinary hardware. The point of this module is catching a regression that makes
either path *qualitatively* slower (an accidental per-row fsync, an O(n^2) walk, a stray
``sleep``), not micro-benchmarking either one. Per the task brief this pass was commissioned under:
**measure, don't assert** -- if a measurement ever misses its budget, that is a finding to report,
not a threshold to loosen.

**This module measures elapsed time; it does not make anything *depend* on elapsed time.**
Nothing here changes ``run/turn_cycle.py`` or ``resilience/detector.py`` to add a turn-level time
bound -- FR-014 says a turn has no time bound, and that stays true regardless of how fast a single
store write or a single detection pass happens to run. ``run_decision_loop`` still has exactly the
same two exit conditions (the agent's own end-turn decision, or the no-progress backstop) it had
before this file existed; this file never imports or touches it.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civsim_harness.models.common import RunId
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.decision import Decision
from civsim_harness.models.records import ModelCall
from civsim_harness.models.run import Run
from civsim_harness.models.turn import DecisionStep, Observation, TurnCycle
from civsim_harness.nexus.client import StateIndices
from civsim_harness.resilience.detector import DetectionAggregator
from civsim_harness.resilience.heartbeat_monitor import HeartbeatMonitor
from civsim_harness.resilience.liveness import ProcessLivenessMonitor
from civsim_harness.store.port import DecisionStepBundle, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)

# --------------------------------------------------------------------------
# Part 1 -- store write, several-hundred-step turn, 5 s currency window
# --------------------------------------------------------------------------

#: Comfortably in "several hundred" territory (plan.md's own phrase), and matching the scale
#: `tests/unit/test_no_truncation.py` (500) and `tests/contract/test_match_store_port.py` (350)
#: already exercise elsewhere in this suite for the same reason: a late-game turn is not small.
STORE_STEP_COUNT = 400

#: Less than half of deliverable 1's own 5 s currency window (plan.md line 162), leaving generous
#: room for everything else in the write-then-advance/polling pipeline this one call is only a part
#: of -- while still an order of magnitude above the sub-100ms cost a single-transaction, WAL-mode
#: SQLite write of this size is actually expected to take on ordinary hardware.
STORE_WRITE_THRESHOLD_S = 2.0


def _catalog_ref() -> dict[str, str]:
    return {"version": "2026.09.1", "content_hash": "perf-hash"}


def _make_run(run_id: str) -> Run:
    return Run.model_validate(
        {
            "run_id": run_id,
            "config_id": "cfg-perf",
            "lifecycle_state": "playing",
            "record_completeness_status": "unknown",
            "comparability_status": "comparable",
            "observation_catalog_version": _catalog_ref(),
            "action_catalog_version": _catalog_ref(),
            "game_build": "win/1.0.12.9",
            "host_support_tier": "validated",
            "capture_path": "none",
        }
    )


def _make_config(config_id: str) -> RunConfiguration:
    return RunConfiguration.model_validate(
        {
            "config_id": config_id,
            "map_seed": "1",
            "civilization": "CIVILIZATION_ROME",
            "leader": "LEADER_TRAJAN",
            "ruleset": "RULESET_STANDARD",
            "difficulty": "DIFFICULTY_PRINCE",
            "stop_condition": {"type": "turn_reached", "turn": 300},
            "model_config": {"primary": {"provider": "openrouter", "model": "x"}},
            "no_progress_step_limit": 8,
            "recovery_attempt_limit": 3,
            "min_free_disk_gb": 25,
            "created_at": NOW,
        }
    )


def _make_step_bundle(run_id: str, turn_cycle_id: str, step_index: int) -> DecisionStepBundle:
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
            "catalog_version": _catalog_ref(),
            "entries": [
                {
                    "declaration_id": "game.turn_state",
                    "key": "turn_number",
                    "value": step_index,
                    "context": "InGame",
                }
            ],
            "captures": [],
            "screen_identity": "world",
        }
    )
    decision = Decision.model_validate(
        {
            "decision_id": dec_id,
            "decision_step_id": step_id,
            "action_declaration_id": "units.move_to",
            "reasoning": f"performance-pass filler reasoning for step {step_index}",
            "trigger": "proactive",
            "model_call_id": call_id,
            "execution": {"outcome": "applied", "verified_at": NOW},
        }
    )
    model_call = ModelCall.model_validate(
        {
            "model_call_id": call_id,
            "run_id": run_id,
            "turn_cycle_id": turn_cycle_id,
            "decision_step_id": step_id,
            "model_requested": {"provider": "openrouter", "model": "x"},
            "model_served": {"provider": "openrouter", "model": "x"},
            "latency_ms": 100,
            "cost": {},
            "retry_count": 0,
            "fallback_occurred": False,
            "image_count": 0,
            "outcome": "decision_returned",
        }
    )
    return DecisionStepBundle(
        step=step, observation=observation, decision=decision, model_call=model_call
    )


def _make_turn_cycle_record(run_id: str, turn_number: int, *, num_steps: int) -> TurnCycleRecord:
    turn_cycle_id = f"{run_id}-t{turn_number}-perf"
    bundles = [
        _make_step_bundle(run_id, turn_cycle_id, index) for index in range(1, num_steps + 1)
    ]
    turn_cycle = TurnCycle.model_validate(
        {
            "turn_cycle_id": turn_cycle_id,
            "run_id": run_id,
            "turn_number": turn_number,
            "attempt_index": 0,
            "is_authoritative": True,
            "save_point_id": f"{run_id}-sp{turn_number}",
            "step_count": len(bundles),
            "outcome": "ended_by_agent",
            "final_no_progress_streak": 0,
            "visually_degraded": False,
            "started_at": NOW,
            "ended_at": NOW,
            "persisted_at": NOW,
        }
    )
    return TurnCycleRecord(turn_cycle=turn_cycle, steps=bundles)


def test_store_write_turn_cycle_stays_well_inside_the_5s_currency_window(tmp_path: Path) -> None:
    """Real timing: one atomic ``write_turn_cycle`` call for a 400-step late-game turn, against a
    real ``SqliteMatchStore`` (WAL journal mode, ``synchronous=FULL`` -- the strongest durability
    pragma this adapter offers, per its own module docstring), must return in well under
    deliverable 1's 5 s currency window (plan.md "Performance Goals").

    This is the actual store implementation `write_turn_cycle` uses in production, not a fake --
    the timing is only meaningful measured against the real fsync-before-return durability path.
    """
    store = SqliteMatchStore(tmp_path / "match.db")
    try:
        run_id = "perf-run-store"
        store.create_run(_make_run(run_id), _make_config("cfg-perf"))
        record = _make_turn_cycle_record(run_id, 1, num_steps=STORE_STEP_COUNT)

        started = time.perf_counter()
        store.write_turn_cycle(record)
        elapsed_s = time.perf_counter() - started
    finally:
        store.close()

    assert elapsed_s < STORE_WRITE_THRESHOLD_S, (
        f"write_turn_cycle took {elapsed_s:.3f}s for a {STORE_STEP_COUNT}-step turn -- "
        f"expected well under the 5s currency window (threshold {STORE_WRITE_THRESHOLD_S}s); "
        "this is a real regression, not a flaky timing, if it reproduces"
    )


# --------------------------------------------------------------------------
# Part 2 -- detection under late-game load, 60 s budget (SC-010)
# --------------------------------------------------------------------------

#: Matches the step count `tests/unit/test_no_truncation.py`'s own 500-step productive turn uses --
#: one detection pass per decision step, the cadence a real run orchestrator would poll at.
DETECTION_STEP_COUNT = 500

#: An order of magnitude below SC-010's own 60 s budget: 500 detection passes -- an entire
#: late-game turn's worth -- completing in well under 15s, let alone 60s, is what confirms nothing
#: here has grown a hidden per-call cost that would eat into the budget a single fault's detection
#: is supposed to have.
DETECTION_THRESHOLD_S = 15.0


class _FakeHeartbeatClient:
    """Duck-typed `NexusClient` stand-in: only what `probe_heartbeat` touches (mirrors
    `tests/unit/test_detection.py`'s own fixture of the same shape). Always reports a healthy
    round-trip -- this test measures the aggregator's own overhead under sustained calling, not a
    scripted failure path, which the functional tests in `test_detection.py` already cover.
    """

    def __init__(self) -> None:
        self.state_indices = StateIndices(
            by_name={"GameCore_Tuner": 0, "InGame": 1}, game_core_tuner=0, in_game=1
        )
        self.calls = 0

    async def execute_command(
        self, *, state_index: int, lua_body: str, timeout_s: float | None = None
    ) -> Any:
        self.calls += 1
        return True


class _ScreenProbe:
    """Always-known screen-identity probe, for the same reason as `_FakeHeartbeatClient` above."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return True


async def test_detection_check_once_stays_well_inside_the_60s_budget_under_late_game_load() -> (
    None
):
    """Real timing: `DetectionAggregator.check_once` -- all three signals wired (process liveness
    against this test process's own real PID via `psutil`, a heartbeat, a screen-identity probe) --
    called once per decision step across a 500-step late-game turn, must have its *cumulative*
    wall-clock cost stay well inside SC-010's 60 s crash/hang/unresponsiveness detection budget.

    `DetectionAggregator` itself has no notion of a turn (see its own module docstring: "No signal,
    and no function in this module, measures elapsed turn time") and this test does not give it
    one -- `occurred_at` is supplied per call exactly as any real caller would, and nothing here
    reads back an accumulated duration. It only calls `check_once` the number of times a genuinely
    long turn's own polling cadence would, and times how long that actually takes.
    """
    liveness = ProcessLivenessMonitor(pid=os.getpid())
    heartbeat = HeartbeatMonitor(client=_FakeHeartbeatClient())  # type: ignore[arg-type]
    screen = _ScreenProbe()
    aggregator = DetectionAggregator(
        liveness=liveness, heartbeat=heartbeat, screen_identity_probe=screen
    )

    started = time.perf_counter()
    for step_index in range(1, DETECTION_STEP_COUNT + 1):
        events = await aggregator.check_once(
            run_id=RunId("perf-run-detection"),
            occurred_at=NOW,
            turn_number=287,
            step_index=step_index,
        )
        assert events == []  # every signal healthy throughout -- only timing is under test here
    elapsed_s = time.perf_counter() - started

    assert elapsed_s < DETECTION_THRESHOLD_S, (
        f"DetectionAggregator.check_once took {elapsed_s:.3f}s across "
        f"{DETECTION_STEP_COUNT} calls ({elapsed_s / DETECTION_STEP_COUNT * 1000:.3f}ms/call) -- "
        f"expected well under SC-010's 60s budget (threshold {DETECTION_THRESHOLD_S}s); "
        "this is a real regression, not a flaky timing, if it reproduces"
    )
