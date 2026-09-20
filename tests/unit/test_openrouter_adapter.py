"""Unit tests for the OpenRouter adapter's ``complete()`` (T105).

No live network access or real API key is used anywhere here: every test
injects an ``httpx.Client`` built on ``httpx.MockTransport`` per the task's
instruction to test against a mocked transport rather than the live API.
"""

from __future__ import annotations

import base64
import inspect
import json
from pathlib import Path

import httpx
import pytest

from civsim_harness.agent.decisions import RESPONSE_SCHEMA, MultipleDecisionsError
from civsim_harness.models.common import ModelRef
from civsim_harness.models.records import CallOutcome
from civsim_harness.provider.openrouter import (
    CredentialResolutionError,
    OpenRouterProvider,
    _resolve_api_key,
)
from civsim_harness.provider.port import DecisionRequest, Image

_MODEL = ModelRef(provider="openrouter", model="anthropic/claude-opus-5")
_FAKE_KEY = "sk-or-v1-TOTALLYFAKESECRETVALUE1234567890"

_VALID_DECISION_CONTENT = json.dumps(
    {
        "action_declaration_id": "units.move_to",
        "reasoning": "Scout the nearby hills.",
        "parameters": {"unit_id": "unit-1"},
    }
)


def _request(
    *, images: list[Image] | None = None, response_schema: dict | None = None
) -> DecisionRequest:
    return DecisionRequest(
        model=_MODEL,
        system="You are the sole player of Civilization VI.",
        observation="Current screen: WorldScreen",
        images=images if images is not None else [],
        step_index=1,
        response_schema=response_schema if response_schema is not None else RESPONSE_SCHEMA,
    )


def _json_response(status_code: int, body: dict) -> httpx.Response:
    return httpx.Response(status_code, json=body)


def _provider_with_handler(handler, monkeypatch: pytest.MonkeyPatch) -> OpenRouterProvider:
    monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://openrouter.ai/api/v1")
    return OpenRouterProvider(client=client)


# --------------------------------------------------------------------------
# Successful decision
# --------------------------------------------------------------------------


def test_successful_response_returns_decision_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            200,
            {
                "model": "anthropic/claude-opus-5-20260101",
                "choices": [{"message": {"role": "assistant", "content": _VALID_DECISION_CONTENT}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "cost": 0.0034,
                },
            },
        )

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request())

    assert response.outcome == CallOutcome.DECISION_RETURNED
    assert response.decision is not None
    assert response.decision.action_declaration_id == "units.move_to"
    assert response.model_served == ModelRef(
        provider="openrouter", model="anthropic/claude-opus-5-20260101"
    )
    assert response.cost.input_tokens == 100
    assert response.cost.output_tokens == 20
    assert response.cost.total_tokens == 120
    assert response.cost.amount_usd == pytest.approx(0.0034)
    assert response.retry_count == 0
    assert response.fallback_occurred is False
    assert response.image_count == 0
    assert response.latency_ms >= 0


def test_model_served_falls_back_to_requested_model_if_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            200,
            {"choices": [{"message": {"content": _VALID_DECISION_CONTENT}}]},
        )

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request())
    assert response.model_served == _MODEL


# --------------------------------------------------------------------------
# Empty response (P4) -- a failed call, never a decision to do nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "{}", "null", "not json"])
def test_empty_or_malformed_content_maps_to_empty_response(
    content: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"choices": [{"message": {"content": content}}]})

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request())

    assert response.outcome == CallOutcome.EMPTY_RESPONSE
    assert response.decision is None


def test_no_choices_at_all_maps_to_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"choices": []})

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request())
    assert response.outcome == CallOutcome.EMPTY_RESPONSE
    assert response.decision is None


# --------------------------------------------------------------------------
# Multi-decision (P10) -- must raise, never be reconciled into an outcome
# --------------------------------------------------------------------------


def test_multi_decision_response_raises_and_is_not_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    two_decisions = json.dumps(
        [
            json.loads(_VALID_DECISION_CONTENT),
            {**json.loads(_VALID_DECISION_CONTENT), "reasoning": "a second decision"},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"choices": [{"message": {"content": two_decisions}}]})

    provider = _provider_with_handler(handler, monkeypatch)
    with pytest.raises(MultipleDecisionsError):
        provider.complete(_request())


# --------------------------------------------------------------------------
# HTTP-level outcomes
# --------------------------------------------------------------------------


def test_rate_limit_status_maps_to_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request())
    assert response.outcome == CallOutcome.RATE_LIMITED
    assert response.decision is None


def test_server_error_maps_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request())
    assert response.outcome == CallOutcome.FAILED
    assert response.decision is None


def test_context_length_error_maps_to_context_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "This model's maximum context length is exceeded."}},
        )

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request())
    assert response.outcome == CallOutcome.CONTEXT_REJECTED


def test_network_error_maps_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request())
    assert response.outcome == CallOutcome.FAILED
    assert response.decision is None


# --------------------------------------------------------------------------
# No image dropping (P2) -- image_count always equals len(request.images)
# --------------------------------------------------------------------------


def test_image_count_matches_request_images_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    images = [
        Image(media_type="image/png", data=b"fake-1"),
        Image(media_type="image/png", data=b"fake-2"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"choices": [{"message": {"content": _VALID_DECISION_CONTENT}}]})

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request(images=images))
    assert response.image_count == 2


def test_image_count_matches_request_images_even_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    images = [Image(media_type="image/png", data=b"fake-1")]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request(images=images))
    assert response.image_count == 1


def test_payload_carries_one_image_url_part_per_image(monkeypatch: pytest.MonkeyPatch) -> None:
    images = [
        Image(media_type="image/png", data=b"one"),
        Image(media_type="image/jpeg", data=b"two"),
    ]
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return _json_response(200, {"choices": [{"message": {"content": _VALID_DECISION_CONTENT}}]})

    provider = _provider_with_handler(handler, monkeypatch)
    provider.complete(_request(images=images))

    user_message = captured["body"]["messages"][1]
    assert user_message["role"] == "user"
    image_parts = [part for part in user_message["content"] if part["type"] == "image_url"]
    assert len(image_parts) == 2
    assert image_parts[0]["image_url"]["url"] == (
        f"data:image/png;base64,{base64.b64encode(b'one').decode('ascii')}"
    )
    assert image_parts[1]["image_url"]["url"] == (
        f"data:image/jpeg;base64,{base64.b64encode(b'two').decode('ascii')}"
    )


def test_no_images_means_no_image_url_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return _json_response(200, {"choices": [{"message": {"content": _VALID_DECISION_CONTENT}}]})

    provider = _provider_with_handler(handler, monkeypatch)
    provider.complete(_request(images=[]))

    user_message = captured["body"]["messages"][1]
    assert all(part["type"] != "image_url" for part in user_message["content"])


# --------------------------------------------------------------------------
# Credentials (P8) -- resolve at call time, never leak
# --------------------------------------------------------------------------


def test_resolve_api_key_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
    monkeypatch.delenv("CIVSIM_SECRETS_FILE", raising=False)
    assert _resolve_api_key() == _FAKE_KEY


def test_resolve_api_key_reads_secrets_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text(f"openrouter_api_key: {_FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("CIVSIM_SECRETS_FILE", str(secrets_file))
    assert _resolve_api_key() == _FAKE_KEY


def test_missing_credential_raises_credential_resolution_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Point the file source at a path that cannot exist instead of delenv-ing
    # it: with no override the resolver falls back to Path.cwd()/secrets.yaml,
    # so a real operator key in the repo root would satisfy this test's
    # "missing" premise. _read_secrets_file returns {} for an absent file.
    monkeypatch.setenv("CIVSIM_SECRETS_FILE", str(tmp_path / "absent.yaml"))
    with pytest.raises(CredentialResolutionError):
        _resolve_api_key()


def test_missing_credential_error_message_contains_no_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("CIVSIM_SECRETS_FILE", str(tmp_path / "absent.yaml"))
    with pytest.raises(CredentialResolutionError) as excinfo:
        _resolve_api_key()
    assert _FAKE_KEY not in str(excinfo.value)


def test_credential_sent_on_wire_but_never_in_returned_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        return _json_response(200, {"choices": [{"message": {"content": _VALID_DECISION_CONTENT}}]})

    provider = _provider_with_handler(handler, monkeypatch)
    response = provider.complete(_request())

    # The key really was sent -- this adapter's job is to send it on the wire.
    assert _FAKE_KEY in captured_headers.get("authorization", "")
    # But it must never leak into anything the harness might log, store, or raise.
    assert _FAKE_KEY not in repr(response)
    assert _FAKE_KEY not in str(response)


def test_credential_never_appears_in_a_raised_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    two_decisions = json.dumps(
        [json.loads(_VALID_DECISION_CONTENT), json.loads(_VALID_DECISION_CONTENT)]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"choices": [{"message": {"content": two_decisions}}]})

    provider = _provider_with_handler(handler, monkeypatch)
    with pytest.raises(MultipleDecisionsError) as excinfo:
        provider.complete(_request())
    assert _FAKE_KEY not in str(excinfo.value)
    assert _FAKE_KEY not in repr(excinfo.value)


# --------------------------------------------------------------------------
# describe() -- fails closed when the models endpoint can't confirm a model (T183)
# --------------------------------------------------------------------------


def test_describe_fails_closed_when_the_model_is_absent_from_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
    provider = OpenRouterProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            base_url="https://openrouter.ai/api/v1",
        )
    )
    capabilities = provider.describe(_MODEL)
    assert capabilities.confirmed is False


# --------------------------------------------------------------------------
# No vendor SDK on the decision path (FR-037); no turn-level timeout (P11)
# --------------------------------------------------------------------------


def test_no_vendor_sdk_is_imported() -> None:
    source = Path("src/civsim_harness/provider/openrouter.py").read_text(encoding="utf-8")
    for forbidden in ("import openai", "import anthropic", "from openai", "from anthropic"):
        assert forbidden not in source


def test_complete_signature_carries_no_turn_level_state() -> None:
    signature = inspect.signature(OpenRouterProvider.complete)
    params = list(signature.parameters)
    assert params == ["self", "request"]
