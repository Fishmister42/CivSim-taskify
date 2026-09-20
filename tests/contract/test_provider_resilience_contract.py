"""End-to-end resilience-chain tests: retry, fallback, exhaustion, accounting, and decision
linkage exercised *together* (T155, T156, T184-T187).

`tests/contract/test_model_provider_port.py` asserts P1-P11 against single calls and single
`ProviderChain.complete_step` invocations in isolation; this file exercises the pieces the way a
real turn actually uses them: preflight once, then several decision steps through the chain,
each response fed into `provider.accounting.record_model_call` and `agent.decisions.
build_decision`, proving the whole pipeline produces a self-consistent, step-attributable record
rather than merely that each piece works alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from civsim_harness.agent.decisions import build_decision
from civsim_harness.errors import ProviderChainExhausted
from civsim_harness.models.common import (
    CatalogVersionRef,
    DecisionId,
    DecisionStepId,
    EventId,
    ModelCallId,
    ModelRef,
    ObservationId,
    RunId,
    TurnCycleId,
)
from civsim_harness.models.config import ModelConfig
from civsim_harness.models.decision import ActionExecution, DecisionTrigger, ExecutionOutcome
from civsim_harness.models.records import ModelCall, RunEvent, RunEventType
from civsim_harness.models.turn import DecisionStep, Observation, StepProgress
from civsim_harness.provider.accounting import ImageCountAccountingMismatch, record_model_call
from civsim_harness.provider.chain import ProviderChain, RetryPolicy
from civsim_harness.provider.port import DecisionRequest, RawDecision
from civsim_harness.provider.preflight import preflight_chain
from civsim_harness.store.port import DecisionStepBundle
from fakes.fake_provider import FakeModelProvider

_RUN_ID = RunId("run-xyz")


def _model(name: str, provider: str = "openrouter") -> ModelRef:
    return ModelRef(provider=provider, model=name)


def _raw_decision(reasoning: str = "advance the scout") -> RawDecision:
    return RawDecision(action_declaration_id="units.move_to", reasoning=reasoning, parameters={})


def _request(model: ModelRef, *, step_index: int = 1) -> DecisionRequest:
    return DecisionRequest(
        model=model,
        system="You are the sole player of Civilization VI.",
        observation="Current screen: WorldScreen",
        images=[],
        step_index=step_index,
        response_schema={"type": "object"},
    )


def _model_config(primary: ModelRef, *fallbacks: ModelRef) -> ModelConfig:
    return ModelConfig(primary=primary, fallbacks=list(fallbacks))


def _execution() -> ActionExecution:
    return ActionExecution(
        outcome=ExecutionOutcome.APPLIED,
        verification={},
        verified_at=datetime(2026, 9, 20, tzinfo=UTC),
    )


@dataclass
class _EventSink:
    events: list[RunEvent] = field(default_factory=list)

    def write_run_event(self, event: RunEvent) -> EventId:
        self.events.append(event)
        return event.event_id

    def of_type(self, event_type: RunEventType) -> list[RunEvent]:
        return [e for e in self.events if e.event_type == event_type]


@dataclass
class _ModelCallStore:
    calls: list[ModelCall] = field(default_factory=list)

    def write_model_call(self, call: ModelCall) -> ModelCallId:
        self.calls.append(call)
        return call.model_call_id


def _chain(provider: object, model_config: ModelConfig, sink: _EventSink) -> ProviderChain:
    return ProviderChain(
        provider,  # type: ignore[arg-type]
        model_config,
        event_sink=sink,
        retry_policy=RetryPolicy(max_attempts_per_model=2),
        sleep=lambda _delay: None,
        rand=lambda: 0.0,
        clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
    )


# --------------------------------------------------------------------------
# Backoff/jitter shape (P5, FR-041)
# --------------------------------------------------------------------------


def test_retry_policy_backoff_grows_exponentially_and_is_capped() -> None:
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=5.0, multiplier=2.0, jitter_s=0.0)
    assert policy.delay_for(1, rand=lambda: 0.0) == pytest.approx(1.0)
    assert policy.delay_for(2, rand=lambda: 0.0) == pytest.approx(2.0)
    assert policy.delay_for(3, rand=lambda: 0.0) == pytest.approx(4.0)
    assert policy.delay_for(4, rand=lambda: 0.0) == pytest.approx(5.0)  # capped


def test_retry_policy_applies_jitter_on_top_of_the_base_delay() -> None:
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=100.0, multiplier=2.0, jitter_s=1.0)
    low = policy.delay_for(1, rand=lambda: 0.0)
    high = policy.delay_for(1, rand=lambda: 1.0)
    assert high > low
    assert high - low == pytest.approx(1.0)


def test_actual_sleep_is_invoked_between_retries_with_computed_delays() -> None:
    fake = FakeModelProvider()
    model = _model("primary-a")
    fake.queue_rate_limited()
    fake.queue_decision(_raw_decision())
    sink = _EventSink()
    delays: list[float] = []
    chain = ProviderChain(
        fake,
        _model_config(model),
        event_sink=sink,
        retry_policy=RetryPolicy(max_attempts_per_model=3, base_delay_s=2.0, jitter_s=0.0),
        sleep=delays.append,
        rand=lambda: 0.0,
    )
    chain.complete_step(_request(model), run_id=_RUN_ID)
    assert delays == [pytest.approx(2.0)]


# --------------------------------------------------------------------------
# Full pipeline: preflight -> chain -> accounting -> decision linkage
# --------------------------------------------------------------------------


def test_full_turn_pipeline_links_decisions_to_the_model_calls_that_served_them() -> None:
    fake = FakeModelProvider()
    primary = _model("primary-a")
    fallback = _model("fallback-a")
    model_config = _model_config(primary, fallback)

    # Preflight passes cleanly on the generous defaults.
    preflight_chain(fake, model_config, worst_case_context_tokens=1_000)

    # Step 1: primary serves directly.
    fake.queue_decision(_raw_decision("step 1"))
    # Step 2: primary fails twice, fallback serves it.
    fake.queue_failed()
    fake.queue_failed()
    fake.queue_decision(_raw_decision("step 2"), model_served=fallback)

    event_sink = _EventSink()
    call_sink = _ModelCallStore()
    chain = _chain(fake, model_config, event_sink)

    turn_cycle_id = TurnCycleId("tc-1")
    decisions = []
    model_calls = []
    for step_index in (1, 2):
        step_id = DecisionStepId(f"step-{step_index}")
        response = chain.complete_step(_request(primary, step_index=step_index), run_id=_RUN_ID)
        assert response.decision is not None
        model_call_id = ModelCallId(f"call-{step_index}")
        call = record_model_call(
            call_sink,
            model_call_id=model_call_id,
            run_id=_RUN_ID,
            turn_cycle_id=turn_cycle_id,
            decision_step_id=step_id,
            model_requested=primary,
            request=_request(primary, step_index=step_index),
            response=response,
        )
        model_calls.append(call)
        decision = build_decision(
            response.decision,
            decision_id=DecisionId(f"decision-{step_index}"),
            decision_step_id=step_id,
            model_call_id=model_call_id,
            trigger=DecisionTrigger.PROACTIVE,
            execution=_execution(),
        )
        decisions.append(decision)

    # Step 1 served by primary, step 2 served by fallback -- distinguishable per step (SC-016).
    assert model_calls[0].fallback_occurred is False
    assert model_calls[0].model_served == primary
    assert model_calls[1].fallback_occurred is True
    assert model_calls[1].model_served == fallback

    # Every Decision resolves, through its own model_call_id, to the ModelCall that produced it.
    for decision, call in zip(decisions, model_calls, strict=True):
        assert decision.model_call_id == call.model_call_id

    assert len(call_sink.calls) == 2
    assert len(event_sink.of_type(RunEventType.PROVIDER_FALLBACK)) == 1


def test_decision_step_bundle_accepts_the_produced_records_as_cross_consistent() -> None:
    """`build_decision` + `record_model_call`'s outputs satisfy `store.port.
    DecisionStepBundle`'s own cross-record consistency validator -- i.e. what this task's
    records produce is actually shaped the way the store expects to durably record it, without
    this task touching store/** itself.
    """
    fake = FakeModelProvider()
    model = _model("primary-a")
    fake.queue_decision(_raw_decision())
    chain = _chain(fake, _model_config(model), _EventSink())
    call_sink = _ModelCallStore()

    step_id = DecisionStepId("step-1")
    turn_cycle_id = TurnCycleId("tc-1")
    request = _request(model, step_index=1)
    response = chain.complete_step(request, run_id=_RUN_ID)
    assert response.decision is not None
    model_call_id = ModelCallId("call-1")
    call = record_model_call(
        call_sink,
        model_call_id=model_call_id,
        run_id=_RUN_ID,
        turn_cycle_id=turn_cycle_id,
        decision_step_id=step_id,
        model_requested=model,
        request=request,
        response=response,
    )
    decision = build_decision(
        response.decision,
        decision_id=DecisionId("decision-1"),
        decision_step_id=step_id,
        model_call_id=model_call_id,
        trigger=DecisionTrigger.PROACTIVE,
        execution=_execution(),
    )
    observation = Observation(
        observation_id=ObservationId("obs-1"),
        decision_step_id=step_id,
        assembled_at=datetime(2026, 9, 20, tzinfo=UTC),
        catalog_version=CatalogVersionRef(version="1", content_hash="deadbeef"),
        entries=[],
        captures=[],
        screen_identity="WorldScreen",
    )
    step = DecisionStep(
        decision_step_id=step_id,
        turn_cycle_id=turn_cycle_id,
        step_index=1,
        observation_id=observation.observation_id,
        decision_id=decision.decision_id,
        model_call_id=model_call_id,
        progress=StepProgress.CHANGED_STATE,
        no_progress_streak_after=0,
        visually_degraded=False,
        started_at=datetime(2026, 9, 20, tzinfo=UTC),
        ended_at=datetime(2026, 9, 20, tzinfo=UTC),
    )

    # Raises if any record disagrees with another -- this is the assertion.
    DecisionStepBundle(step=step, observation=observation, decision=decision, model_call=call)


# --------------------------------------------------------------------------
# Accounting-level image-count defence (P2, T185) -- independent of the chain
# --------------------------------------------------------------------------


def test_accounting_refuses_to_record_a_mismatched_image_count() -> None:
    fake = FakeModelProvider()
    model = _model("primary-a")
    fake.queue_decision(_raw_decision(), image_count=99)
    response = fake.complete(_request(model))
    call_sink = _ModelCallStore()

    with pytest.raises(ImageCountAccountingMismatch):
        record_model_call(
            call_sink,
            model_call_id=ModelCallId("call-1"),
            run_id=_RUN_ID,
            turn_cycle_id=TurnCycleId("tc-1"),
            decision_step_id=DecisionStepId("step-1"),
            model_requested=model,
            request=_request(model),
            response=response,
        )
    assert call_sink.calls == []


# --------------------------------------------------------------------------
# Exhaustion pauses in a recorded state, never fabricates (P6, T156)
# --------------------------------------------------------------------------


def test_exhaustion_produces_no_fabricated_decision_and_no_model_call_is_recorded() -> None:
    fake = FakeModelProvider()
    model = _model("primary-a")
    fake.queue_failed()
    fake.queue_failed()
    sink = _EventSink()
    call_sink = _ModelCallStore()
    chain = ProviderChain(
        fake,
        _model_config(model),
        event_sink=sink,
        retry_policy=RetryPolicy(max_attempts_per_model=2),
        sleep=lambda _d: None,
        rand=lambda: 0.0,
    )

    with pytest.raises(ProviderChainExhausted):
        chain.complete_step(_request(model), run_id=_RUN_ID)

    # No accounting record exists for an exhausted step -- nothing was ever recorded for it.
    assert call_sink.calls == []
    assert len(sink.of_type(RunEventType.MODEL_CHAIN_EXHAUSTED)) == 1
