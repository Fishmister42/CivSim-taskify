"""Model swap is configuration-only (T182, FR-038, P9, SC-015).

Two independent claims, both required by "changing which model plays is a change to run
configuration and nothing else" (Principle VII):

1. **No model identifier appears anywhere in the harness's own source outside
   `RunConfiguration.model_config`.** Enforced here by an AST scan over `src/civsim_harness`
   for any `ModelRef(model=<string literal>)` construction -- the one shape that would let a
   model name leak into code rather than configuration. A dynamically-built `ModelRef` (e.g.
   `ModelRef(provider=requested.provider, model=served_model)`, where `served_model` is a
   variable holding whatever a provider actually reported) is not a violation; only a literal
   is.
2. **A swap is exercised, not just asserted absent.** The same code path (`ProviderChain` over
   `OpenRouterProvider`) is driven twice, changing only the `ModelRef` passed in, routing
   through two distinct *vendors* rather than two models from the same one -- discharging
   SC-015's "verified on at least two providers" as quickstart.md Scenario 6 specifies
   (`anthropic/claude-sonnet-5` and `google/gemini-3-pro`, both via OpenRouter). No live network
   access or real API key is used; `httpx.MockTransport` echoes back whichever model was
   requested as the model that "served" it, which is enough to prove the record correctly names
   it -- the substance of this test is the *code path*, not the live model roster.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

from civsim_harness.models.common import DecisionStepId, ModelCallId, ModelRef, RunId, TurnCycleId
from civsim_harness.models.config import ModelConfig
from civsim_harness.models.records import ModelCall
from civsim_harness.provider.accounting import record_model_call
from civsim_harness.provider.chain import ProviderChain
from civsim_harness.provider.openrouter import OpenRouterProvider
from civsim_harness.provider.port import DecisionRequest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "civsim_harness"
_FAKE_KEY = "sk-or-v1-TOTALLYFAKESECRETVALUEFORSWAPTEST"


# --------------------------------------------------------------------------
# 1. No hardcoded model identifier outside RunConfiguration.model_config
# --------------------------------------------------------------------------


def _model_ref_literal_violations(tree: ast.AST, filename: str) -> list[str]:
    """Every `ModelRef(model=<string literal>)` call site in *tree*, by (filename, lineno)."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if called_name != "ModelRef":
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "model"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                violations.append(
                    f"{filename}:{node.lineno}: ModelRef(model={keyword.value.value!r}) is a "
                    "hardcoded model identifier -- model identifiers may only ever come from "
                    "RunConfiguration.model_config, never a literal in source (FR-038, P9)"
                )
    return violations


def test_no_hardcoded_model_identifier_anywhere_in_source() -> None:
    violations: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(
            _model_ref_literal_violations(tree, str(path.relative_to(_SRC_ROOT)))
        )
    assert violations == [], "\n" + "\n".join(violations)


# --------------------------------------------------------------------------
# 2. Two distinct vendors, one unchanged code path
# --------------------------------------------------------------------------


class _EchoTransportScript:
    """Answers every chat-completion call by echoing back whichever model was requested as the
    model that served it -- enough to prove ``model_served`` is faithfully recorded without any
    live network access or a real roster.
    """

    def __init__(self) -> None:
        self.chat_call_count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.chat_call_count += 1
        payload = json.loads(request.content)
        requested_model = payload["model"]
        content = json.dumps(
            {
                "action_declaration_id": "units.move_to",
                "reasoning": f"decision for {requested_model}",
            }
        )
        return httpx.Response(
            200,
            json={
                "model": requested_model,
                "choices": [{"message": {"content": content}}],
            },
        )


class _NullEventSink:
    def write_run_event(self, event: object) -> str:
        return ""


class _RecordingModelCallStore:
    def __init__(self) -> None:
        self.calls: list[ModelCall] = []

    def write_model_call(self, call: ModelCall) -> ModelCallId:
        self.calls.append(call)
        return call.model_call_id


def _request(model: ModelRef, *, step_index: int) -> DecisionRequest:
    return DecisionRequest(
        model=model,
        system="You are the sole player of Civilization VI.",
        observation="Current screen: WorldScreen",
        images=[],
        step_index=step_index,
        response_schema={"type": "object"},
    )


def _run_seed_through_model(
    primary: ModelRef, *, monkeypatch: pytest.MonkeyPatch
) -> list[ModelCall]:
    """The one code path under test: build a chain over the OpenRouter adapter for *primary*,
    and drive three decision steps through it. Called twice with only *primary* varying --
    everything else (this function, `ProviderChain`, `OpenRouterProvider`, `record_model_call`)
    is byte-for-byte identical between the two calls, which is what "no code or integration
    change between the two" (Phase 7's Independent Test) means in practice.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
    script = _EchoTransportScript()
    client = httpx.Client(
        transport=httpx.MockTransport(script.handler), base_url="https://openrouter.ai/api/v1"
    )
    provider = OpenRouterProvider(client=client)
    model_config = ModelConfig(primary=primary, fallbacks=[])
    chain = ProviderChain(
        provider, model_config, event_sink=_NullEventSink(), sleep=lambda _delay: None
    )
    call_store = _RecordingModelCallStore()

    run_id = RunId("run-swap")
    turn_cycle_id = TurnCycleId("tc-1")
    calls: list[ModelCall] = []
    for step_index in range(1, 4):
        request = _request(primary, step_index=step_index)
        response = chain.complete_step(request, run_id=run_id)
        assert response.decision is not None
        call = record_model_call(
            call_store,
            model_call_id=ModelCallId(f"call-{step_index}"),
            run_id=run_id,
            turn_cycle_id=turn_cycle_id,
            decision_step_id=DecisionStepId(f"step-{step_index}"),
            model_requested=primary,
            request=request,
            response=response,
        )
        calls.append(call)

    assert script.chat_call_count == 3
    return calls


def test_two_distinct_vendors_both_complete_and_name_their_serving_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anthropic_model = ModelRef(provider="openrouter", model="anthropic/claude-sonnet-5")
    google_model = ModelRef(provider="openrouter", model="google/gemini-3-pro")

    anthropic_calls = _run_seed_through_model(anthropic_model, monkeypatch=monkeypatch)
    google_calls = _run_seed_through_model(google_model, monkeypatch=monkeypatch)

    # Both configurations completed every step.
    assert len(anthropic_calls) == 3
    assert len(google_calls) == 3

    # Every call names the model that actually served it.
    assert all(call.model_served == anthropic_model for call in anthropic_calls)
    assert all(call.model_served == google_model for call in google_calls)

    # Two distinct vendors, not two models from the same one (SC-015).
    served_vendors = {
        call.model_served.model.split("/", 1)[0] for call in (*anthropic_calls, *google_calls)
    }
    assert served_vendors == {"anthropic", "google"}
