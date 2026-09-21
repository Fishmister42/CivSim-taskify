"""One definition of the observation-assembly-failure event (T096, T245).

T245 is T231's exact shape recurring: ``observe/assemble.py``'s
``handle_assembly_failure`` -- T096's named home for turning an
``ObservationAssemblyError`` into its ``observation_assembly_failed``
``RunEvent`` (FR-046) -- had zero callers, while ``resilience/recovery.py``
built the same event inline. Two implementations that agree on every input,
which is precisely why no behavioural test could catch the split: the guard
must be structural (T231's AST-scan idiom, ``tests/contract/
test_parity_redteam.py``'s precedent). Resolved the same way T231 was: the
caller now routes through the named home, and this file holds the guards.

- The AST scan asserts exactly one module in ``src/civsim_harness/``
  constructs a ``RunEvent`` with ``event_type=...OBSERVATION_ASSEMBLY_FAILED``
  -- naming the enum elsewhere (recovery's ``trigger_event_type``, audit's
  event filter) is reading, not building, and does not count.
- The spy proves recovery's production path actually *calls* the helper --
  an unused import beside a surviving inline twin is the exact failure mode
  Phase 11 was written about, and the one an import-level check would miss.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.errors import ObservationAssemblyError
from civsim_harness.models.common import (
    CapturePath,
    CatalogVersionRef,
    ConfigId,
    EventId,
    RunId,
    SavePointId,
)
from civsim_harness.models.records import RetentionStatus, RunEvent, RunEventType, SavePoint
from civsim_harness.models.run import (
    ComparabilityStatus,
    HostSupportTier,
    LifecycleState,
    RecordCompletenessStatus,
    Run,
)
from civsim_harness.observe.assemble import handle_assembly_failure
from civsim_harness.resilience import recovery as recovery_module
from civsim_harness.resilience.recovery import RecoveryEngine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS_ROOT = _REPO_ROOT / "src" / "civsim_harness"

_T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# The helper's own contract (T096): the event it builds.
# --------------------------------------------------------------------------


def test_handle_assembly_failure_builds_the_fr046_event() -> None:
    error = ObservationAssemblyError(
        "capability result failed its declared output_schema",
        detail={"declaration_id": "game.turn_state", "step_index": 4},
    )

    event = handle_assembly_failure(
        error,
        run_id=RunId("run-1"),
        turn_number=7,
        step_index=4,
        occurred_at=_T0,
        event_id=EventId("evt-fixed"),
    )

    assert event.event_type is RunEventType.OBSERVATION_ASSEMBLY_FAILED
    assert event.run_id == "run-1"
    assert event.turn_number == 7
    assert event.step_index == 4
    assert event.occurred_at == _T0
    assert event.event_id == "evt-fixed"
    # The error's own message and detail ride on the event verbatim.
    assert event.detail["message"] == error.message
    assert event.detail["declaration_id"] == "game.turn_state"
    assert event.detail["step_index"] == 4


def test_handle_assembly_failure_records_no_step_index_when_the_caller_has_none() -> None:
    """The recovery path (the production caller, T245) has no discrete step
    index of its own by the time it runs -- the step attribution, where it
    exists, lives inside the error's own detail and is merged verbatim."""
    error = ObservationAssemblyError("stale board mid-turn", detail={"step_index": 3})

    event = handle_assembly_failure(
        error, run_id=RunId("run-1"), turn_number=2, occurred_at=_T0
    )

    assert event.step_index is None
    assert event.detail["step_index"] == 3
    assert event.event_id  # generated when the caller supplies none


# --------------------------------------------------------------------------
# The spy: recovery's production path routes through the named home (T245).
# Mirrors tests/contract/test_parity_redteam.py's T231 spy -- the call must
# actually happen, since an unused import beside a revived inline twin is
# the exact failure mode this task closed.
# --------------------------------------------------------------------------


def _run(lifecycle_state: LifecycleState) -> Run:
    return Run.model_validate(
        {
            "run_id": RunId("run-1"),
            "config_id": ConfigId("cfg-1"),
            "lifecycle_state": lifecycle_state,
            "record_completeness_status": RecordCompletenessStatus.UNKNOWN,
            "comparability_status": ComparabilityStatus.COMPARABLE,
            "observation_catalog_version": CatalogVersionRef(
                version="1.0.0", content_hash="obs-hash"
            ),
            "action_catalog_version": CatalogVersionRef(
                version="1.0.0", content_hash="act-hash"
            ),
            "game_build": "win/1.0.12.9",
            "host_support_tier": HostSupportTier.VALIDATED,
            "capture_path": CapturePath.NONE,
        }
    )


def _save(turn_number: int) -> SavePoint:
    return SavePoint(
        save_point_id=SavePointId(f"sp-{turn_number}"),
        run_id=RunId("run-1"),
        turn_number=turn_number,
        save_name=f"civsim__run-1__t{turn_number:04d}",
        taken_at=_T0,
        verified=True,
        retention_status=RetentionStatus.RETAINED,
    )


@dataclass
class _FakeStore:
    """Minimal duck-typed `MatchStore`: only the operations recovery.py calls."""

    run: Run
    events: list[RunEvent] = field(default_factory=list)

    def write_run_event(self, event: RunEvent) -> Any:
        self.events.append(event)
        return event.event_id

    def update_run(self, run_id: RunId, **fields: Any) -> None:
        merged = self.run.model_dump()
        merged.update(fields)
        self.run = Run.model_validate(merged)

    def get_run(self, run_id: RunId) -> Run | None:
        return self.run

    def get_last_known_good(self, run_id: RunId) -> SavePoint | None:
        return None


@dataclass
class _FakeLoader:
    calls: int = 0

    async def load(self, save: SavePoint) -> None:
        self.calls += 1


async def test_recovery_builds_the_assembly_failure_event_through_the_named_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T245: `RecoveryEngine.recover_from_observation_assembly_error` builds
    its `observation_assembly_failed` event via `observe.assemble.
    handle_assembly_failure`, and the event the spy saw built is the event
    the store received -- not a byproduct beside a surviving inline twin.
    """
    built: list[RunEvent] = []
    real = recovery_module.handle_assembly_failure

    def spy(*args: Any, **kwargs: Any) -> RunEvent:
        event = real(*args, **kwargs)
        built.append(event)
        return event

    monkeypatch.setattr(recovery_module, "handle_assembly_failure", spy)

    run = _run(LifecycleState.PLAYING)
    store = _FakeStore(run=run)
    engine = RecoveryEngine(
        run_id=run.run_id, store=store, loader=_FakeLoader(), recovery_attempt_limit=3
    )
    error = ObservationAssemblyError("stale board mid-turn", detail={"step_index": 4})

    result = await engine.recover_from_observation_assembly_error(
        run, turn_number=2, turn_start_save=_save(2), error=error
    )

    assert result.run.lifecycle_state is LifecycleState.PLAYING
    assert len(built) == 1, "recovery did not route through handle_assembly_failure"
    written = [
        e for e in store.events if e.event_type is RunEventType.OBSERVATION_ASSEMBLY_FAILED
    ]
    assert written == built, (
        "the observation_assembly_failed event the store received is not the one "
        "handle_assembly_failure built -- an inline twin is back (T245)"
    )
    assert written[0].turn_number == 2
    assert written[0].detail["message"] == error.message
    assert written[0].detail["step_index"] == 4


# --------------------------------------------------------------------------
# The structural guard: exactly one module builds the event (T231's idiom).
# --------------------------------------------------------------------------


def _references_the_enum_member(node: ast.expr) -> bool:
    return any(
        isinstance(sub, ast.Attribute) and sub.attr == "OBSERVATION_ASSEMBLY_FAILED"
        for sub in ast.walk(node)
    )


def _modules_building_the_event(root: Path) -> set[str]:
    """Every module under *root* containing a call that passes
    ``event_type=<...OBSERVATION_ASSEMBLY_FAILED>`` -- the construction
    signature shared by ``RunEvent(...)`` and any local builder wrapping it.
    Merely *naming* the enum member (an event filter, a ``trigger_event_type``
    argument, the enum's own definition) is reading, not building, and is
    deliberately not counted -- the same loads-are-not-callers reasoning as
    ``tests/contract/test_reachability.py``.
    """
    builders: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "event_type" and _references_the_enum_member(keyword.value):
                    builders.add(path.relative_to(root).as_posix())
    return builders


def test_exactly_one_module_in_the_harness_builds_the_assembly_failure_event() -> None:
    """T245, by T231's rule: one definition, and it lives in the named home.
    A second module constructing this event -- recovery growing its inline
    builder back, or a third copy anywhere else -- fails here by name, which
    no behavioural test can do while the copies still agree.
    """
    builders = _modules_building_the_event(_HARNESS_ROOT)
    assert builders == {"observe/assemble.py"}, (
        "the observation_assembly_failed RunEvent must have exactly one "
        f"construction site (observe/assemble.py::handle_assembly_failure); found: "
        f"{sorted(builders)}"
    )


def test_the_event_construction_scan_can_fail(tmp_path: Path) -> None:
    """The scan's own negative control (the reachability suite's precedent):
    a synthetic tree carrying an inline second builder is flagged, and the
    same tree without it is not -- so a green scan means something.
    """
    home = "home.py"
    twin = "twin.py"
    (tmp_path / home).write_text(
        "def build():\n"
        "    return RunEvent(event_type=RunEventType.OBSERVATION_ASSEMBLY_FAILED)\n",
        encoding="utf-8",
    )
    assert _modules_building_the_event(tmp_path) == {home}

    (tmp_path / twin).write_text(
        "def sneak():\n"
        "    return _event(event_type=RunEventType.OBSERVATION_ASSEMBLY_FAILED)\n"
        "\n"
        "def reader():\n"
        "    return [RunEventType.OBSERVATION_ASSEMBLY_FAILED]\n",
        encoding="utf-8",
    )
    assert _modules_building_the_event(tmp_path) == {home, twin}
