"""``ModelProvider`` conformance suite: P1-P11 (T178), plus the three red-team
cases T179-T181 (contracts/model-provider-port.md).

Runs against both the fake (`tests/fakes/fake_provider.py`, already built) and the OpenRouter
adapter (`civsim_harness.provider.openrouter.OpenRouterProvider`, driven over
`httpx.MockTransport` -- no live network access or real API key anywhere in this file, matching
`tests/unit/test_openrouter_adapter.py`'s own approach).

Properties that only make sense at the single-call level (P3, P4, P7, P10) are exercised
directly against `provider.complete()`/`describe()` for both adapters. Properties that are
chain- or config-level (P1, P2, P5, P6, P9, P11) are exercised through
`civsim_harness.provider.chain.ProviderChain` and `civsim_harness.provider.preflight.
preflight_chain`, which is what actually implements them -- the port itself only shapes the
types (see `provider/port.py`'s module docstring).
"""

from __future__ import annotations

import inspect
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from civsim_harness.agent.decisions import MultipleDecisionsError
from civsim_harness.errors import ProviderChainExhausted
from civsim_harness.models.common import EventId, ModelRef, RunId
from civsim_harness.models.config import ModelConfig
from civsim_harness.models.records import CallOutcome, RunEvent, RunEventType
from civsim_harness.provider.chain import ImageCountMismatch, ProviderChain, RetryPolicy
from civsim_harness.provider.openrouter import OpenRouterProvider
from civsim_harness.provider.port import (
    AdapterObligationViolation,
    DecisionRequest,
    Image,
    ModelCapabilities,
    ObligationEnforcingProvider,
    RawDecision,
)
from civsim_harness.provider.preflight import ChainPreflightError, preflight_chain
from fakes.fake_provider import DEFAULT_CAPABILITIES, FakeModelProvider, ProviderContractViolation

_FAKE_KEY = "sk-or-v1-TOTALLYFAKESECRETVALUE1234567890"
_RUN_ID = RunId("run-1")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _model(name: str, provider: str = "openrouter") -> ModelRef:
    return ModelRef(provider=provider, model=name)


def _raw_decision(reasoning: str = "Scout the nearby hills.") -> RawDecision:
    return RawDecision(
        action_declaration_id="units.move_to", reasoning=reasoning, parameters={"unit_id": "u1"}
    )


def _request(
    model: ModelRef, *, images: list[Image] | None = None, step_index: int = 1
) -> DecisionRequest:
    return DecisionRequest(
        model=model,
        system="You are the sole player of Civilization VI.",
        observation="Current screen: WorldScreen",
        images=images if images is not None else [],
        step_index=step_index,
        response_schema={"type": "object"},
    )


def _model_config(primary: ModelRef, *fallbacks: ModelRef) -> ModelConfig:
    return ModelConfig(primary=primary, fallbacks=list(fallbacks))


@dataclass
class _RecordingEventSink:
    """Minimal ``ProviderChain.RunEventSink`` double: records every event, in order."""

    events: list[RunEvent] = field(default_factory=list)

    def write_run_event(self, event: RunEvent) -> EventId:
        self.events.append(event)
        return event.event_id

    def of_type(self, event_type: RunEventType) -> list[RunEvent]:
        return [event for event in self.events if event.event_type == event_type]


def _chain(
    provider: Any,
    model_config: ModelConfig,
    *,
    sink: _RecordingEventSink | None = None,
    max_attempts: int = 2,
) -> ProviderChain:
    return ProviderChain(
        provider,
        model_config,
        event_sink=sink if sink is not None else _RecordingEventSink(),
        retry_policy=RetryPolicy(max_attempts_per_model=max_attempts),
        sleep=lambda _delay: None,
        rand=lambda: 0.0,
        clock=lambda: datetime(2026, 9, 20, tzinfo=UTC),
    )


# --------------------------------------------------------------------------
# OpenRouter scripting harness (test-only; no live network)
# --------------------------------------------------------------------------


class _OpenRouterScript:
    """Scripts an ``OpenRouterProvider`` over ``httpx.MockTransport`` the way
    ``FakeModelProvider`` scripts itself, so the same conformance assertions can run against
    both adapters.
    """

    def __init__(self) -> None:
        self.models_entries: list[dict[str, Any]] = []
        self._chat_responses: deque[httpx.Response] = deque()
        self.chat_call_count = 0
        self.models_call_count = 0

    def set_model_entry(self, model: ModelRef, **fields: Any) -> None:
        self.models_entries = [e for e in self.models_entries if e.get("id") != model.model]
        self.models_entries.append({"id": model.model, **fields})

    def queue_decision_response(
        self,
        *,
        model_served: str | None = None,
        raw: RawDecision | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        decision = raw if raw is not None else _raw_decision()
        content = json.dumps(
            {
                "action_declaration_id": decision.action_declaration_id,
                "reasoning": decision.reasoning,
                "parameters": decision.parameters,
                "is_end_turn": decision.is_end_turn,
            }
        )
        body: dict[str, Any] = {"choices": [{"message": {"content": content}}]}
        if model_served:
            body["model"] = model_served
        if usage:
            body["usage"] = usage
        self._chat_responses.append(httpx.Response(200, json=body))

    def queue_multi_decision_response(self) -> None:
        decisions = [
            {"action_declaration_id": "units.move_to", "reasoning": "first"},
            {"action_declaration_id": "units.move_to", "reasoning": "second"},
        ]
        body = {"choices": [{"message": {"content": json.dumps(decisions)}}]}
        self._chat_responses.append(httpx.Response(200, json=body))

    def queue_status(self, status_code: int, **kwargs: Any) -> None:
        self._chat_responses.append(httpx.Response(status_code, **kwargs))

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            self.models_call_count += 1
            return httpx.Response(200, json={"data": self.models_entries})
        self.chat_call_count += 1
        if self._chat_responses:
            return self._chat_responses.popleft()
        raise AssertionError("no chat completion response was scripted for this call")


def _openrouter(script: _OpenRouterScript, monkeypatch: pytest.MonkeyPatch) -> OpenRouterProvider:
    monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
    transport = httpx.MockTransport(script.handler)
    client = httpx.Client(transport=transport, base_url="https://openrouter.ai/api/v1")
    return OpenRouterProvider(client=client)


# ==========================================================================
# P1 -- chain preflight
# ==========================================================================


def test_p1_preflight_passes_when_every_model_confirms_sufficient_capabilities() -> None:
    fake = FakeModelProvider()
    primary, fallback = _model("primary-a"), _model("fallback-a")
    results = preflight_chain(
        fake, _model_config(primary, fallback), worst_case_context_tokens=1_000
    )
    assert [r.model for r in results] == [primary, fallback]
    assert all(r.ok for r in results)
    assert fake.calls == []  # preflight never calls complete()


def test_p1_preflight_over_openrouter_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _OpenRouterScript()
    primary = _model("anthropic/claude-sonnet-5")
    script.set_model_entry(
        primary, context_length=200_000, architecture={"input_modalities": ["text", "image"]}
    )
    provider = _openrouter(script, monkeypatch)

    results = preflight_chain(provider, _model_config(primary), worst_case_context_tokens=50_000)
    assert results[0].ok
    assert script.chat_call_count == 0


# ==========================================================================
# P2 / T185 / T179 -- no image dropping, ever
# ==========================================================================


def test_p2_image_count_mismatch_from_adapter_raises_as_a_defect() -> None:
    fake = FakeModelProvider()
    model = _model("primary-a")
    fake.queue_decision(_raw_decision(), image_count=0)  # misreports on purpose
    chain = _chain(fake, _model_config(model))
    images = [Image(media_type="image/png", data=b"one")]

    with pytest.raises(ImageCountMismatch):
        chain.complete_step(_request(model, images=images), run_id=_RUN_ID)


def test_t179_tiny_context_model_fails_the_run_never_a_reduced_image_call() -> None:
    fake = FakeModelProvider()
    model = _model("tiny-context-model")
    fake.set_capabilities(
        model,
        ModelCapabilities(
            accepts_images=True,
            max_context_tokens=100,
            max_images_per_request=None,
            confirmed=True,
        ),
    )

    with pytest.raises(ChainPreflightError) as excinfo:
        preflight_chain(fake, _model_config(model), worst_case_context_tokens=1_000_000)

    assert "tiny-context-model" in str(excinfo.value.detail)
    assert fake.calls == []  # never attempted a reduced-image call


def test_t179_no_image_support_fails_the_run_never_a_reduced_image_call() -> None:
    fake = FakeModelProvider()
    model = _model("text-only-model")
    fake.set_capabilities(
        model,
        ModelCapabilities(
            accepts_images=False,
            max_context_tokens=1_000_000,
            max_images_per_request=None,
            confirmed=True,
        ),
    )

    with pytest.raises(ChainPreflightError) as excinfo:
        preflight_chain(fake, _model_config(model), worst_case_context_tokens=1_000)

    assert "text-only-model" in str(excinfo.value.detail)
    assert fake.calls == []


def test_t179_unconfirmed_capabilities_fail_closed() -> None:
    fake = FakeModelProvider()
    model = _model("unconfirmed-model")
    fake.set_capabilities(
        model,
        ModelCapabilities(
            accepts_images=True,
            max_context_tokens=1_000_000,
            max_images_per_request=None,
            confirmed=False,
        ),
    )
    with pytest.raises(ChainPreflightError):
        preflight_chain(fake, _model_config(model), worst_case_context_tokens=1_000)
    assert fake.calls == []


def test_t179_openrouter_unconfirmable_model_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _OpenRouterScript()  # no entries at all -> describe() can't confirm anything
    model = _model("ghost-model")
    provider = _openrouter(script, monkeypatch)

    with pytest.raises(ChainPreflightError):
        preflight_chain(provider, _model_config(model), worst_case_context_tokens=1_000)
    assert script.chat_call_count == 0


# ==========================================================================
# P3 -- served model recorded
# ==========================================================================


def test_p3_fake_reports_a_different_served_model() -> None:
    fake = FakeModelProvider()
    requested = _model("primary-a")
    served = _model("primary-a-20260101")
    fake.queue_decision(_raw_decision(), model_served=served)
    chain = _chain(fake, _model_config(requested))

    response = chain.complete_step(_request(requested), run_id=_RUN_ID)
    assert response.model_served == served


def test_p3_openrouter_reports_the_dated_snapshot_it_actually_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _OpenRouterScript()
    requested = _model("anthropic/claude-sonnet-5")
    script.queue_decision_response(model_served="anthropic/claude-sonnet-5-20260115")
    provider = _openrouter(script, monkeypatch)
    chain = _chain(provider, _model_config(requested))

    response = chain.complete_step(_request(requested), run_id=_RUN_ID)
    assert response.model_served == _model("anthropic/claude-sonnet-5-20260115")


# ==========================================================================
# P4 -- empty is failure, never a decision to do nothing
# ==========================================================================


def test_p4_fake_empty_response_is_not_a_decision() -> None:
    fake = FakeModelProvider()
    model = _model("primary-a")
    fake.queue_empty_response()
    fake.queue_decision(_raw_decision())
    chain = _chain(fake, _model_config(model))

    response = chain.complete_step(_request(model), run_id=_RUN_ID)
    assert response.decision is not None  # retried and eventually succeeded
    sink = chain._event_sink  # type: ignore[attr-defined]
    assert len(sink.of_type(RunEventType.PROVIDER_FAILURE)) == 1


def test_p4_openrouter_empty_content_is_not_a_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _OpenRouterScript()
    model = _model("anthropic/claude-sonnet-5")
    script.queue_status(200, json={"choices": [{"message": {"content": "{}"}}]})
    provider = _openrouter(script, monkeypatch)

    response = provider.complete(_request(model))
    assert response.outcome == CallOutcome.EMPTY_RESPONSE
    assert response.decision is None


# ==========================================================================
# P5 -- retry then fall back, every attempt recorded
# ==========================================================================


def test_p5_fake_retries_then_succeeds_on_the_same_model() -> None:
    fake = FakeModelProvider()
    model = _model("primary-a")
    fake.queue_rate_limited()
    fake.queue_decision(_raw_decision())
    sink = _RecordingEventSink()
    chain = _chain(fake, _model_config(model), sink=sink, max_attempts=3)

    response = chain.complete_step(_request(model), run_id=_RUN_ID, turn_number=5)
    assert response.decision is not None
    assert response.fallback_occurred is False
    assert response.retry_count == 1
    assert len(sink.of_type(RunEventType.PROVIDER_FAILURE)) == 1
    assert len(sink.of_type(RunEventType.PROVIDER_RETRY)) == 1
    assert sink.events[0].turn_number == 5


def test_p5_fake_falls_back_to_next_model_after_exhausting_retries() -> None:
    fake = FakeModelProvider()
    primary, fallback = _model("primary-a"), _model("fallback-a")
    fake.queue_rate_limited()
    fake.queue_rate_limited()
    fake.queue_decision(_raw_decision(), model_served=fallback)
    sink = _RecordingEventSink()
    chain = _chain(fake, _model_config(primary, fallback), sink=sink, max_attempts=2)

    response = chain.complete_step(_request(primary), run_id=_RUN_ID)
    assert response.model_served == fallback
    assert response.fallback_occurred is True
    assert response.retry_count == 2
    assert len(sink.of_type(RunEventType.PROVIDER_FALLBACK)) == 1
    fallback_event = sink.of_type(RunEventType.PROVIDER_FALLBACK)[0]
    assert fallback_event.detail["from_model"] == "openrouter/primary-a"
    assert fallback_event.detail["to_model"] == "openrouter/fallback-a"


def test_p5_context_rejected_is_never_retried_in_place_only_falls_back() -> None:
    fake = FakeModelProvider()
    primary, fallback = _model("primary-a"), _model("fallback-a")
    fake.queue_context_rejected()
    fake.queue_decision(_raw_decision(), model_served=fallback)
    sink = _RecordingEventSink()
    chain = _chain(fake, _model_config(primary, fallback), sink=sink, max_attempts=5)

    response = chain.complete_step(_request(primary), run_id=_RUN_ID)
    assert response.model_served == fallback
    # Exactly one attempt against primary -- no retry loop for a context rejection.
    assert len(fake.calls) == 2
    assert len(sink.of_type(RunEventType.PROVIDER_RETRY)) == 0
    assert len(sink.of_type(RunEventType.CONTEXT_REJECTED)) == 1


def test_p5_openrouter_retries_429_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _OpenRouterScript()
    primary, fallback = _model("anthropic/claude-sonnet-5"), _model("google/gemini-3-pro")
    script.queue_status(429, json={"error": {"message": "rate limited"}})
    script.queue_status(429, json={"error": {"message": "rate limited"}})
    script.queue_decision_response(model_served=fallback.model)
    provider = _openrouter(script, monkeypatch)
    sink = _RecordingEventSink()
    chain = _chain(provider, _model_config(primary, fallback), sink=sink, max_attempts=2)

    response = chain.complete_step(_request(primary), run_id=_RUN_ID)
    assert response.model_served == fallback
    assert len(sink.of_type(RunEventType.PROVIDER_FALLBACK)) == 1


# ==========================================================================
# P6 / T156 -- exhaustion has no escape hatch
# ==========================================================================


def test_p6_fake_chain_exhaustion_raises_and_records_the_event() -> None:
    fake = FakeModelProvider()
    primary, fallback = _model("primary-a"), _model("fallback-a")
    for _ in range(4):
        fake.queue_failed()
    sink = _RecordingEventSink()
    chain = _chain(fake, _model_config(primary, fallback), sink=sink, max_attempts=2)

    with pytest.raises(ProviderChainExhausted) as excinfo:
        chain.complete_step(_request(primary), run_id=_RUN_ID)

    assert len(sink.of_type(RunEventType.MODEL_CHAIN_EXHAUSTED)) == 1
    exhausted_event = sink.of_type(RunEventType.MODEL_CHAIN_EXHAUSTED)[0]
    assert exhausted_event.detail["chain"] == ["openrouter/primary-a", "openrouter/fallback-a"]
    assert excinfo.value.detail["chain"] == ["openrouter/primary-a", "openrouter/fallback-a"]


def test_p6_exhaustion_has_no_fabricate_skip_or_default_move_path() -> None:
    """The only outcomes of a fully-failed chain are the exception and the event -- nothing
    resembling a decision (no ``RawDecision``, no default action) is produced anywhere."""
    fake = FakeModelProvider()
    model = _model("primary-a")
    fake.queue_failed()
    sink = _RecordingEventSink()
    chain = _chain(fake, _model_config(model), sink=sink, max_attempts=1)

    with pytest.raises(ProviderChainExhausted):
        chain.complete_step(_request(model), run_id=_RUN_ID)

    # Every recorded event's detail is failure/exhaustion bookkeeping -- never a decision payload.
    for event in sink.events:
        assert "action_declaration_id" not in event.detail


def test_p6_openrouter_chain_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _OpenRouterScript()
    model = _model("anthropic/claude-sonnet-5")
    script.queue_status(500, text="internal server error")
    script.queue_status(500, text="internal server error")
    provider = _openrouter(script, monkeypatch)
    sink = _RecordingEventSink()
    chain = _chain(provider, _model_config(model), sink=sink, max_attempts=2)

    with pytest.raises(ProviderChainExhausted):
        chain.complete_step(_request(model), run_id=_RUN_ID)
    assert len(sink.of_type(RunEventType.MODEL_CHAIN_EXHAUSTED)) == 1


# ==========================================================================
# P7 -- cost from usage, never independently priced
# ==========================================================================


def test_p7_fake_cost_passed_through_untouched() -> None:
    from civsim_harness.models.common import Cost

    fake = FakeModelProvider()
    model = _model("primary-a")
    cost = Cost(input_tokens=10, output_tokens=5, total_tokens=15, amount_usd=0.002)
    fake.queue_decision(_raw_decision(), cost=cost)
    chain = _chain(fake, _model_config(model))

    response = chain.complete_step(_request(model), run_id=_RUN_ID)
    assert response.cost == cost


def test_p7_openrouter_cost_from_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _OpenRouterScript()
    model = _model("anthropic/claude-sonnet-5")
    script.queue_decision_response(
        usage={"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240, "cost": 0.01}
    )
    provider = _openrouter(script, monkeypatch)

    response = provider.complete(_request(model))
    assert response.cost.input_tokens == 200
    assert response.cost.amount_usd == pytest.approx(0.01)


# ==========================================================================
# P8 / T181 -- no credentials anywhere
# ==========================================================================


def test_t181_planted_key_never_appears_in_events_or_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _OpenRouterScript()
    model = _model("anthropic/claude-sonnet-5")
    # One retryable failure, then a success -- exercises failure/retry event detail too.
    script.queue_status(429, json={"error": {"message": "rate limited"}})
    script.queue_decision_response()
    provider = _openrouter(script, monkeypatch)
    sink = _RecordingEventSink()
    chain = _chain(provider, _model_config(model), sink=sink, max_attempts=2)

    chain.complete_step(_request(model), run_id=_RUN_ID)

    for event in sink.events:
        assert _FAKE_KEY not in json.dumps(event.detail, default=str)
        assert _FAKE_KEY not in repr(event)


def test_t181_planted_key_never_appears_when_the_chain_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _OpenRouterScript()
    model = _model("anthropic/claude-sonnet-5")
    script.queue_status(500, text="boom")
    script.queue_status(500, text="boom")
    provider = _openrouter(script, monkeypatch)
    sink = _RecordingEventSink()
    chain = _chain(provider, _model_config(model), sink=sink, max_attempts=2)

    with pytest.raises(ProviderChainExhausted) as excinfo:
        chain.complete_step(_request(model), run_id=_RUN_ID)

    assert _FAKE_KEY not in str(excinfo.value)
    assert _FAKE_KEY not in repr(excinfo.value)
    for event in sink.events:
        assert _FAKE_KEY not in json.dumps(event.detail, default=str)


# ==========================================================================
# P9 -- model choice is configuration (see also tests/unit/test_model_swap.py, T182)
# ==========================================================================


def test_p9_changing_only_model_config_changes_which_model_is_asked() -> None:
    fake = FakeModelProvider()
    fake.queue_decision(_raw_decision())
    fake.queue_decision(_raw_decision())

    chain_a = _chain(fake, _model_config(_model("vendor-a/model-a")))
    chain_a.complete_step(_request(_model("vendor-a/model-a")), run_id=_RUN_ID)

    chain_b = _chain(fake, _model_config(_model("vendor-b/model-b")))
    chain_b.complete_step(_request(_model("vendor-b/model-b")), run_id=_RUN_ID)

    assert fake.calls[0].model == _model("vendor-a/model-a")
    assert fake.calls[1].model == _model("vendor-b/model-b")


# ==========================================================================
# P10 / T180 -- one call, one decision
# ==========================================================================


def test_t180_fake_multi_decision_raises_and_is_not_reconciled() -> None:
    fake = FakeModelProvider()
    model = _model("primary-a")
    fake.queue_multi_decision([_raw_decision("first"), _raw_decision("second")])
    chain = _chain(fake, _model_config(model))

    with pytest.raises(ProviderContractViolation):
        chain.complete_step(_request(model), run_id=_RUN_ID)

    # Not silently dropped, not queued for a later call, not retried into a fallback.
    assert len(fake.calls) == 1


def test_t180_openrouter_multi_decision_raises_and_is_not_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _OpenRouterScript()
    model = _model("anthropic/claude-sonnet-5")
    script.queue_multi_decision_response()
    provider = _openrouter(script, monkeypatch)
    chain = _chain(provider, _model_config(model))

    with pytest.raises(MultipleDecisionsError):
        chain.complete_step(_request(model), run_id=_RUN_ID)
    assert script.chat_call_count == 1


# ==========================================================================
# P11 -- no wall-clock coupling to the turn
# ==========================================================================


def test_p11_complete_step_signature_carries_no_turn_deadline() -> None:
    signature = inspect.signature(ProviderChain.complete_step)
    params = set(signature.parameters) - {"self"}
    assert params == {"request", "run_id", "turn_number"}
    for forbidden in ("deadline", "budget", "timeout", "elapsed", "started_at"):
        assert forbidden not in params


def test_p11_retry_policy_carries_no_total_or_turn_level_bound() -> None:
    fields = {f for f in RetryPolicy.__dataclass_fields__}
    for forbidden in ("turn_budget", "total_timeout", "deadline", "max_turn_duration"):
        assert forbidden not in fields


# ==========================================================================
# Adapter obligations (contract "Adapter obligations"; T188)
# ==========================================================================


class _ImageMutatingProvider:
    """A deliberately-broken adapter: appends to ``request.images`` in place."""

    def describe(self, model: ModelRef) -> ModelCapabilities:
        return DEFAULT_CAPABILITIES

    def complete(self, request: DecisionRequest) -> Any:
        request.images.append(Image(media_type="image/png", data=b"smuggled"))
        fake = FakeModelProvider()
        fake.queue_decision(_raw_decision())
        return fake.complete(request)


def test_obligation_wrapper_passes_through_a_well_behaved_adapter() -> None:
    fake = FakeModelProvider()
    fake.queue_decision(_raw_decision())
    wrapped = ObligationEnforcingProvider(fake)
    response = wrapped.complete(_request(_model("primary-a")))
    assert response.decision is not None


def test_obligation_wrapper_catches_image_mutation() -> None:
    wrapped = ObligationEnforcingProvider(_ImageMutatingProvider())
    with pytest.raises(AdapterObligationViolation):
        wrapped.complete(_request(_model("primary-a")))


def test_obligation_wrapper_catches_decision_none_with_decision_returned_outcome() -> None:
    class _LyingProvider:
        def describe(self, model: ModelRef) -> ModelCapabilities:
            return DEFAULT_CAPABILITIES

        def complete(self, request: DecisionRequest) -> Any:
            fake = FakeModelProvider()
            fake.queue_decision(_raw_decision())
            response = fake.complete(request)
            import dataclasses

            return dataclasses.replace(response, decision=None)

    wrapped = ObligationEnforcingProvider(_LyingProvider())
    with pytest.raises(AdapterObligationViolation):
        wrapped.complete(_request(_model("primary-a")))
