"""Unit tests for `civsim_harness.operator.audit` (T138-T141).

Exercises every audit function against `SqliteMatchStore` (the reference
`MatchStore` adapter) loaded with hand-built, individually-valid records --
the same "build through `.model_validate()`" discipline
`tests/contract/test_match_store_port.py` uses, so every fixture also
exercises each model's own validators. The real `catalogs/` at the repo root
is used for parity/capability resolution (`units.move_to`, `map.state`,
`views.world` are real, already-authored declarations), so these tests double
as a sanity check that the audits work against production catalog data, not
just a synthetic fixture catalog.

A few tests reach for `Model.model_construct()` to build a record shape the
normal, validated construction path cannot produce (e.g. a `Decision` with a
`model_call_id` that does not match its paired `ModelCall` -- normally
rejected by `DecisionStepBundle`'s own cross-record validator). Those are
explicitly defense-in-depth checks: proof that the audit itself would catch
the violation if it ever reached the store by some path this codebase's own
models did not anticipate, not a claim that the violation is reachable through
`MatchStore.write_turn_cycle` today.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.capability.loader import Catalog, load_catalog
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.models.catalog import (
    CapabilityPath,
    CatalogVersion,
    IntegrationCapability,
    ParityDeclaration,
)
from civsim_harness.models.common import AcceptanceId, CapabilityId, DeclarationId, SeedSetId
from civsim_harness.models.config import RunConfiguration, SeedSet
from civsim_harness.models.decision import Decision
from civsim_harness.models.records import ModelCall, RunEvent, SavePoint
from civsim_harness.models.run import Run
from civsim_harness.models.turn import DecisionStep, Observation, TurnCycle
from civsim_harness.operator import audit
from civsim_harness.store.port import DecisionStepBundle, MatchStore, TurnCycleRecord
from civsim_harness.store.sqlite_adapter import SqliteMatchStore

REPO_CATALOG_ROOT = Path(__file__).resolve().parents[2] / "catalogs"
NOW = datetime(2026, 1, 1, tzinfo=UTC)

OBSERVATION_DECLARATION = DeclarationId("map.state")
VIEW_DECLARATION = DeclarationId("views.world")
ACTION_DECLARATION = DeclarationId("units.move_to")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteMatchStore]:
    adapter = SqliteMatchStore(tmp_path / "match.db")
    yield adapter
    adapter.close()


@pytest.fixture(scope="module")
def registry() -> CapabilityRegistry:
    return CapabilityRegistry(load_catalog(REPO_CATALOG_ROOT))


class _NoSideChannelStore:
    """Wraps a real `MatchStore`, hiding `_conn`/`get_capture`/`list_run_events`
    so `audit._capture_reader` / `audit._event_reader` see an adapter with no
    way to answer those questions -- proving the "unverifiable" degradation
    path (see `operator/audit.py`'s module docstring) without needing a second
    real `MatchStore` implementation.
    """

    _HIDDEN = frozenset({"_conn", "get_capture", "list_run_events"})

    def __init__(self, inner: MatchStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in self._HIDDEN:
            raise AttributeError(name)
        return getattr(self._inner, name)


# --------------------------------------------------------------------------
# Record builders
# --------------------------------------------------------------------------


def _catalog_ref() -> dict[str, str]:
    return {"version": "2026.09.1", "content_hash": "abc123"}


def _make_config(config_id: str) -> RunConfiguration:
    return RunConfiguration.model_validate(
        {
            "config_id": config_id,
            "map_seed": "123",
            "civilization": "CIVILIZATION_ROME",
            "leader": "LEADER_TRAJAN",
            "ruleset": "RULESET_STANDARD",
            "difficulty": "DIFFICULTY_PRINCE",
            "stop_condition": {"type": "turn_reached", "turn": 50},
            "model_config": {"primary": {"provider": "openrouter", "model": "x"}},
            "no_progress_step_limit": 8,
            "recovery_attempt_limit": 3,
            "min_free_disk_gb": 25,
            "created_at": NOW,
        }
    )


def _make_run(run_id: str) -> Run:
    return Run.model_validate(
        {
            "run_id": run_id,
            "config_id": "cfg-" + run_id,
            "lifecycle_state": "playing",
            "record_completeness_status": "unknown",
            "comparability_status": "comparable",
            "observation_catalog_version": _catalog_ref(),
            "action_catalog_version": _catalog_ref(),
            "game_build": "win/1.0.12.9",
            "host_support_tier": "validated",
            "capture_path": "windows_graphics_capture",
        }
    )


def _make_save_point(run_id: str, turn_number: int) -> dict[str, Any]:
    return {
        "save_point_id": f"{run_id}-sp{turn_number}",
        "run_id": run_id,
        "turn_number": turn_number,
        "save_name": f"civsim__{run_id}__t{turn_number:04d}",
        "taken_at": NOW,
        "verified": True,
        "retention_status": "retained",
    }


def _make_turn_cycle(
    turn_cycle_id: str, run_id: str, turn_number: int, *, step_count: int
) -> TurnCycle:
    return TurnCycle.model_validate(
        {
            "turn_cycle_id": turn_cycle_id,
            "run_id": run_id,
            "turn_number": turn_number,
            "attempt_index": 0,
            "is_authoritative": True,
            "save_point_id": f"{run_id}-sp{turn_number}",
            "step_count": step_count,
            "outcome": "ended_by_agent",
            "final_no_progress_streak": 0,
            "visually_degraded": False,
            "started_at": NOW,
            "ended_at": NOW,
            "persisted_at": NOW,
        }
    )


def _make_step_bundle(
    run_id: str,
    turn_cycle_id: str,
    step_index: int,
    *,
    observation_id: str | None = None,
    capture_ids: list[str] | None = None,
    observation_declaration: DeclarationId = OBSERVATION_DECLARATION,
    action_declaration: DeclarationId = ACTION_DECLARATION,
    trigger: str = "proactive",
    prompt_type: str | None = None,
    assembled_at: datetime = NOW,
    verified_at: datetime = NOW,
) -> DecisionStepBundle:
    step_id = f"{turn_cycle_id}-step{step_index}"
    obs_id = observation_id or f"{step_id}-obs"
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
            "assembled_at": assembled_at,
            "catalog_version": _catalog_ref(),
            "entries": [
                {
                    "declaration_id": observation_declaration,
                    "key": "k",
                    "value": 1,
                    "context": "GameCore_Tuner",
                }
            ],
            "captures": list(capture_ids or []),
            "screen_identity": "world",
        }
    )
    decision = Decision.model_validate(
        {
            "decision_id": dec_id,
            "decision_step_id": step_id,
            "action_declaration_id": action_declaration,
            "reasoning": "test reasoning",
            "trigger": trigger,
            "prompt_type": prompt_type,
            "model_call_id": call_id,
            "execution": {
                "outcome": "applied",
                # FR-011: an applied execution must carry the predicate verdict that
                # confirmed it; an empty verification means nothing re-read the board.
                "verification": {"declaration_id": ACTION_DECLARATION, "result": True},
                "verified_at": verified_at,
            },
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


def _seed_run_with_turns(
    store: MatchStore, run_id: str, turns: dict[int, list[DecisionStepBundle]]
) -> None:
    """Create *run_id*, then write one save point + one authoritative turn
    cycle per `(turn_number, bundles)` pair in *turns* -- the minimum a real
    write path would produce, and exactly what `_discover_turns` (via save
    points) and `get_turn_cycle` need to make a turn auditable.
    """
    from civsim_harness.models.records import SavePoint

    store.create_run(_make_run(run_id), _make_config("cfg-" + run_id))
    for turn_number, bundles in turns.items():
        store.write_save_point(SavePoint.model_validate(_make_save_point(run_id, turn_number)))
        turn_cycle_id = bundles[0].step.turn_cycle_id
        tc = _make_turn_cycle(turn_cycle_id, run_id, turn_number, step_count=len(bundles))
        store.write_turn_cycle(TurnCycleRecord(turn_cycle=tc, steps=bundles))


# --------------------------------------------------------------------------
# Shared precondition behaviour: INSUFFICIENT_DATA is distinct from PASSED
# --------------------------------------------------------------------------


def _call(
    name: str, store: MatchStore, registry: CapabilityRegistry, run_id: str
) -> audit.AuditReport:
    fn = getattr(audit, f"audit_{name}")
    if name in ("parity", "capabilities"):
        return fn(store, registry, run_id)  # type: ignore[no-any-return]
    return fn(store, run_id)  # type: ignore[no-any-return]


@pytest.mark.parametrize("name", ["parity", "prompts", "decisions", "steps", "capabilities"])
def test_insufficient_data_when_run_is_not_on_record(
    store: SqliteMatchStore, registry: CapabilityRegistry, name: str
) -> None:
    report = _call(name, store, registry, "no-such-run")
    assert report.outcome is audit.AuditOutcome.INSUFFICIENT_DATA
    assert report.reason
    assert report.findings == []
    assert audit.exit_code_for(report) == 2


@pytest.mark.parametrize("name", ["parity", "prompts", "decisions", "steps", "capabilities"])
def test_insufficient_data_when_run_has_no_turns_yet(
    store: SqliteMatchStore, registry: CapabilityRegistry, name: str
) -> None:
    store.create_run(_make_run("run-empty"), _make_config("cfg-empty"))
    report = _call(name, store, registry, "run-empty")
    assert report.outcome is audit.AuditOutcome.INSUFFICIENT_DATA
    assert "no recorded turns" in (report.reason or "")


def test_audit_loop_insufficient_data_for_missing_run_and_missing_turn(
    store: SqliteMatchStore,
) -> None:
    missing_run = audit.audit_loop(store, "no-such-run", 1)
    assert missing_run.outcome is audit.AuditOutcome.INSUFFICIENT_DATA

    store.create_run(_make_run("run-noturn"), _make_config("cfg-noturn"))
    missing_turn = audit.audit_loop(store, "run-noturn", 1)
    assert missing_turn.outcome is audit.AuditOutcome.INSUFFICIENT_DATA


# --------------------------------------------------------------------------
# T138 -- audit_parity
# --------------------------------------------------------------------------


def test_audit_parity_passes_for_a_well_formed_run(
    store: SqliteMatchStore, registry: CapabilityRegistry
) -> None:
    from civsim_harness.models.turn import ScreenCapture

    run_id = "run-parity-ok"
    bundle = _make_step_bundle(run_id, f"{run_id}-t1-a0", 1, capture_ids=["cap-1"])
    _seed_run_with_turns(store, run_id, {1: [bundle]})
    store.write_capture(
        ScreenCapture.model_validate(
            {
                "capture_id": "cap-1",
                "run_id": run_id,
                "turn_number": 1,
                "decision_step_id": bundle.step.decision_step_id,
                "captured_at": NOW,
                "view_declaration_id": VIEW_DECLARATION,
                "screening_status": "screened_clean",
                "shown_to_agent": True,
                "retained_as_evidence": True,
                "blob_ref": __import__("hashlib").sha256(b"x").hexdigest(),
                "capture_path": "windows_graphics_capture",
            }
        ),
        b"x",
    )

    report = audit.audit_parity(store, registry, run_id)

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.findings == []
    assert report.summary["turns_examined"] == 1
    assert report.summary["steps_examined"] == 1
    assert report.summary["distinct_observation_declarations"] == 1
    assert report.summary["distinct_view_declarations"] == 1
    assert report.summary["distinct_action_declarations"] == 1
    assert report.summary["visual_capture_lookup_supported"] is True


def test_audit_parity_flags_an_unresolved_observation_declaration(
    store: SqliteMatchStore, registry: CapabilityRegistry
) -> None:
    run_id = "run-parity-bad-obs"
    bundle = _make_step_bundle(
        run_id,
        f"{run_id}-t1-a0",
        1,
        observation_declaration=DeclarationId("made.up.observation"),
    )
    _seed_run_with_turns(store, run_id, {1: [bundle]})

    report = audit.audit_parity(store, registry, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    kinds = {f["kind"] for f in report.findings}
    assert "unresolved_observation" in kinds


def test_audit_parity_flags_an_unresolved_action_declaration(
    store: SqliteMatchStore, registry: CapabilityRegistry
) -> None:
    run_id = "run-parity-bad-act"
    bundle = _make_step_bundle(
        run_id, f"{run_id}-t1-a0", 1, action_declaration=DeclarationId("made.up.action")
    )
    _seed_run_with_turns(store, run_id, {1: [bundle]})

    report = audit.audit_parity(store, registry, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "unresolved_action" for f in report.findings)


def test_audit_parity_flags_a_dangling_capture_reference(
    store: SqliteMatchStore, registry: CapabilityRegistry
) -> None:
    run_id = "run-parity-dangling"
    bundle = _make_step_bundle(run_id, f"{run_id}-t1-a0", 1, capture_ids=["cap-never-written"])
    _seed_run_with_turns(store, run_id, {1: [bundle]})

    report = audit.audit_parity(store, registry, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "dangling_capture_reference" for f in report.findings)


def test_audit_parity_reports_visual_capture_unverifiable_when_store_cannot_answer(
    store: SqliteMatchStore, registry: CapabilityRegistry
) -> None:
    run_id = "run-parity-noconn"
    bundle = _make_step_bundle(run_id, f"{run_id}-t1-a0", 1, capture_ids=["cap-1"])
    _seed_run_with_turns(store, run_id, {1: [bundle]})

    wrapped = _NoSideChannelStore(store)
    report = audit.audit_parity(wrapped, registry, run_id)  # type: ignore[arg-type]

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "visual_capture_unverifiable" for f in report.findings)
    assert report.summary["visual_capture_lookup_supported"] is False


# --------------------------------------------------------------------------
# T139 -- audit_prompts / audit_decisions
# --------------------------------------------------------------------------


def test_audit_prompts_counts_prompt_response_decisions_and_stalls(
    store: SqliteMatchStore,
) -> None:
    run_id = "run-prompts"
    proactive = _make_step_bundle(run_id, f"{run_id}-t1-a0", 1)
    prompt = _make_step_bundle(
        run_id,
        f"{run_id}-t1-a0",
        2,
        trigger="prompt_response",
        prompt_type="unit_promotion",
    )
    _seed_run_with_turns(store, run_id, {1: [proactive, prompt]})

    report = audit.audit_prompts(store, run_id)
    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["prompt_response_decisions"] == 1
    assert report.summary["recorded_stalls"] == 0
    assert report.summary["stall_visibility_supported"] is True

    store.write_run_event(
        RunEvent.model_validate(
            {
                "event_id": "evt-stall-1",
                "run_id": run_id,
                "event_type": "unknown_screen",
                "occurred_at": NOW,
            }
        )
    )
    report_after_stall = audit.audit_prompts(store, run_id)
    assert report_after_stall.summary["recorded_stalls"] == 1


def test_audit_prompts_degrades_honestly_when_stall_visibility_is_unsupported(
    store: SqliteMatchStore,
) -> None:
    run_id = "run-prompts-noconn"
    _seed_run_with_turns(store, run_id, {1: [_make_step_bundle(run_id, f"{run_id}-t1-a0", 1)]})

    report = audit.audit_prompts(_NoSideChannelStore(store), run_id)  # type: ignore[arg-type]
    assert report.summary["stall_visibility_supported"] is False
    assert report.summary["recorded_stalls"] is None


def test_audit_decisions_passes_for_well_formed_decisions(store: SqliteMatchStore) -> None:
    run_id = "run-decisions-ok"
    _seed_run_with_turns(store, run_id, {1: [_make_step_bundle(run_id, f"{run_id}-t1-a0", 1)]})

    report = audit.audit_decisions(store, run_id)

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["decisions_examined"] == 1


class _FixedRecordStore:
    """A minimal `MatchStore` stand-in that hands back one fixed, in-memory
    `TurnCycleRecord` for one turn, bypassing `SqliteMatchStore`'s own
    write/read round-trip entirely.

    Used only to prove `audit_decisions`'s own finding-detection logic is
    correct against a record shape that cannot actually reach a real store:
    `DecisionStepBundle`'s cross-record validator rejects a
    decision/model_call `model_call_id` mismatch *both* at bundle
    construction time and again when such a bundle is nested inside a
    `TurnCycleRecord` built the normal way (pydantic re-validates nested
    model instances there) -- so this fake is the only way to hand
    `audit_decisions` that shape at all, built with `model_construct` on both
    the bundle and the record to skip validation completely.
    """

    def __init__(self, run: Run, turn_number: int, record: TurnCycleRecord) -> None:
        self._run = run
        self._turn_number = turn_number
        self._record = record

    def get_run(self, run_id: str) -> Run | None:
        return self._run if run_id == self._run.run_id else None

    def list_save_points(self, run_id: str) -> list[Any]:
        from civsim_harness.models.records import SavePoint

        if run_id != self._run.run_id:
            return []
        return [SavePoint.model_validate(_make_save_point(run_id, self._turn_number))]

    def get_turn_cycle(
        self, run_id: str, turn: int, *, authoritative_only: bool = True
    ) -> TurnCycleRecord | None:
        if run_id == self._run.run_id and turn == self._turn_number:
            return self._record
        return None


def test_audit_decisions_flags_a_model_call_mismatch_via_a_fabricated_record() -> None:
    """Defense in depth: this exact shape cannot reach a real `MatchStore` --
    see `_FixedRecordStore`'s docstring -- so this test hands `audit_decisions`
    a fabricated in-memory record directly, to prove its own mismatch check is
    correct rather than only ever exercising the trivially-true case.
    """
    run_id = "run-decisions-bad"
    turn_cycle_id = f"{run_id}-t1-a0"
    good = _make_step_bundle(run_id, turn_cycle_id, 1)

    step2_id = f"{turn_cycle_id}-step2"
    step2 = DecisionStep.model_validate(
        {
            "decision_step_id": step2_id,
            "turn_cycle_id": turn_cycle_id,
            "step_index": 2,
            "observation_id": f"{step2_id}-obs",
            "decision_id": f"{step2_id}-dec",
            "model_call_id": f"{step2_id}-call",
            "progress": "changed_state",
            "no_progress_streak_after": 0,
            "visually_degraded": False,
            "started_at": NOW,
            "ended_at": NOW,
        }
    )
    observation2 = Observation.model_validate(
        {
            "observation_id": f"{step2_id}-obs",
            "decision_step_id": step2_id,
            "assembled_at": NOW,
            "catalog_version": _catalog_ref(),
            "entries": [],
            "captures": [],
            "screen_identity": "world",
        }
    )
    # A Decision naming a model_call_id that does not belong to model_call2 --
    # legal on its own (Decision has no cross-record check), only rejected by
    # DecisionStepBundle's validator, which model_construct bypasses below.
    decision2 = Decision.model_validate(
        {
            "decision_id": f"{step2_id}-dec",
            "decision_step_id": step2_id,
            "action_declaration_id": ACTION_DECLARATION,
            "reasoning": "r",
            "trigger": "proactive",
            "model_call_id": "some-other-call-id",
            "execution": {
                "outcome": "applied",
                # FR-011: an applied execution must carry the predicate verdict that
                # confirmed it; an empty verification means nothing re-read the board.
                "verification": {"declaration_id": ACTION_DECLARATION, "result": True},
                "verified_at": NOW,
            },
        }
    )
    model_call2 = ModelCall.model_validate(
        {
            "model_call_id": f"{step2_id}-call",
            "run_id": run_id,
            "turn_cycle_id": turn_cycle_id,
            "decision_step_id": step2_id,
            "model_requested": {"provider": "openrouter", "model": "x"},
            "model_served": {"provider": "openrouter", "model": "x"},
            "latency_ms": 1,
            "cost": {},
            "retry_count": 0,
            "fallback_occurred": False,
            "image_count": 0,
            "outcome": "decision_returned",
        }
    )
    bad_bundle = DecisionStepBundle.model_construct(
        step=step2, observation=observation2, decision=decision2, model_call=model_call2
    )
    tc = _make_turn_cycle(turn_cycle_id, run_id, 1, step_count=2)
    record = TurnCycleRecord.model_construct(turn_cycle=tc, steps=[good, bad_bundle])

    fixed_store = _FixedRecordStore(_make_run(run_id), 1, record)
    report = audit.audit_decisions(fixed_store, run_id)  # type: ignore[arg-type]

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "decision_model_call_mismatch" for f in report.findings)


# --------------------------------------------------------------------------
# T140 -- audit_steps / audit_loop
# --------------------------------------------------------------------------


def test_audit_steps_passes_for_two_well_formed_turns(store: SqliteMatchStore) -> None:
    run_id = "run-steps-ok"
    turn1 = [_make_step_bundle(run_id, f"{run_id}-t1-a0", 1)]
    turn2 = [_make_step_bundle(run_id, f"{run_id}-t2-a0", 1)]
    _seed_run_with_turns(store, run_id, {1: turn1, 2: turn2})

    report = audit.audit_steps(store, run_id)

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary == {"turns_examined": 2, "steps_examined": 2}


def test_audit_steps_flags_non_contiguous_step_index(store: SqliteMatchStore) -> None:
    run_id = "run-steps-gap"
    turn_cycle_id = f"{run_id}-t1-a0"
    bundles = [
        _make_step_bundle(run_id, turn_cycle_id, 1),
        _make_step_bundle(run_id, turn_cycle_id, 2),
        _make_step_bundle(run_id, turn_cycle_id, 4),
    ]
    _seed_run_with_turns(store, run_id, {1: bundles})

    report = audit.audit_steps(store, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    finding = next(f for f in report.findings if f["kind"] == "non_contiguous_step_index")
    assert finding["step_indices"] == [1, 2, 4]


def test_audit_steps_flags_an_observation_reused_across_turns(store: SqliteMatchStore) -> None:
    run_id = "run-steps-obsreuse"
    shared_observation_id = "shared-observation"
    turn1 = [_make_step_bundle(run_id, f"{run_id}-t1-a0", 1, observation_id=shared_observation_id)]
    turn2 = [_make_step_bundle(run_id, f"{run_id}-t2-a0", 1, observation_id=shared_observation_id)]
    _seed_run_with_turns(store, run_id, {1: turn1, 2: turn2})

    report = audit.audit_steps(store, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "observation_reused" for f in report.findings)


def test_audit_steps_flags_a_capture_reused_across_turns(store: SqliteMatchStore) -> None:
    run_id = "run-steps-capreuse"
    turn1 = [_make_step_bundle(run_id, f"{run_id}-t1-a0", 1, capture_ids=["cap-shared"])]
    turn2 = [_make_step_bundle(run_id, f"{run_id}-t2-a0", 1, capture_ids=["cap-shared"])]
    _seed_run_with_turns(store, run_id, {1: turn1, 2: turn2})

    report = audit.audit_steps(store, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "capture_reused" for f in report.findings)


def test_audit_loop_passes_when_each_observation_follows_the_prior_verification(
    store: SqliteMatchStore,
) -> None:
    run_id = "run-loop-ok"
    turn_cycle_id = f"{run_id}-t5-a0"
    step1 = _make_step_bundle(
        run_id,
        turn_cycle_id,
        1,
        capture_ids=["cap-1"],
        assembled_at=NOW,
        verified_at=NOW + timedelta(seconds=1),
    )
    step2 = _make_step_bundle(
        run_id,
        turn_cycle_id,
        2,
        capture_ids=["cap-2"],
        assembled_at=NOW + timedelta(seconds=2),
        verified_at=NOW + timedelta(seconds=3),
    )
    _seed_run_with_turns(store, run_id, {5: [step1, step2]})

    report = audit.audit_loop(store, run_id, 5)

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary == {"turn": 5, "steps_examined": 2}


def test_audit_loop_flags_an_observation_assembled_before_the_prior_verification(
    store: SqliteMatchStore,
) -> None:
    run_id = "run-loop-stale"
    turn_cycle_id = f"{run_id}-t1-a0"
    step1 = _make_step_bundle(
        run_id, turn_cycle_id, 1, assembled_at=NOW, verified_at=NOW + timedelta(seconds=10)
    )
    # step2's observation was assembled *before* step1's decision was
    # verified -- exactly the stale-board failure FR-008/I14 forbid.
    step2 = _make_step_bundle(
        run_id,
        turn_cycle_id,
        2,
        assembled_at=NOW + timedelta(seconds=1),
        verified_at=NOW + timedelta(seconds=20),
    )
    _seed_run_with_turns(store, run_id, {1: [step1, step2]})

    report = audit.audit_loop(store, run_id, 1)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(
        f["kind"] == "observation_assembled_before_prior_verification" for f in report.findings
    )


def test_audit_loop_flags_a_capture_reused_within_the_same_turn(store: SqliteMatchStore) -> None:
    run_id = "run-loop-capreuse"
    turn_cycle_id = f"{run_id}-t1-a0"
    step1 = _make_step_bundle(run_id, turn_cycle_id, 1, capture_ids=["cap-x"])
    step2 = _make_step_bundle(
        run_id,
        turn_cycle_id,
        2,
        capture_ids=["cap-x"],
        assembled_at=NOW + timedelta(seconds=1),
        verified_at=NOW + timedelta(seconds=2),
    )
    _seed_run_with_turns(store, run_id, {1: [step1, step2]})

    report = audit.audit_loop(store, run_id, 1)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "capture_reused_within_turn" for f in report.findings)


# --------------------------------------------------------------------------
# T141 -- audit_capabilities
# --------------------------------------------------------------------------


def test_audit_capabilities_passes_for_firetuner_only_declarations(
    store: SqliteMatchStore, registry: CapabilityRegistry
) -> None:
    run_id = "run-caps-ok"
    _seed_run_with_turns(store, run_id, {1: [_make_step_bundle(run_id, f"{run_id}-t1-a0", 1)]})

    report = audit.audit_capabilities(store, registry, run_id)

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["bespoke_capabilities"] == 0
    assert report.summary["firetuner_capabilities"] >= 1


def test_audit_capabilities_flags_an_unresolved_declaration(
    store: SqliteMatchStore, registry: CapabilityRegistry
) -> None:
    run_id = "run-caps-unresolved"
    bundle = _make_step_bundle(
        run_id, f"{run_id}-t1-a0", 1, action_declaration=DeclarationId("made.up.action")
    )
    _seed_run_with_turns(store, run_id, {1: [bundle]})

    report = audit.audit_capabilities(store, registry, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "unresolved_capability_for_declaration" for f in report.findings)


def test_audit_capabilities_flags_a_bespoke_capability_with_no_firetuner_gap(
    store: SqliteMatchStore,
) -> None:
    """A `bespoke` capability with an empty `firetuner_gap` cannot be built
    through `load_catalog` -- `IntegrationCapability`'s own validator already
    forbids it (FR-028). This test builds one via `model_construct` to prove
    `audit_capabilities` would still catch it if it ever reached a catalog by
    some path that validator did not see -- see the module docstring.
    """
    ungapped = IntegrationCapability.model_construct(
        capability_id=CapabilityId("cap.bespoke_no_gap"),
        path=CapabilityPath.BESPOKE,
        implementation_ref="ref.lua",
        reads=[],
        writes=[],
        firetuner_gap=None,
        parity_basis=None,
    )
    declaration_id = DeclarationId("made.up.bespoke_action")
    declaration = ParityDeclaration.model_validate(
        {
            "declaration_id": declaration_id,
            "kind": "action",
            "summary": "s",
            "parity_basis": "b",
            "context": "InGame",
            "capability_id": ungapped.capability_id,
            "availability_predicate": "true",
            "verification_predicate": "true",
            "introduced_in_version": "t.1",
        }
    )
    catalog = Catalog(
        root=Path("."),
        version=CatalogVersion(version="t.1", content_hash="h", declaration_ids=[declaration_id]),
        declarations={declaration_id: declaration},
        capabilities={ungapped.capability_id: ungapped},
    )
    registry = CapabilityRegistry(catalog)

    run_id = "run-caps-bespoke"
    bundle = _make_step_bundle(run_id, f"{run_id}-t1-a0", 1, action_declaration=declaration_id)
    _seed_run_with_turns(store, run_id, {1: [bundle]})

    report = audit.audit_capabilities(store, registry, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(
        f["kind"] == "bespoke_capability_missing_firetuner_gap" for f in report.findings
    )


# --------------------------------------------------------------------------
# format_audit_report / exit_code_for
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# T160 -- audit_recovery
# --------------------------------------------------------------------------


def _event(
    run_id: str, event_id: str, event_type: str, turn: int | None, at: datetime
) -> RunEvent:
    return RunEvent.model_validate(
        {
            "event_id": event_id,
            "run_id": run_id,
            "event_type": event_type,
            "turn_number": turn,
            "occurred_at": at,
        }
    )


def test_audit_recovery_insufficient_data_for_unknown_run(store: SqliteMatchStore) -> None:
    report = audit.audit_recovery(store, "no-such-run")
    assert report.outcome is audit.AuditOutcome.INSUFFICIENT_DATA


def test_audit_recovery_passes_with_no_recovery_events(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-recov-clean"), _make_config("cfg-recov-clean"))
    report = audit.audit_recovery(store, "run-recov-clean")
    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["turn_abandoned_events"] == 0
    assert report.summary["resumed_events"] == 0


def test_audit_recovery_matches_a_healthy_abandon_resume_pair(store: SqliteMatchStore) -> None:
    run_id = "run-recov-ok"
    store.create_run(_make_run(run_id), _make_config("cfg-" + run_id))
    store.write_save_point(SavePoint.model_validate(_make_save_point(run_id, 3)))
    tc = _make_turn_cycle(f"{run_id}-t3-a0", run_id, 3, step_count=1)
    store.write_turn_cycle(
        TurnCycleRecord(turn_cycle=tc, steps=[_make_step_bundle(run_id, f"{run_id}-t3-a0", 1)])
    )
    store.write_run_event(_event(run_id, "evt-abandon", "turn_abandoned", 3, NOW))
    store.write_run_event(_event(run_id, "evt-resume", "resumed", 3, NOW + timedelta(seconds=1)))

    report = audit.audit_recovery(store, run_id)

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["matched_abandon_resume_pairs"] == 1
    assert 3 in report.summary["turns_with_abandonment"]


def test_audit_recovery_flags_a_resumed_event_with_no_prior_abandonment(
    store: SqliteMatchStore,
) -> None:
    run_id = "run-recov-orphan-resume"
    store.create_run(_make_run(run_id), _make_config("cfg-" + run_id))
    store.write_run_event(_event(run_id, "evt-resume", "resumed", 7, NOW))

    report = audit.audit_recovery(store, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "resumed_without_abandonment" for f in report.findings)


def test_audit_recovery_flags_an_abandoned_turn_with_no_record_at_all(
    store: SqliteMatchStore,
) -> None:
    run_id = "run-recov-no-record"
    store.create_run(_make_run(run_id), _make_config("cfg-" + run_id))
    store.write_run_event(_event(run_id, "evt-abandon", "turn_abandoned", 9, NOW))

    report = audit.audit_recovery(store, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "abandoned_turn_missing_from_record" for f in report.findings)


def test_audit_recovery_flags_an_abandoned_attempt_still_marked_authoritative(
    store: SqliteMatchStore,
) -> None:
    """T152's own bookkeeping (attempt_index bookkeeping, not this module's
    job) is what is supposed to demote an abandoned attempt once recovery
    replays it -- this proves the audit catches it if that demotion is ever
    skipped (invariant I9).
    """
    run_id = "run-recov-stuck"
    store.create_run(_make_run(run_id), _make_config("cfg-" + run_id))
    store.write_save_point(SavePoint.model_validate(_make_save_point(run_id, 4)))
    stuck_tc = TurnCycle.model_validate(
        {
            "turn_cycle_id": f"{run_id}-t4-a0",
            "run_id": run_id,
            "turn_number": 4,
            "attempt_index": 0,
            "is_authoritative": True,
            "save_point_id": f"{run_id}-sp4",
            "step_count": 1,
            "outcome": "abandoned",
            "final_no_progress_streak": 0,
            "visually_degraded": False,
            "started_at": NOW,
            "ended_at": NOW,
            "persisted_at": NOW,
        }
    )
    store.write_turn_cycle(
        TurnCycleRecord(
            turn_cycle=stuck_tc, steps=[_make_step_bundle(run_id, f"{run_id}-t4-a0", 1)]
        )
    )
    store.write_run_event(_event(run_id, "evt-abandon", "turn_abandoned", 4, NOW))

    report = audit.audit_recovery(store, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "abandoned_attempt_still_authoritative" for f in report.findings)


# --------------------------------------------------------------------------
# T160 -- audit_completeness
# --------------------------------------------------------------------------


def test_audit_completeness_insufficient_data_for_unknown_run(store: SqliteMatchStore) -> None:
    report = audit.audit_completeness(store, "no-such-run")
    assert report.outcome is audit.AuditOutcome.INSUFFICIENT_DATA


def test_audit_completeness_passes_for_a_gap_free_run(store: SqliteMatchStore) -> None:
    run_id = "run-complete-ok"
    turn1 = [_make_step_bundle(run_id, f"{run_id}-t1-a0", 1)]
    turn2 = [_make_step_bundle(run_id, f"{run_id}-t2-a0", 1)]
    _seed_run_with_turns(store, run_id, {1: turn1, 2: turn2})

    report = audit.audit_completeness(store, run_id)

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["turn_gaps"] == 0
    assert report.summary["step_gaps"] == 0
    assert report.summary["authoritative_turns"] == "2/2"
    assert report.summary["record_completeness_status"] == "complete"


def test_audit_completeness_flags_a_missing_turn(store: SqliteMatchStore) -> None:
    run_id = "run-complete-turngap"
    turn1 = [_make_step_bundle(run_id, f"{run_id}-t1-a0", 1)]
    turn3 = [_make_step_bundle(run_id, f"{run_id}-t3-a0", 1)]
    _seed_run_with_turns(store, run_id, {1: turn1, 3: turn3})

    report = audit.audit_completeness(store, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert {"kind": "turn_gap", "turn": 2} in report.findings
    assert report.summary["record_completeness_status"] == "has_gaps"


def test_audit_completeness_flags_a_missing_step(store: SqliteMatchStore) -> None:
    run_id = "run-complete-stepgap"
    turn_cycle_id = f"{run_id}-t1-a0"
    bundles = [
        _make_step_bundle(run_id, turn_cycle_id, 1),
        _make_step_bundle(run_id, turn_cycle_id, 2),
        _make_step_bundle(run_id, turn_cycle_id, 4),
    ]
    _seed_run_with_turns(store, run_id, {1: bundles})

    report = audit.audit_completeness(store, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    step_gap_finding = next(f for f in report.findings if f["kind"] == "step_gap")
    assert step_gap_finding["missing_step_indices"] == [3]


# --------------------------------------------------------------------------
# T176 -- audit_lineage
# --------------------------------------------------------------------------


def _make_branch_run(run_id: str, *, parent_run_id: str, parent_turn: int) -> Run:
    run = _make_run(run_id)
    return run.model_copy(update={"parent_run_id": parent_run_id, "parent_turn": parent_turn})


def _seed_parent_with_save(store: SqliteMatchStore, parent_run_id: str, turn: int) -> None:
    store.create_run(_make_run(parent_run_id), _make_config("cfg-" + parent_run_id))
    store.write_save_point(SavePoint.model_validate(_make_save_point(parent_run_id, turn)))


def test_audit_lineage_insufficient_data_for_unknown_branch(store: SqliteMatchStore) -> None:
    report = audit.audit_lineage(store, "no-such-branch")
    assert report.outcome is audit.AuditOutcome.INSUFFICIENT_DATA


def test_audit_lineage_passes_for_a_well_formed_branch(store: SqliteMatchStore) -> None:
    _seed_parent_with_save(store, "parent-a", 23)
    store.create_run(
        _make_branch_run("branch-a", parent_run_id="parent-a", parent_turn=23),
        _make_config("cfg-branch-a"),
    )
    store.write_run_event(
        RunEvent.model_validate(
            {
                "event_id": "evt-branch-a",
                "run_id": "branch-a",
                "event_type": "branch_created",
                "turn_number": 23,
                "occurred_at": NOW,
                "detail": {"parent_run_id": "parent-a", "parent_turn": 23},
            }
        )
    )

    report = audit.audit_lineage(store, "branch-a")

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["parent_run_on_record"] is True
    assert report.summary["parent_save_point_on_record"] is True
    assert report.summary["branch_created_events_matching"] == 1


def test_audit_lineage_flags_a_run_with_no_recorded_lineage(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-not-a-branch"), _make_config("cfg-not-branch"))

    report = audit.audit_lineage(store, "run-not-a-branch")

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "missing_lineage" for f in report.findings)


def test_audit_lineage_flags_a_missing_parent_run(store: SqliteMatchStore) -> None:
    store.create_run(
        _make_branch_run("branch-orphan", parent_run_id="ghost-parent", parent_turn=1),
        _make_config("cfg-branch-orphan"),
    )

    report = audit.audit_lineage(store, "branch-orphan")

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "parent_run_missing" for f in report.findings)


def test_audit_lineage_flags_a_missing_parent_save_point(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("parent-nosave"), _make_config("cfg-parent-nosave"))
    store.create_run(
        _make_branch_run("branch-nosave", parent_run_id="parent-nosave", parent_turn=5),
        _make_config("cfg-branch-nosave"),
    )

    report = audit.audit_lineage(store, "branch-nosave")

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "parent_save_point_missing" for f in report.findings)


def test_audit_lineage_flags_a_missing_branch_created_event(store: SqliteMatchStore) -> None:
    _seed_parent_with_save(store, "parent-b", 12)
    store.create_run(
        _make_branch_run("branch-b", parent_run_id="parent-b", parent_turn=12),
        _make_config("cfg-branch-b"),
    )

    report = audit.audit_lineage(store, "branch-b")

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "branch_created_event_missing_or_mismatched" for f in report.findings)


# --------------------------------------------------------------------------
# T176 -- audit_immutability
# --------------------------------------------------------------------------


def test_audit_immutability_insufficient_data_for_unknown_run(store: SqliteMatchStore) -> None:
    report = audit.audit_immutability(store, "no-such-run")
    assert report.outcome is audit.AuditOutcome.INSUFFICIENT_DATA


def test_audit_immutability_passes_for_a_well_owned_run(store: SqliteMatchStore) -> None:
    run_id = "run-immut-ok"
    _seed_run_with_turns(store, run_id, {1: [_make_step_bundle(run_id, f"{run_id}-t1-a0", 1)]})

    report = audit.audit_immutability(store, run_id)

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["is_branch"] is False
    assert report.summary["save_points_examined"] == 1


class _MismatchedOwnerStore:
    """A minimal `MatchStore` stand-in returning a save point whose own
    `run_id` disagrees with the run it was fetched for -- a shape a real,
    correctly-filtering `MatchStore.list_save_points` cannot produce (see
    `tests/unit/test_audit.py`'s own module docstring on why some tests
    reach for a fabricated store), proving `audit_immutability` would catch
    exactly this if it ever happened.
    """

    def __init__(self, run: Run, bad_save_point: SavePoint) -> None:
        self._run = run
        self._bad_save_point = bad_save_point

    def get_run(self, run_id: str) -> Run | None:
        return self._run if run_id == self._run.run_id else None

    def list_save_points(self, run_id: str) -> list[SavePoint]:
        return [self._bad_save_point] if run_id == self._run.run_id else []

    def get_turn_cycle(
        self, run_id: str, turn: int, *, authoritative_only: bool = True
    ) -> TurnCycleRecord | None:
        return None


def test_audit_immutability_flags_a_save_point_owned_by_another_run() -> None:
    run = _make_run("run-immut-bad")
    bad_save_point = SavePoint.model_validate(_make_save_point("some-other-run", 1))
    fake_store = _MismatchedOwnerStore(run, bad_save_point)

    report = audit.audit_immutability(fake_store, "run-immut-bad")  # type: ignore[arg-type]

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "save_point_owned_by_another_run" for f in report.findings)


# --------------------------------------------------------------------------
# T176 -- audit_builds
# --------------------------------------------------------------------------


def _seed_set(**overrides: Any) -> SeedSet:
    payload: dict[str, Any] = {
        "seed_set_id": SeedSetId("shuffle-classic-2026q3"),
        "name": "shuffle-classic-2026q3",
        "seeds": ["123"],
        "civilization": "CIVILIZATION_ROME",
        "leader": "LEADER_TRAJAN",
        "ruleset": "RULESET_STANDARD",
        "mod_set": [],
        "game_build": "win/1.0.12.9",
        "accepted_build_changes": [],
        "created_at": NOW,
    }
    payload.update(overrides)
    return SeedSet.model_validate(payload)


def test_audit_builds_reports_a_uniform_set_with_no_acceptances(store: SqliteMatchStore) -> None:
    report = audit.audit_builds(store, _seed_set())

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["is_uniform"] is True
    assert report.summary["run_enumeration_supported"] is False


def test_audit_builds_reports_a_non_uniform_set_once_any_change_is_accepted(
    store: SqliteMatchStore,
) -> None:
    acceptance = {
        "acceptance_id": AcceptanceId("acc-1"),
        "from_build": "win/1.0.12.9",
        "to_build": "win/1.0.12.11",
        "accepted_by": "researcher",
        "accepted_at": NOW,
        "reason": "matchmaking-only patch",
        "r20_spike_ref": None,
    }
    seed_set = _seed_set(accepted_build_changes=[acceptance])

    report = audit.audit_builds(store, seed_set)

    assert report.outcome is audit.AuditOutcome.PASSED  # a version-only acceptance needs no spike
    assert report.summary["is_uniform"] is False
    assert report.summary["accepted_transitions"][0]["to_build"] == "win/1.0.12.11"


def test_audit_builds_flags_a_platform_crossing_acceptance_with_no_spike_ref(
    store: SqliteMatchStore,
) -> None:
    acceptance = {
        "acceptance_id": AcceptanceId("acc-2"),
        "from_build": "win/1.0.12.9",
        "to_build": "mac/1.0.12.9",
        "accepted_by": "researcher",
        "accepted_at": NOW,
        "reason": "testing cross-platform",
        "r20_spike_ref": None,
    }
    seed_set = _seed_set(accepted_build_changes=[acceptance])

    report = audit.audit_builds(store, seed_set)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(
        f["kind"] == "platform_transition_acceptance_missing_spike_ref" for f in report.findings
    )


def test_audit_builds_partitions_supplied_runs_by_build(store: SqliteMatchStore) -> None:
    store.create_run(_make_run("run-win-a"), _make_config("cfg-run-win-a"))
    accepted_run = _make_run("run-win-b").model_copy(update={"game_build": "win/1.0.12.11"})
    store.create_run(accepted_run, _make_config("cfg-run-win-b"))
    acceptance = {
        "acceptance_id": AcceptanceId("acc-3"),
        "from_build": "win/1.0.12.9",
        "to_build": "win/1.0.12.11",
        "accepted_by": "researcher",
        "accepted_at": NOW,
        "reason": "patch",
        "r20_spike_ref": None,
    }
    seed_set = _seed_set(accepted_build_changes=[acceptance])

    report = audit.audit_builds(store, seed_set, run_ids=["run-win-a", "run-win-b"])

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["run_enumeration_supported"] is True
    assert set(report.summary["builds_seen"]["win/1.0.12.9"]) == {"run-win-a"}
    assert set(report.summary["builds_seen"]["win/1.0.12.11"]) == {"run-win-b"}


def test_audit_builds_flags_a_run_build_nothing_ever_accepted(store: SqliteMatchStore) -> None:
    drifted = _make_run("run-drift").model_copy(update={"game_build": "win/9.9.9.9"})
    store.create_run(drifted, _make_config("cfg-run-drift"))

    report = audit.audit_builds(store, _seed_set(), run_ids=["run-drift"])

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "run_build_not_accounted_for" for f in report.findings)


# --------------------------------------------------------------------------
# T190 -- audit_models
# --------------------------------------------------------------------------


def test_audit_models_insufficient_data_when_run_has_no_turns(
    store: SqliteMatchStore,
) -> None:
    store.create_run(_make_run("run-models-empty"), _make_config("cfg-models-empty"))
    report = audit.audit_models(store, "run-models-empty")
    assert report.outcome is audit.AuditOutcome.INSUFFICIENT_DATA


def test_audit_models_rolls_up_latency_retry_cost_and_fallback_per_turn(
    store: SqliteMatchStore,
) -> None:
    run_id = "run-models-ok"
    turn_cycle_id = f"{run_id}-t1-a0"
    step1 = _make_step_bundle(run_id, turn_cycle_id, 1)
    step2 = _make_step_bundle(run_id, turn_cycle_id, 2)
    fallback_call = step2.model_call.model_copy(
        update={"fallback_occurred": True, "latency_ms": 250, "retry_count": 2}
    )
    step2 = step2.model_copy(update={"model_call": fallback_call})
    _seed_run_with_turns(store, run_id, {1: [step1, step2]})

    report = audit.audit_models(store, run_id)

    assert report.outcome is audit.AuditOutcome.PASSED
    per_turn = report.summary["per_turn"][1]
    assert per_turn["latency_ms_total"] == 100 + 250
    assert per_turn["retry_count_total"] == 0 + 2
    assert per_turn["fallback_occurred"] is True
    assert per_turn["models_served"] == ["openrouter/x"]
    assert report.summary["turns_served_by_fallback"] == 1


# --------------------------------------------------------------------------
# T190 -- audit_secrets
# --------------------------------------------------------------------------


def test_audit_secrets_insufficient_data_for_unknown_run(store: SqliteMatchStore) -> None:
    report = audit.audit_secrets(store, "no-such-run")
    assert report.outcome is audit.AuditOutcome.INSUFFICIENT_DATA


def test_audit_secrets_passes_for_a_clean_run(store: SqliteMatchStore) -> None:
    run_id = "run-secrets-clean"
    _seed_run_with_turns(store, run_id, {1: [_make_step_bundle(run_id, f"{run_id}-t1-a0", 1)]})

    report = audit.audit_secrets(store, run_id)

    assert report.outcome is audit.AuditOutcome.PASSED
    assert report.summary["records_examined"] > 0


def test_audit_secrets_flags_a_fake_planted_key_shaped_value(store: SqliteMatchStore) -> None:
    """Only a fake, obviously-not-real key shape is ever planted here -- see
    `operator/audit.py`'s own module docstring and this feature's credential
    handling notes; a real key must never appear in test data.
    """
    run_id = "run-secrets-planted"
    bundle = _make_step_bundle(run_id, f"{run_id}-t1-a0", 1)
    leaky_decision = bundle.decision.model_copy(
        update={"reasoning": "remember api_key: sk-FAKE1234567890abcdefFAKE for later"}
    )
    bundle = bundle.model_copy(update={"decision": leaky_decision})
    _seed_run_with_turns(store, run_id, {1: [bundle]})

    report = audit.audit_secrets(store, run_id)

    assert report.outcome is audit.AuditOutcome.FAILED
    assert any(f["kind"] == "credential_shaped_value_in_decision" for f in report.findings)


def test_format_and_exit_code_distinguish_all_three_outcomes() -> None:
    passed = audit.AuditReport(outcome=audit.AuditOutcome.PASSED, summary={"n": 1})
    failed = audit.AuditReport(
        outcome=audit.AuditOutcome.FAILED, findings=[{"kind": "x"}], summary={"n": 1}
    )
    insufficient = audit.AuditReport(outcome=audit.AuditOutcome.INSUFFICIENT_DATA, reason="why")

    assert audit.exit_code_for(passed) == 0
    assert audit.exit_code_for(failed) == 1
    assert audit.exit_code_for(insufficient) == 2

    assert "findings: none" in audit.format_audit_report("x", passed)
    assert "findings (1)" in audit.format_audit_report("x", failed)
    assert "reason: why" in audit.format_audit_report("x", insufficient)
