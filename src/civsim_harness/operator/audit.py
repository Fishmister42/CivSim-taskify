"""`civsim audit parity|prompts|decisions|steps|loop|capabilities` (T138-T141).

Every audit here works **from the run's record alone**, through
`civsim_harness.store.port.MatchStore` and a loaded
`civsim_harness.capability.registry.CapabilityRegistry` -- that is the entire
point (quickstart.md Scenario 3: "working only from a completed run's record").
None of these ever connect to a live game client, replay a turn, or re-derive
anything the harness did not already write down.

**"No findings" is not the same outcome as "could not run," and this module
keeps them structurally distinct** (`AuditOutcome.PASSED` /
`AuditOutcome.FAILED` / `AuditOutcome.INSUFFICIENT_DATA`) rather than
overloading an empty findings list to mean both "verified clean" and "nothing
to look at." `INSUFFICIENT_DATA` fires for exactly two conditions: the named
`run_id` is not on record at all, or the run is on record but has not yet
produced a single turn to examine (no save points, hence no discoverable turn
range -- see `_discover_turns`). Anything else that actually ran and found no
problems is `PASSED`.

**A genuine `MatchStore`-port gap, worked around the way this codebase already
works around it.** `MatchStore` (store/port.py) has no read accessor for an
individual `ScreenCapture` by id, and none for a run's `RunEvent` timeline --
both are write-only in the port's public surface (the same gap
`tests/contract/test_match_store_port.py`'s own `_run_archived_event_types`
helper documents and reaches past the port for, "the port has no read op for
events"). `audit_parity` and `audit_capabilities` need the former (a
capture's `view_declaration_id`) to audit visual observations; `audit_prompts`
needs the latter (`unknown_screen` events, i.e. recorded stalls) for FR-005's
"or stall the run visibly" half. `_capture_reader` / `_event_reader` below
resolve this the same way: prefer a duck-typed `get_capture` /
`list_run_events` method if a store happens to expose one, otherwise fall back
to reading `SqliteMatchStore`'s own `_conn` directly (the only adapter this
codebase has today). When neither is available, the affected audit degrades
honestly -- it reports the limitation in its summary and, for `audit_parity`,
raises a distinct `visual_capture_unverifiable` finding per affected capture
rather than silently claiming a visual observation resolved when it was never
actually checked.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError
from civsim_harness.models.catalog import CapabilityPath
from civsim_harness.models.common import CaptureId, DeclarationId, RunId, Timestamp
from civsim_harness.models.decision import DecisionTrigger
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.turn import ScreenCapture
from civsim_harness.store.port import MatchStore, TurnCycleRecord

__all__ = [
    "AuditOutcome",
    "AuditReport",
    "audit_capabilities",
    "audit_decisions",
    "audit_loop",
    "audit_parity",
    "audit_prompts",
    "audit_steps",
    "exit_code_for",
    "format_audit_report",
]


class AuditOutcome(StrEnum):
    """The three outcomes an audit may report -- see the module docstring for
    why "ran clean" and "could not run" are never conflated.
    """

    PASSED = "passed"
    FAILED = "failed"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class AuditReport:
    """One audit's result: an outcome, its findings (empty unless `FAILED`),
    a human-readable `reason` (populated only for `INSUFFICIENT_DATA`), and a
    `summary` of what was actually examined (counts, flags for any degraded
    check) so a "0 findings" result is legible rather than suspicious.
    """

    outcome: AuditOutcome
    findings: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.outcome is AuditOutcome.PASSED


def exit_code_for(report: AuditReport) -> int:
    """CLI exit code: 0 clean, 1 findings, 2 could not run -- distinguishable
    by automation, not just by reading the text.
    """
    return {
        AuditOutcome.PASSED: 0,
        AuditOutcome.FAILED: 1,
        AuditOutcome.INSUFFICIENT_DATA: 2,
    }[report.outcome]


def format_audit_report(name: str, report: AuditReport) -> str:
    lines = [f"audit {name}: {report.outcome.value}"]
    if report.outcome is AuditOutcome.INSUFFICIENT_DATA:
        lines.append(f"  reason: {report.reason}")
        return "\n".join(lines)
    for key, value in report.summary.items():
        lines.append(f"  {key}: {value}")
    if report.findings:
        lines.append(f"  findings ({len(report.findings)}):")
        for finding in report.findings:
            lines.append(f"    - {finding}")
    else:
        lines.append("  findings: none")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Shared record-walking helpers
# --------------------------------------------------------------------------


def _discover_turns(store: MatchStore, run_id: RunId) -> list[int]:
    """The turn numbers this run has ever attempted, from its save points.

    `MatchStore` has no "highest recorded turn" or "list turns" accessor, but
    FR-007 guarantees a quicksave at the start of every turn attempt -- so the
    distinct `turn_number`s across `list_save_points` are exactly the turns
    worth asking `get_turn_cycle` about. An empty result means this run has
    not produced a single turn yet, which the caller treats as
    `INSUFFICIENT_DATA` rather than a vacuous pass.
    """
    return sorted({save.turn_number for save in store.list_save_points(run_id)})


def _iter_authoritative_records(
    store: MatchStore, run_id: RunId
) -> Iterator[tuple[int, TurnCycleRecord]]:
    """Yield `(turn_number, record)` for every turn with an authoritative
    attempt on record, skipping gaps -- completeness auditing (turn/step gaps)
    is a different audit's job, not this module's.
    """
    for turn in _discover_turns(store, run_id):
        record = store.get_turn_cycle(run_id, turn, authoritative_only=True)
        if record is not None:
            yield turn, record


def _capture_reader(store: MatchStore) -> Callable[[CaptureId], ScreenCapture | None] | None:
    """A best-effort `CaptureId -> ScreenCapture | None` reader, or `None` if
    *store* offers no way to answer that question -- see the module docstring.
    """
    get_capture = getattr(store, "get_capture", None)
    if callable(get_capture):
        return get_capture  # type: ignore[no-any-return]

    maybe_conn = getattr(store, "_conn", None)
    if not isinstance(maybe_conn, sqlite3.Connection):
        return None
    sqlite_conn: sqlite3.Connection = maybe_conn

    def _read(capture_id: CaptureId) -> ScreenCapture | None:
        row = sqlite_conn.execute(
            "SELECT capture_json FROM captures WHERE capture_id = ?", (str(capture_id),)
        ).fetchone()
        return ScreenCapture.model_validate_json(row[0]) if row is not None else None

    return _read


def _event_reader(store: MatchStore) -> Callable[[RunId], list[RunEvent]] | None:
    """A best-effort `RunId -> list[RunEvent]` reader, or `None` -- see the
    module docstring; same gap and same fallback shape as `_capture_reader`.
    """
    list_events = getattr(store, "list_run_events", None)
    if callable(list_events):
        return list_events  # type: ignore[no-any-return]

    maybe_conn = getattr(store, "_conn", None)
    if not isinstance(maybe_conn, sqlite3.Connection):
        return None
    sqlite_conn: sqlite3.Connection = maybe_conn

    def _read(run_id: RunId) -> list[RunEvent]:
        rows = sqlite_conn.execute(
            "SELECT event_json FROM run_events WHERE run_id = ? ORDER BY occurred_at",
            (str(run_id),),
        ).fetchall()
        return [RunEvent.model_validate_json(row[0]) for row in rows]

    return _read


def _resolves(registry: CapabilityRegistry, declaration_id: DeclarationId) -> bool:
    try:
        registry.resolve(declaration_id)
    except CatalogError:
        return False
    return True


def _insufficient_data(reason: str) -> AuditReport:
    return AuditReport(outcome=AuditOutcome.INSUFFICIENT_DATA, reason=reason)


def _preconditions(store: MatchStore, run_id: RunId) -> AuditReport | list[int]:
    """Shared precondition check for every per-run audit below: the run must
    exist and must have at least one discoverable turn. Returns the turn list
    on success, or the `INSUFFICIENT_DATA` report to return immediately.
    """
    if store.get_run(run_id) is None:
        return _insufficient_data(f"run {run_id!r} is not on record in this store")
    turns = _discover_turns(store, run_id)
    if not turns:
        return _insufficient_data(f"run {run_id!r} has no recorded turns yet")
    return turns


# --------------------------------------------------------------------------
# T138 -- civsim audit parity <run_id>
# --------------------------------------------------------------------------


def audit_parity(store: MatchStore, registry: CapabilityRegistry, run_id: RunId) -> AuditReport:
    """Enumerate every distinct observation and action this run used --
    structured and visual, across every decision step -- and resolve each to
    its parity declaration (SC-006, SC-007).
    """
    pre = _preconditions(store, run_id)
    if isinstance(pre, AuditReport):
        return pre

    run = store.get_run(run_id)
    assert run is not None  # narrowed by _preconditions
    capture_reader = _capture_reader(store)

    findings: list[dict[str, Any]] = []
    observation_declarations: set[str] = set()
    view_declarations: set[str] = set()
    action_declarations: set[str] = set()
    turns_examined = 0
    steps_examined = 0

    for turn, record in _iter_authoritative_records(store, run_id):
        turns_examined += 1
        for bundle in record.steps:
            steps_examined += 1
            step_index = bundle.step.step_index
            observation = bundle.observation

            for entry in observation.entries:
                observation_declarations.add(str(entry.declaration_id))
                if not _resolves(registry, entry.declaration_id):
                    findings.append(
                        {
                            "kind": "unresolved_observation",
                            "turn": turn,
                            "step_index": step_index,
                            "declaration_id": str(entry.declaration_id),
                        }
                    )

            for capture_id in observation.captures:
                if capture_reader is None:
                    findings.append(
                        {
                            "kind": "visual_capture_unverifiable",
                            "turn": turn,
                            "step_index": step_index,
                            "capture_id": str(capture_id),
                            "reason": "store exposes no capture-read accessor",
                        }
                    )
                    continue
                capture = capture_reader(capture_id)
                if capture is None:
                    findings.append(
                        {
                            "kind": "dangling_capture_reference",
                            "turn": turn,
                            "step_index": step_index,
                            "capture_id": str(capture_id),
                        }
                    )
                    continue
                view_declarations.add(str(capture.view_declaration_id))
                if not _resolves(registry, capture.view_declaration_id):
                    findings.append(
                        {
                            "kind": "unresolved_view",
                            "turn": turn,
                            "step_index": step_index,
                            "declaration_id": str(capture.view_declaration_id),
                        }
                    )

            action_declarations.add(str(bundle.decision.action_declaration_id))
            if not _resolves(registry, bundle.decision.action_declaration_id):
                findings.append(
                    {
                        "kind": "unresolved_action",
                        "turn": turn,
                        "step_index": step_index,
                        "declaration_id": str(bundle.decision.action_declaration_id),
                    }
                )

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {
        "turns_examined": turns_examined,
        "steps_examined": steps_examined,
        "distinct_observation_declarations": len(observation_declarations),
        "distinct_view_declarations": len(view_declarations),
        "distinct_action_declarations": len(action_declarations),
        "observation_catalog_version": run.observation_catalog_version.version,
        "action_catalog_version": run.action_catalog_version.version,
        "visual_capture_lookup_supported": capture_reader is not None,
    }
    return AuditReport(outcome=outcome, findings=findings, summary=summary)


# --------------------------------------------------------------------------
# T139 -- civsim audit prompts <run_id> / audit decisions <run_id>
# --------------------------------------------------------------------------

def audit_prompts(store: MatchStore, run_id: RunId) -> AuditReport:
    """Every prompt encountered is either a recorded `prompt_response`
    decision or a recorded stall (SC-005).

    From the record alone there is no independent ground truth for "how many
    prompts the game actually presented" -- only what the harness recorded.
    This audit therefore surfaces every `prompt_response` decision and every
    recorded stall (`unknown_screen` event) for review, and raises a finding
    only for a structural anomaly it *can* detect: a `prompt_response`
    decision missing its `prompt_type` cannot actually be constructed
    (`models.decision.Decision` enforces this), so a finding here would mean
    the store returned data pydantic's own validation should have rejected --
    worth surfacing loudly rather than silently trusting.
    """
    pre = _preconditions(store, run_id)
    if isinstance(pre, AuditReport):
        return pre

    findings: list[dict[str, Any]] = []
    prompt_decisions = 0
    for turn, record in _iter_authoritative_records(store, run_id):
        for bundle in record.steps:
            decision = bundle.decision
            if decision.trigger != DecisionTrigger.PROMPT_RESPONSE:
                continue
            prompt_decisions += 1
            if not decision.prompt_type:
                findings.append(
                    {
                        "kind": "prompt_response_missing_prompt_type",
                        "turn": turn,
                        "step_index": bundle.step.step_index,
                        "decision_id": str(decision.decision_id),
                    }
                )

    event_reader = _event_reader(store)
    stall_count: int | None = None
    if event_reader is not None:
        stall_count = sum(
            1 for event in event_reader(run_id) if event.event_type == RunEventType.UNKNOWN_SCREEN
        )

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {
        "prompt_response_decisions": prompt_decisions,
        "recorded_stalls": stall_count,
        "stall_visibility_supported": event_reader is not None,
    }
    return AuditReport(outcome=outcome, findings=findings, summary=summary)


def audit_decisions(store: MatchStore, run_id: RunId) -> AuditReport:
    """Zero decisions exist without a `model_call_id` (SC-012, invariant I5).

    `models.decision.Decision.model_call_id` is a required field and
    `store.port.DecisionStepBundle` cross-checks it against the paired
    `ModelCall` at construction time, so a store round-tripping correctly can
    never actually produce a violation here. This audit re-asserts it anyway,
    reading the record the way an external auditor would rather than trusting
    that every write path upheld the invariant -- exactly the point of an
    audit that works "from the record alone."
    """
    pre = _preconditions(store, run_id)
    if isinstance(pre, AuditReport):
        return pre

    findings: list[dict[str, Any]] = []
    decisions_examined = 0
    for turn, record in _iter_authoritative_records(store, run_id):
        for bundle in record.steps:
            decisions_examined += 1
            decision = bundle.decision
            if not decision.model_call_id:
                findings.append(
                    {
                        "kind": "decision_missing_model_call_id",
                        "turn": turn,
                        "step_index": bundle.step.step_index,
                        "decision_id": str(decision.decision_id),
                    }
                )
            elif decision.model_call_id != bundle.model_call.model_call_id:
                findings.append(
                    {
                        "kind": "decision_model_call_mismatch",
                        "turn": turn,
                        "step_index": bundle.step.step_index,
                        "decision_id": str(decision.decision_id),
                        "decision_model_call_id": str(decision.model_call_id),
                        "bundle_model_call_id": str(bundle.model_call.model_call_id),
                    }
                )

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    return AuditReport(
        outcome=outcome, findings=findings, summary={"decisions_examined": decisions_examined}
    )


# --------------------------------------------------------------------------
# T140 -- civsim audit steps <run_id> / audit loop <run_id> --turn N
# --------------------------------------------------------------------------


def audit_steps(store: MatchStore, run_id: RunId) -> AuditReport:
    """Exactly one decision, one model call, and one observation per step;
    contiguous `step_index` from 1; and no observation or capture reused
    across steps *anywhere in the run* (SC-003, invariants I13, I14).
    """
    pre = _preconditions(store, run_id)
    if isinstance(pre, AuditReport):
        return pre

    findings: list[dict[str, Any]] = []
    seen_observations: dict[str, tuple[int, int]] = {}
    seen_captures: dict[str, tuple[int, int]] = {}
    turns_examined = 0
    steps_examined = 0

    for turn, record in _iter_authoritative_records(store, run_id):
        turns_examined += 1
        indices = [bundle.step.step_index for bundle in record.steps]
        if indices != list(range(1, len(indices) + 1)):
            findings.append(
                {"kind": "non_contiguous_step_index", "turn": turn, "step_indices": indices}
            )

        for bundle in record.steps:
            steps_examined += 1
            here = (turn, bundle.step.step_index)

            observation_id = str(bundle.observation.observation_id)
            first_seen = seen_observations.get(observation_id)
            if first_seen is not None:
                findings.append(
                    {
                        "kind": "observation_reused",
                        "observation_id": observation_id,
                        "first_seen_at": first_seen,
                        "reused_at": here,
                    }
                )
            else:
                seen_observations[observation_id] = here

            for capture_id in bundle.observation.captures:
                capture_key = str(capture_id)
                first_capture_seen = seen_captures.get(capture_key)
                if first_capture_seen is not None:
                    findings.append(
                        {
                            "kind": "capture_reused",
                            "capture_id": capture_key,
                            "first_seen_at": first_capture_seen,
                            "reused_at": here,
                        }
                    )
                else:
                    seen_captures[capture_key] = here

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {"turns_examined": turns_examined, "steps_examined": steps_examined}
    return AuditReport(outcome=outcome, findings=findings, summary=summary)


def audit_loop(store: MatchStore, run_id: RunId, turn: int) -> AuditReport:
    """Scenario 8a: for turn *turn*, each step's observation was assembled
    strictly after the prior step's decision was verified, with a distinct
    capture per step and exactly one model call per step (invariants I14, I15).
    """
    if store.get_run(run_id) is None:
        return _insufficient_data(f"run {run_id!r} is not on record in this store")

    record = store.get_turn_cycle(run_id, turn, authoritative_only=True)
    if record is None:
        return _insufficient_data(
            f"no authoritative turn cycle is recorded for run {run_id!r} turn {turn}"
        )

    findings: list[dict[str, Any]] = []
    steps = record.steps  # already strictly ascending by step_index (TurnCycleRecord validator)

    indices = [bundle.step.step_index for bundle in steps]
    if indices != list(range(1, len(indices) + 1)):
        findings.append(
            {"kind": "non_contiguous_step_index", "turn": turn, "step_indices": indices}
        )

    seen_captures: set[str] = set()
    previous_verified_at: Timestamp | None = None
    for bundle in steps:
        prior = previous_verified_at
        if prior is not None and bundle.observation.assembled_at < prior:
            findings.append(
                {
                    "kind": "observation_assembled_before_prior_verification",
                    "turn": turn,
                    "step_index": bundle.step.step_index,
                    "observation_assembled_at": bundle.observation.assembled_at.isoformat(),
                    "prior_decision_verified_at": prior.isoformat(),
                }
            )
        previous_verified_at = bundle.decision.execution.verified_at

        for capture_id in bundle.observation.captures:
            capture_key = str(capture_id)
            if capture_key in seen_captures:
                findings.append(
                    {
                        "kind": "capture_reused_within_turn",
                        "turn": turn,
                        "step_index": bundle.step.step_index,
                        "capture_id": capture_key,
                    }
                )
            else:
                seen_captures.add(capture_key)

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {"turn": turn, "steps_examined": len(steps)}
    return AuditReport(outcome=outcome, findings=findings, summary=summary)


# --------------------------------------------------------------------------
# T141 -- civsim audit capabilities <run_id>
# --------------------------------------------------------------------------


def audit_capabilities(
    store: MatchStore, registry: CapabilityRegistry, run_id: RunId
) -> AuditReport:
    """Every capability this run used is `path: firetuner`, or carries a
    non-empty `firetuner_gap` (FR-028, SC-020).

    `IntegrationCapability`'s own model validator already forbids a `bespoke`
    capability with no gap statement (and a `firetuner` one with a stray one)
    at catalog-*load* time, so this can only ever fail here if the loaded
    catalog and this run's actually-used declarations disagree in a way the
    validator never saw -- e.g. a declaration this run used no longer resolves
    in the catalog on disk. Both that case (an unresolved declaration) and the
    redundant-but-cheap re-assertion of the gap rule itself are checked below,
    so a caller gets the same "from the record alone" guarantee `audit_parity`
    gives for observations and actions, but for capabilities.
    """
    pre = _preconditions(store, run_id)
    if isinstance(pre, AuditReport):
        return pre

    capture_reader = _capture_reader(store)
    findings: list[dict[str, Any]] = []
    declarations_examined = 0
    capabilities_used: dict[str, CapabilityPath] = {}
    ungapped_bespoke: set[str] = set()

    def _check(declaration_id: DeclarationId, *, turn: int, step_index: int, origin: str) -> None:
        nonlocal declarations_examined
        declarations_examined += 1
        try:
            capability = registry.capability_for(declaration_id)
        except CatalogError:
            findings.append(
                {
                    "kind": "unresolved_capability_for_declaration",
                    "turn": turn,
                    "step_index": step_index,
                    "origin": origin,
                    "declaration_id": str(declaration_id),
                }
            )
            return
        capabilities_used[str(capability.capability_id)] = capability.path
        has_gap = bool((capability.firetuner_gap or "").strip())
        if capability.path != CapabilityPath.FIRETUNER and not has_gap:
            ungapped_bespoke.add(str(capability.capability_id))

    for turn, record in _iter_authoritative_records(store, run_id):
        for bundle in record.steps:
            step_index = bundle.step.step_index
            for entry in bundle.observation.entries:
                _check(entry.declaration_id, turn=turn, step_index=step_index, origin="observation")
            for capture_id in bundle.observation.captures:
                if capture_reader is None:
                    continue
                capture = capture_reader(capture_id)
                if capture is not None:
                    _check(
                        capture.view_declaration_id,
                        turn=turn,
                        step_index=step_index,
                        origin="view",
                    )
            _check(
                bundle.decision.action_declaration_id,
                turn=turn,
                step_index=step_index,
                origin="action",
            )

    for capability_id in sorted(ungapped_bespoke):
        findings.append(
            {"kind": "bespoke_capability_missing_firetuner_gap", "capability_id": capability_id}
        )

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {
        "declarations_examined": declarations_examined,
        "capabilities_used": len(capabilities_used),
        "firetuner_capabilities": sum(
            1 for path in capabilities_used.values() if path == CapabilityPath.FIRETUNER
        ),
        "bespoke_capabilities": sum(
            1 for path in capabilities_used.values() if path != CapabilityPath.FIRETUNER
        ),
        "visual_capture_lookup_supported": capture_reader is not None,
    }
    return AuditReport(outcome=outcome, findings=findings, summary=summary)
