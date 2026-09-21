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

**`MatchStore` now exposes `get_capture` and `list_run_events` directly.**
Earlier waves of this module reached past the port into
`SqliteMatchStore`'s own `_conn` when a store did not (yet) expose these as
first-class reads. The port (`store/port.py`) now names both as required
members of the `MatchStore` Protocol, so `_capture_reader` / `_event_reader`
below only ever prefer a store's own `get_capture` / `list_run_events` --
there is no `_conn` fallback left to reach past the port with. A store that
still does not expose them (e.g. a deliberately narrowed test double) is
handled the same as ever: the affected audit degrades honestly, reporting
the limitation in its summary and, for `audit_parity`, raising a distinct
`visual_capture_unverifiable` finding per affected capture rather than
silently claiming a visual observation resolved when it was never actually
checked.

**Audits added since the module above (T160, T176, T190) work from records
the five audits above never touch** -- `RunEvent` timelines, save-point
lineage, and per-call model accounting -- and each documents its own
precondition and coverage. Two are worth flagging up front:

- `audit_builds` (T176) cannot enumerate *which runs used a seed set* from
  `MatchStore` alone: the port has no read accessor from a `SeedSet` (or its
  `seed_set_id`) to the runs whose `RunConfiguration` named it, and no read
  accessor for `RunConfiguration` at all once a run exists. It therefore
  accepts an optional, caller-supplied `run_ids` sequence (empty by default,
  since `operator/cli.py` has no source for one today) and reports the gap
  honestly via `run_enumeration_supported` rather than fabricating a
  partition of runs it never actually looked up.
- `audit_secrets` (T190) covers every record and capture reachable through
  `MatchStore` for one run -- not "logs" in the sense of a process's stdout
  or a log file, which this store-only module has no access to at all;
  log-line redaction is `telemetry/redaction.py`'s own, structurally
  separate guarantee (see that module's docstring), not something a
  completed run's *store* record can attest to after the fact.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError
from civsim_harness.models.catalog import CapabilityPath
from civsim_harness.models.common import CaptureId, DeclarationId, RunId, Timestamp
from civsim_harness.models.config import SeedSet
from civsim_harness.models.decision import DecisionTrigger
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.turn import ScreenCapture
from civsim_harness.observe.game_build import is_platform_transition
from civsim_harness.saves.addressing import find_save_point
from civsim_harness.store.completeness import first_owed_turn, record_completeness_status
from civsim_harness.store.port import MatchStore, TurnCycleRecord
from civsim_harness.telemetry.redaction import redact_value

__all__ = [
    "AuditOutcome",
    "AuditReport",
    "audit_builds",
    "audit_capabilities",
    "audit_completeness",
    "audit_decisions",
    "audit_immutability",
    "audit_lineage",
    "audit_loop",
    "audit_models",
    "audit_parity",
    "audit_prompts",
    "audit_recovery",
    "audit_secrets",
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
    return None


def _event_reader(store: MatchStore) -> Callable[[RunId], list[RunEvent]] | None:
    """A best-effort `RunId -> list[RunEvent]` reader, or `None` -- see the
    module docstring; same gap-handling shape as `_capture_reader`.
    """
    list_events = getattr(store, "list_run_events", None)
    if callable(list_events):
        return list_events  # type: ignore[no-any-return]
    return None


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


# --------------------------------------------------------------------------
# Shared precondition for the run-timeline audits below (T160, T190):
# unlike audit_parity/prompts/decisions/steps/capabilities, these work
# primarily from RunEvent timelines and per-call accounting rather than from
# observations/actions a turn used -- a run legitimately has zero turns yet
# (still preparing, or failed before turn 1) and that is not "insufficient
# data" for a timeline audit the way it is for those five. INSUFFICIENT_DATA
# here fires on exactly one condition: the run itself is not on record.
# --------------------------------------------------------------------------


def _run_precondition(store: MatchStore, run_id: RunId) -> AuditReport | None:
    if store.get_run(run_id) is None:
        return _insufficient_data(f"run {run_id!r} is not on record in this store")
    return None


# --------------------------------------------------------------------------
# T160 -- civsim audit recovery <run_id>
# --------------------------------------------------------------------------

#: The four `resilience.detector` signals plus the mid-turn observation
#: failure (T153) -- every trigger that can lead into `RecoveryEngine`'s
#: abandon-and-replay path (`resilience/recovery.py`).
_RECOVERY_TRIGGER_EVENT_TYPES = frozenset(
    {
        RunEventType.CRASH_DETECTED,
        RunEventType.HANG_DETECTED,
        RunEventType.UNRESPONSIVE_DETECTED,
        RunEventType.UNKNOWN_SCREEN,
        RunEventType.OBSERVATION_ASSEMBLY_FAILED,
    }
)


def audit_recovery(store: MatchStore, run_id: RunId) -> AuditReport:
    """`civsim audit recovery <run_id>` (T160): crash/resume event pairs, and
    abandoned-vs-authoritative attempts per turn (SC-003, SC-011, SC-021).

    Reads `run_id`'s full `RunEvent` timeline and pairs each `resumed` event
    with the most recent `turn_abandoned` event at the same `turn_number`
    that precedes it -- FR-047's "both the abandonment and the resumption
    are recorded" -- flagging a `resumed` with no matching abandonment as
    `resumed_without_abandonment`. For every turn with a `turn_abandoned` on
    record, it also cross-checks the abandoned attempt against the turn's
    current authoritative attempt (`MatchStore.get_turn_cycle`, both
    `authoritative_only` variants): the abandoned attempt must still be
    retrievable (`abandoned_turn_missing_from_record` if not -- T152's "the
    abandoned attempt is retained with all the steps it completed"), and
    once a later attempt exists it must no longer be the authoritative one
    (`abandoned_attempt_still_authoritative` if it is -- invariant I9,
    "exactly one authoritative attempt per (run_id, turn_number)").
    """
    insufficient = _run_precondition(store, run_id)
    if insufficient is not None:
        return insufficient

    events = store.list_run_events(run_id)
    abandoned = [e for e in events if e.event_type == RunEventType.TURN_ABANDONED]
    resumed = [e for e in events if e.event_type == RunEventType.RESUMED]
    limit_reached = [e for e in events if e.event_type == RunEventType.RECOVERY_LIMIT_REACHED]

    findings: list[dict[str, Any]] = []
    matched_pairs = 0
    for r in resumed:
        candidates = [
            a
            for a in abandoned
            if a.turn_number == r.turn_number and a.occurred_at <= r.occurred_at
        ]
        if not candidates:
            findings.append(
                {
                    "kind": "resumed_without_abandonment",
                    "turn": r.turn_number,
                    "event_id": str(r.event_id),
                }
            )
        else:
            matched_pairs += 1

    per_turn: dict[int, dict[str, Any]] = {}
    abandoned_turns = sorted({a.turn_number for a in abandoned if a.turn_number is not None})
    for turn_number in abandoned_turns:
        latest_attempt = store.get_turn_cycle(run_id, turn_number, authoritative_only=False)
        authoritative = store.get_turn_cycle(run_id, turn_number, authoritative_only=True)
        if latest_attempt is None:
            findings.append({"kind": "abandoned_turn_missing_from_record", "turn": turn_number})
            continue
        per_turn[turn_number] = {
            "latest_attempt_index": latest_attempt.turn_cycle.attempt_index,
            "latest_attempt_outcome": latest_attempt.turn_cycle.outcome.value,
            "authoritative_attempt_index": (
                authoritative.turn_cycle.attempt_index if authoritative is not None else None
            ),
        }
        if (
            authoritative is not None
            and latest_attempt.turn_cycle.outcome.value == "abandoned"
            and authoritative.turn_cycle.attempt_index == latest_attempt.turn_cycle.attempt_index
        ):
            findings.append(
                {
                    "kind": "abandoned_attempt_still_authoritative",
                    "turn": turn_number,
                    "attempt_index": latest_attempt.turn_cycle.attempt_index,
                }
            )

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {
        "detection_trigger_events": sum(
            1 for e in events if e.event_type in _RECOVERY_TRIGGER_EVENT_TYPES
        ),
        "turn_abandoned_events": len(abandoned),
        "resumed_events": len(resumed),
        "matched_abandon_resume_pairs": matched_pairs,
        "recovery_limit_reached_events": len(limit_reached),
        "turns_with_abandonment": per_turn,
    }
    return AuditReport(outcome=outcome, findings=findings, summary=summary)


# --------------------------------------------------------------------------
# T160 -- civsim audit completeness <run_id>
# --------------------------------------------------------------------------


def audit_completeness(store: MatchStore, run_id: RunId) -> AuditReport:
    """`civsim audit completeness <run_id>` (T160): zero silently missing
    turns or steps (SC-003, SC-011, SC-021).

    `store.completeness.record_completeness_status` (T158) already derives
    the *rolled-up* `RecordCompletenessStatus` this run's own `Run` record
    should carry; this audit additionally enumerates every gap, since
    `record_completeness_status` reports only that gaps exist, not which
    turns or which steps. Both are computed directly from the port's own
    `turn_gaps`/`step_gaps` (FR-052), over every turn from 1 to the highest
    one this run has ever attempted (`_discover_turns`, via save points, the
    same range `record_completeness_status` itself checks against).
    """
    insufficient = _run_precondition(store, run_id)
    if insufficient is not None:
        return insufficient

    turns = _discover_turns(store, run_id)
    highest_turn = max(turns) if turns else 0
    # T239: a branch owes its record only from its branch point onward -- turns before
    # `parent_turn` are the parent's record, reachable through the recorded lineage (FR-033,
    # Principle IV). The floor is `store.completeness.first_owed_turn`, the same single
    # definition the rolled-up `record_completeness_status` applies, so this audit's own gap
    # enumeration can never disagree with the status it reports alongside them.
    floor = first_owed_turn(store.get_run(run_id))
    turn_gap_list = [turn for turn in store.turn_gaps(run_id) if turn >= floor]

    findings: list[dict[str, Any]] = [{"kind": "turn_gap", "turn": turn} for turn in turn_gap_list]

    authoritative_count = 0
    step_gap_total = 0
    owed_turns = range(floor, highest_turn + 1)
    for turn in owed_turns:
        gaps = store.step_gaps(run_id, turn)
        if gaps:
            step_gap_total += len(gaps)
            findings.append({"kind": "step_gap", "turn": turn, "missing_step_indices": gaps})
        if store.get_turn_cycle(run_id, turn, authoritative_only=True) is not None:
            authoritative_count += 1

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {
        "highest_recorded_turn": highest_turn,
        "first_owed_turn": floor,
        "turn_gaps": len(turn_gap_list),
        "step_gaps": step_gap_total,
        "authoritative_turns": f"{authoritative_count}/{len(owed_turns)}",
        "record_completeness_status": record_completeness_status(store, run_id).value,
    }
    return AuditReport(outcome=outcome, findings=findings, summary=summary)


# --------------------------------------------------------------------------
# T176 -- civsim audit lineage <branch_id>
# --------------------------------------------------------------------------


def audit_lineage(store: MatchStore, branch_id: RunId) -> AuditReport:
    """`civsim audit lineage <branch_id>` (T176): `branch_id` records a
    parent run and turn, a matching `branch_created` event, and the parent
    actually has a save point at that turn (quickstart.md Scenario 5,
    FR-033).
    """
    insufficient = _run_precondition(store, branch_id)
    if insufficient is not None:
        return insufficient

    run = store.get_run(branch_id)
    assert run is not None  # narrowed by _run_precondition

    findings: list[dict[str, Any]] = []
    if run.parent_run_id is None or run.parent_turn is None:
        findings.append({"kind": "missing_lineage", "run_id": str(branch_id)})
        return AuditReport(
            outcome=AuditOutcome.FAILED,
            findings=findings,
            summary={"parent_run_id": None, "parent_turn": None},
        )

    parent = store.get_run(run.parent_run_id)
    if parent is None:
        findings.append(
            {
                "kind": "parent_run_missing",
                "parent_run_id": str(run.parent_run_id),
            }
        )

    parent_save = find_save_point(store, run.parent_run_id, run.parent_turn)
    if parent_save is None:
        findings.append(
            {
                "kind": "parent_save_point_missing",
                "parent_run_id": str(run.parent_run_id),
                "parent_turn": run.parent_turn,
            }
        )

    branch_created_events = [
        e
        for e in store.list_run_events(branch_id, event_types=[RunEventType.BRANCH_CREATED])
        if e.detail.get("parent_run_id") == run.parent_run_id
        and e.detail.get("parent_turn") == run.parent_turn
    ]
    if not branch_created_events:
        findings.append(
            {
                "kind": "branch_created_event_missing_or_mismatched",
                "parent_run_id": str(run.parent_run_id),
                "parent_turn": run.parent_turn,
            }
        )

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {
        "parent_run_id": str(run.parent_run_id),
        "parent_turn": run.parent_turn,
        "parent_run_on_record": parent is not None,
        "parent_save_point_on_record": parent_save is not None,
        "branch_created_events_matching": len(branch_created_events),
    }
    return AuditReport(outcome=outcome, findings=findings, summary=summary)


# --------------------------------------------------------------------------
# T176 -- civsim audit immutability <run_id>
# --------------------------------------------------------------------------


def audit_immutability(store: MatchStore, run_id: RunId) -> AuditReport:
    """`civsim audit immutability <run_id>` (T176): every record this run
    owns still names *this* run, never a child's (invariant I12, FR-034).

    `tests/integration/test_branching.py` already proves parent immutability
    the strong way, with an actual before/after snapshot around a real
    `create_branch` call -- that proof needs the write path in hand, which a
    store-only audit run after the fact does not have. What this audit *can*
    check, from the record alone: every save point `list_save_points`
    returns for `run_id`, and every turn cycle / model call reachable
    through `get_turn_cycle`, still declares `run_id` as its own owner
    (never a mismatched or child run's id) -- the structural half of "a
    child run cannot write to a parent's records" that remains checkable
    after the fact, re-asserted here the way `audit_decisions` re-asserts
    invariants the write path (`store/sqlite_adapter.py`, T168) already
    enforces, rather than only ever trusting that it did.

    When `run_id` is itself a branch (`parent_run_id` set), this also
    confirms none of its own `save_point_id`/`turn_cycle_id` values collide
    with its parent's at the recorded `parent_turn` -- an accidental id
    collision would be exactly the aliasing FR-034 forbids.
    """
    insufficient = _run_precondition(store, run_id)
    if insufficient is not None:
        return insufficient

    run = store.get_run(run_id)
    assert run is not None  # narrowed by _run_precondition

    findings: list[dict[str, Any]] = []
    save_points = store.list_save_points(run_id)
    for save_point in save_points:
        if save_point.run_id != run_id:
            findings.append(
                {
                    "kind": "save_point_owned_by_another_run",
                    "save_point_id": str(save_point.save_point_id),
                    "owner": str(save_point.run_id),
                }
            )

    turns_examined = 0
    for turn, record in _iter_authoritative_records(store, run_id):
        turns_examined += 1
        if record.turn_cycle.run_id != run_id:
            findings.append(
                {
                    "kind": "turn_cycle_owned_by_another_run",
                    "turn": turn,
                    "owner": str(record.turn_cycle.run_id),
                }
            )
        for bundle in record.steps:
            if bundle.model_call.run_id != run_id:
                findings.append(
                    {
                        "kind": "model_call_owned_by_another_run",
                        "turn": turn,
                        "step_index": bundle.step.step_index,
                        "owner": str(bundle.model_call.run_id),
                    }
                )

    if run.parent_run_id is not None and run.parent_turn is not None:
        parent_saves = {sp.save_point_id for sp in store.list_save_points(run.parent_run_id)}
        own_saves = {sp.save_point_id for sp in save_points}
        colliding_saves = parent_saves & own_saves
        for save_point_id in sorted(colliding_saves, key=str):
            findings.append(
                {
                    "kind": "save_point_id_collides_with_parent",
                    "save_point_id": str(save_point_id),
                }
            )

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {
        "save_points_examined": len(save_points),
        "turns_examined": turns_examined,
        "is_branch": run.parent_run_id is not None,
    }
    return AuditReport(outcome=outcome, findings=findings, summary=summary)


# --------------------------------------------------------------------------
# T176 -- civsim audit builds <seed_set>
# --------------------------------------------------------------------------


def audit_builds(
    store: MatchStore, seed_set: SeedSet, *, run_ids: Sequence[RunId] = ()
) -> AuditReport:
    """`civsim audit builds <seed_set>` (T176): a set carrying **any**
    accepted build change reports as non-uniform, with its runs partitioned
    by build (quickstart.md Scenarios 5 and 9, FR-031).

    *seed_set* is the already-loaded `SeedSet` (there is no `MatchStore`
    read for one -- seed sets are pure YAML documents, `config/seed_set.py`).
    *run_ids*, optional and empty by default, is this seed set's own runs --
    see the module docstring's note on why `MatchStore` cannot supply that
    list itself. When given, each run's recorded `game_build` is resolved
    and partitioned; a build that matches neither the set's base `game_build`
    nor any accepted `to_build` is `run_build_not_accounted_for` (a build
    drift nothing ever accepted). Every platform-crossing entry in
    `accepted_build_changes` is also re-checked for a recorded
    `r20_spike_ref` here (defense in depth: `seedset accept-build`,
    `operator/cli.py`, already refuses to construct one without it).
    """
    findings: list[dict[str, Any]] = []
    accepted_transitions: list[dict[str, Any]] = []
    for acceptance in seed_set.accepted_build_changes:
        accepted_transitions.append(
            {
                "from_build": acceptance.from_build,
                "to_build": acceptance.to_build,
                "is_platform_transition": acceptance.is_platform_transition,
                "r20_spike_ref": acceptance.r20_spike_ref,
            }
        )
        if acceptance.is_platform_transition and not acceptance.r20_spike_ref:
            findings.append(
                {
                    "kind": "platform_transition_acceptance_missing_spike_ref",
                    "from_build": acceptance.from_build,
                    "to_build": acceptance.to_build,
                }
            )
        # Defense in depth against BuildAcceptance.is_platform_transition
        # somehow disagreeing with a fresh derivation from the build
        # strings themselves -- it cannot today (the model recomputes it on
        # every validation, models/common.py), but this audit re-asserts it
        # the way audit_decisions re-asserts a structurally-enforced
        # invariant rather than only ever trusting the model held.
        if acceptance.is_platform_transition != is_platform_transition(
            acceptance.from_build, acceptance.to_build
        ):
            findings.append(
                {
                    "kind": "is_platform_transition_disagrees_with_build_strings",
                    "from_build": acceptance.from_build,
                    "to_build": acceptance.to_build,
                }
            )

    known_builds = {seed_set.game_build, *(a.to_build for a in seed_set.accepted_build_changes)}
    builds_seen: dict[str, list[str]] = {}
    for rid in run_ids:
        run = store.get_run(rid)
        if run is None:
            findings.append({"kind": "run_not_on_record", "run_id": str(rid)})
            continue
        builds_seen.setdefault(run.game_build, []).append(str(rid))
        if run.game_build not in known_builds:
            findings.append(
                {
                    "kind": "run_build_not_accounted_for",
                    "run_id": str(rid),
                    "game_build": run.game_build,
                }
            )

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {
        "is_uniform": seed_set.is_uniform,
        "base_build": seed_set.game_build,
        "accepted_transitions": accepted_transitions,
        "run_enumeration_supported": bool(run_ids),
        "runs_examined": len(run_ids),
        "builds_seen": builds_seen,
    }
    return AuditReport(outcome=outcome, findings=findings, summary=summary)


# --------------------------------------------------------------------------
# T190 -- civsim audit models <run_id>
# --------------------------------------------------------------------------


def audit_models(store: MatchStore, run_id: RunId) -> AuditReport:
    """`civsim audit models <run_id>` (T190): every call names its served
    model with latency, cost, and retry count, resolved per step and rolled
    up per turn (SC-016).

    `ModelCall.model_served`/`latency_ms`/`cost`/`retry_count` are all
    required fields (`models/records.py`) a `DecisionStepBundle` cannot be
    constructed without, so this audit's findings are defense in depth
    rather than the primary guarantee -- its real value is the rollup:
    `summary["per_turn"]` names every model that served each turn, its
    total latency, retry count, and cost, and whether a fallback served any
    step of it, giving an operator the per-turn cost/latency picture P3/P7
    require without ever aggregating a single call's own record away.
    """
    pre = _preconditions(store, run_id)
    if isinstance(pre, AuditReport):
        return pre

    findings: list[dict[str, Any]] = []
    per_turn: dict[int, dict[str, Any]] = {}
    steps_examined = 0
    fallback_served_turns = 0

    for turn, record in _iter_authoritative_records(store, run_id):
        models_served: set[str] = set()
        latency_ms_total = 0
        retry_count_total = 0
        cost_usd_total = 0.0
        turn_fallback = False
        for bundle in record.steps:
            steps_examined += 1
            call = bundle.model_call
            if not call.model_served.provider or not call.model_served.model:
                findings.append(
                    {
                        "kind": "model_call_missing_served_model",
                        "turn": turn,
                        "step_index": bundle.step.step_index,
                    }
                )
            models_served.add(f"{call.model_served.provider}/{call.model_served.model}")
            latency_ms_total += call.latency_ms
            retry_count_total += call.retry_count
            if call.cost.amount_usd is not None:
                cost_usd_total += call.cost.amount_usd
            turn_fallback = turn_fallback or call.fallback_occurred

        if turn_fallback:
            fallback_served_turns += 1
        per_turn[turn] = {
            "models_served": sorted(models_served),
            "latency_ms_total": latency_ms_total,
            "retry_count_total": retry_count_total,
            "cost_usd_total": cost_usd_total,
            "fallback_occurred": turn_fallback,
        }

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {
        "turns_examined": len(per_turn),
        "steps_examined": steps_examined,
        "turns_served_by_fallback": fallback_served_turns,
        "per_turn": per_turn,
    }
    return AuditReport(outcome=outcome, findings=findings, summary=summary)


# --------------------------------------------------------------------------
# T190 -- civsim audit secrets <run_id>
# --------------------------------------------------------------------------


def audit_secrets(store: MatchStore, run_id: RunId) -> AuditReport:
    """`civsim audit secrets <run_id>` (T190): zero credential-shaped values
    appear in this run's records or captures (SC-016, SC-018).

    Every record reachable for `run_id` through `MatchStore` -- the `Run`
    itself, each authoritative turn's `TurnCycle`/`DecisionStep`/
    `Observation`/`Decision`/`ModelCall`, every `ScreenCapture` its
    observations reference, and every `RunEvent` -- is dumped
    (`model_dump(mode="json")`) and passed through
    `telemetry.redaction.redact_value`, the same structural filter
    `RunEvent.detail` and every `HarnessError` already apply at construction
    time (FR-043). A record that comes back *different* from how it went in
    means a credential-shaped value reached the store despite that
    write-time discipline -- exactly what this audit exists to catch, from
    the record alone, rather than only trusting the write path held. This
    covers records and captures; it has no access to a process's log output
    (see the module docstring).
    """
    insufficient = _run_precondition(store, run_id)
    if insufficient is not None:
        return insufficient

    run = store.get_run(run_id)
    assert run is not None  # narrowed by _run_precondition

    findings: list[dict[str, Any]] = []
    records_examined = 0

    def _check(record: Any, *, kind: str, **context: Any) -> None:
        nonlocal records_examined
        records_examined += 1
        dumped = record.model_dump(mode="json")
        if redact_value(dumped) != dumped:
            findings.append({"kind": f"credential_shaped_value_in_{kind}", **context})

    _check(run, kind="run")

    for turn, record in _iter_authoritative_records(store, run_id):
        _check(record.turn_cycle, kind="turn_cycle", turn=turn)
        for bundle in record.steps:
            step_index = bundle.step.step_index
            _check(bundle.step, kind="decision_step", turn=turn, step_index=step_index)
            _check(bundle.observation, kind="observation", turn=turn, step_index=step_index)
            _check(bundle.decision, kind="decision", turn=turn, step_index=step_index)
            _check(bundle.model_call, kind="model_call", turn=turn, step_index=step_index)
            for capture_id in bundle.observation.captures:
                capture = store.get_capture(capture_id)
                if capture is not None:
                    _check(
                        capture,
                        kind="capture",
                        turn=turn,
                        step_index=step_index,
                        capture_id=str(capture_id),
                    )

    for event in store.list_run_events(run_id):
        _check(
            event,
            kind="run_event",
            event_id=str(event.event_id),
            event_type=event.event_type.value,
        )

    outcome = AuditOutcome.FAILED if findings else AuditOutcome.PASSED
    summary = {"records_examined": records_examined}
    return AuditReport(outcome=outcome, findings=findings, summary=summary)
