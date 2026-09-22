"""The default ``ModelProvider`` adapter: OpenRouter over ``httpx`` (T105).

contracts/model-provider-port.md is normative; this module is its
"OpenRouter adapter specifics" section made real. **No vendor SDK appears on
the decision path** (FR-037): the only third-party import here is ``httpx``
(plain HTTP) -- never ``openai``, ``anthropic``, or any other vendor client.
Secrets-file parsing (``PyYAML``) now lives entirely in ``config/secrets.py``,
which this module delegates to (see ``_resolve_api_key`` below).

``complete()`` serves exactly **one** call for exactly **one** decision step.
It does not retry and does not fall back to another model in a chain --
that ladder is ``provider/chain.py`` (T155, a later wave), which wraps this
adapter and decides, from the ``CallOutcome`` a single ``complete()`` call
returns, whether to retry or move to the next model. Consequently this
adapter always reports ``retry_count=0`` and ``fallback_occurred=False``: it
is describing the one HTTP attempt it just made, nothing more.

**No timeout is coupled to the turn** (P11). The only clock here is the
per-request timeout passed to ``httpx``; nothing in this module knows how
long the enclosing turn has been running, and nothing here could cancel a
call on that basis even if it wanted to -- ``complete()``'s signature carries
no turn-level state at all, only a ``DecisionRequest`` scoped to one step.

**Credentials resolve at call time only** (FR-043, P8, T189). ``_resolve_api_key`` is invoked
fresh inside every ``complete()`` call -- never cached on the instance, never read at
construction, and never accepted as a constructor argument -- so a key rotated between calls (or
added to the environment after the process started) takes effect on the very next call. The
resolved key is used solely to build the ``Authorization`` header of the outgoing request; it is
never interpolated into a log line, a raised exception, or any field of the returned
``DecisionResponse``. ``_resolve_api_key`` now delegates entirely to
:func:`civsim_harness.config.secrets.require_secret` -- see that function's docstring for the
exact environment/secrets-file precedence -- rather than re-implementing the same lookup a
second time; this module retains only its own exception type (``CredentialResolutionError``) so
existing callers/tests keep the same observable failure mode.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Final

import httpx

from civsim_harness.agent.decisions import parse_decision
from civsim_harness.config.secrets import provider_key_name, require_secret
from civsim_harness.errors import HarnessError, PreflightError
from civsim_harness.models.common import Cost, ModelRef
from civsim_harness.models.records import CallOutcome
from civsim_harness.provider.liveness import provider_call_liveness
from civsim_harness.provider.port import (
    DecisionRequest,
    DecisionResponse,
    ModelCapabilities,
)

DEFAULT_BASE_URL: Final[str] = "https://openrouter.ai/api/v1"
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 120.0
DEFAULT_MODELS_PATH: Final[str] = "/models"

_OPENROUTER_SECRET_NAME: Final[str] = provider_key_name("openrouter")  # "openrouter_api_key"

_UNCONFIRMED_CAPABILITIES: Final[ModelCapabilities] = ModelCapabilities(
    accepts_images=False,
    max_context_tokens=0,
    max_images_per_request=None,
    confirmed=False,
)

_CONTEXT_REJECTION_KEYWORDS: Final[tuple[str, ...]] = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "does not support image",
    "does not support images",
    "image input is not supported",
    "modality",
    "vision is not supported",
)


class CredentialResolutionError(HarnessError):
    """No OpenRouter API key could be resolved from the environment or a secrets file (FR-043)."""


def _resolve_api_key() -> str:
    """Resolve the OpenRouter API key at call time only (FR-043, P8, T189).

    Delegates entirely to :func:`civsim_harness.config.secrets.require_secret` under the
    secret name ``"openrouter_api_key"`` (the same environment-variable name,
    ``OPENROUTER_API_KEY``, and secrets-file key this function has always used -- delegating
    changes nothing about where the key comes from, only who implements the lookup). The raw
    value is returned to the caller for immediate use in one request's headers and is never
    stored, logged, or embedded in any exception this module raises: a missing secret is
    translated from ``config.secrets``'s ``PreflightError`` into this module's own
    ``CredentialResolutionError`` so existing callers keep seeing the same exception type.
    """
    try:
        return require_secret(_OPENROUTER_SECRET_NAME)
    except PreflightError as exc:
        raise CredentialResolutionError(
            "no OpenRouter API key found in the environment or a secrets file (FR-043)"
        ) from exc


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class OpenRouterProvider:
    """The default ``ModelProvider`` adapter: OpenRouter's OpenAI-compatible chat
    completions endpoint, spoken over plain ``httpx`` (contracts/model-provider-port.md).

    Inject ``client`` (an ``httpx.Client`` built with a ``transport=httpx.MockTransport(...)``)
    to test this adapter without a live key or network access; the default constructs a real
    client against ``base_url``.

    ``describe()`` is intentionally a conservative placeholder: reading OpenRouter's live models
    endpoint for modality/context-length data is T183 (a later US5 task), out of this task's
    scope. Until that lands, ``describe()`` always reports ``confirmed=False`` -- the fail-closed
    answer P1 requires for an unconfirmed model, so anything built against it today can only ever
    get a *safe* answer, never an incorrectly-confirmed one.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE_URL,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(base_url=base_url)
        self._timeout_s = request_timeout_s

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenRouterProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ----------------------------------------------------------------
    # ModelProvider protocol
    # ----------------------------------------------------------------

    def describe(self, model: ModelRef) -> ModelCapabilities:
        """Report *model*'s capabilities by reading OpenRouter's models endpoint (R10, T183).

        Every field maps from that endpoint's own reported data for *model* -- modality and
        context length are never hard-coded per model here, since "the roster moves faster than
        this repo" (research R10). Any failure to reach the endpoint, find *model* in its
        listing, or parse the fields needed maps to ``confirmed=False`` with the most
        conservative possible values for the rest: P1's chain preflight fails a run closed on an
        unconfirmable model, never open.
        """
        try:
            response = self._client.get(DEFAULT_MODELS_PATH, timeout=self._timeout_s)
        except httpx.HTTPError:
            return _UNCONFIRMED_CAPABILITIES

        if response.status_code != 200:
            return _UNCONFIRMED_CAPABILITIES

        try:
            body = response.json()
        except ValueError:
            return _UNCONFIRMED_CAPABILITIES

        entry = self._find_model_entry(body, model.model)
        if entry is None:
            return _UNCONFIRMED_CAPABILITIES

        return self._capabilities_from_entry(entry)

    def complete(self, request: DecisionRequest) -> DecisionResponse:
        """Serve exactly one decision for *request*'s single decision step.

        Makes exactly one HTTP request with a per-request timeout (P11) and
        no retry of its own (that ladder is T155). ``MultipleDecisionsError``
        from :func:`civsim_harness.agent.decisions.parse_decision` is allowed
        to propagate uncaught -- P10 requires the adapter to surface a
        multi-decision response as a raised contract violation, never to
        reconcile it into a normal outcome.
        """
        payload = self._build_payload(request)
        headers = self._build_headers()

        start = time.monotonic()
        # T290: this `post` is the longest silent window in the harness -- up to
        # `self._timeout_s` (120 s by default) with a blocking call and no progress callback,
        # and MEASURED at 146 s on the live driver logs of 2026-09-22 with not one line
        # published in between. The bound is unchanged; what changes is that the wait now
        # publishes what it is waiting for and for how long, so a healthy slow call stops
        # reading like a dead process to a watchdog. See `provider/liveness.py`.
        with provider_call_liveness(
            provider=request.model.provider,
            model=request.model.model,
            step_index=request.step_index,
            bound_s=self._timeout_s,
        ):
            try:
                response = self._client.post(
                    "/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout_s,
                )
            except httpx.HTTPError:
                return self._outcome_only_response(
                    request, self._elapsed_ms(start), CallOutcome.FAILED
                )

        latency_ms = self._elapsed_ms(start)

        if response.status_code == 429:
            return self._outcome_only_response(request, latency_ms, CallOutcome.RATE_LIMITED)

        if response.status_code >= 400:
            return self._outcome_only_response(
                request, latency_ms, self._classify_error_outcome(response)
            )

        try:
            body = response.json()
        except ValueError:
            return self._outcome_only_response(request, latency_ms, CallOutcome.EMPTY_RESPONSE)

        if not isinstance(body, dict):
            return self._outcome_only_response(request, latency_ms, CallOutcome.EMPTY_RESPONSE)

        content = self._extract_content(body)
        # May raise MultipleDecisionsError (P10) -- must propagate, not be caught here.
        decision = parse_decision(content)

        outcome = (
            CallOutcome.DECISION_RETURNED if decision is not None else CallOutcome.EMPTY_RESPONSE
        )

        return DecisionResponse(
            decision=decision,
            model_served=self._extract_model_served(request.model, body),
            latency_ms=latency_ms,
            cost=self._extract_cost(body),
            retry_count=0,
            fallback_occurred=False,
            image_count=len(request.images),
            outcome=outcome,
        )

    # ----------------------------------------------------------------
    # Request construction
    # ----------------------------------------------------------------

    def _build_payload(self, request: DecisionRequest) -> dict[str, Any]:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": request.observation}]
        for image in request.images:
            encoded = base64.b64encode(image.data).decode("ascii")
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.media_type};base64,{encoded}"},
                }
            )

        payload: dict[str, Any] = {
            "model": request.model.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": user_content},
            ],
        }
        if request.response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "civsim_decision",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        return payload

    def _build_headers(self) -> dict[str, str]:
        api_key = _resolve_api_key()
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    # ----------------------------------------------------------------
    # Response parsing
    # ----------------------------------------------------------------

    def _extract_content(self, body: dict[str, Any]) -> str | None:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        message = first.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            return "".join(parts)
        return None

    def _extract_cost(self, body: dict[str, Any]) -> Cost:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return Cost()
        return Cost(
            input_tokens=_as_int(usage.get("prompt_tokens")),
            output_tokens=_as_int(usage.get("completion_tokens")),
            total_tokens=_as_int(usage.get("total_tokens")),
            amount_usd=_as_float(usage.get("cost")),
        )

    def _extract_model_served(self, requested: ModelRef, body: dict[str, Any]) -> ModelRef:
        served_model = body.get("model")
        if isinstance(served_model, str) and served_model:
            return ModelRef(provider=requested.provider, model=served_model)
        return requested

    def _classify_error_outcome(self, response: httpx.Response) -> CallOutcome:
        if 400 <= response.status_code < 500:
            try:
                text = response.text.lower()
            except Exception:  # pragma: no cover - defensive, decoding failure
                text = ""
            if any(keyword in text for keyword in _CONTEXT_REJECTION_KEYWORDS):
                return CallOutcome.CONTEXT_REJECTED
        return CallOutcome.FAILED

    def _elapsed_ms(self, start: float) -> int:
        return max(0, round((time.monotonic() - start) * 1000))

    def _outcome_only_response(
        self, request: DecisionRequest, latency_ms: int, outcome: CallOutcome
    ) -> DecisionResponse:
        return DecisionResponse(
            decision=None,
            model_served=request.model,
            latency_ms=latency_ms,
            cost=Cost(),
            retry_count=0,
            fallback_occurred=False,
            image_count=len(request.images),
            outcome=outcome,
        )

    # ----------------------------------------------------------------
    # describe() support (R10, T183)
    # ----------------------------------------------------------------

    @staticmethod
    def _find_model_entry(body: Any, model_id: str) -> dict[str, Any] | None:
        """Find *model_id*'s own entry in the models endpoint's ``{"data": [...]}`` body."""
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return None
        for entry in data:
            if isinstance(entry, dict) and entry.get("id") == model_id:
                return entry
        return None

    @classmethod
    def _capabilities_from_entry(cls, entry: dict[str, Any]) -> ModelCapabilities:
        """Map one models-endpoint entry to ``ModelCapabilities``; unconfirmable ⇒ closed."""
        context_length = cls._extract_context_length(entry)
        if context_length is None:
            return _UNCONFIRMED_CAPABILITIES

        accepts_images = cls._extract_accepts_images(entry)
        if accepts_images is None:
            return _UNCONFIRMED_CAPABILITIES

        max_images = entry.get("max_images_per_request")
        max_images_per_request = (
            max_images if isinstance(max_images, int) and max_images > 0 else None
        )

        return ModelCapabilities(
            accepts_images=accepts_images,
            max_context_tokens=context_length,
            max_images_per_request=max_images_per_request,
            confirmed=True,
        )

    @staticmethod
    def _extract_context_length(entry: dict[str, Any]) -> int | None:
        context_length = entry.get("context_length")
        if isinstance(context_length, int) and context_length > 0:
            return context_length
        top_provider = entry.get("top_provider")
        if isinstance(top_provider, dict):
            candidate = top_provider.get("context_length")
            if isinstance(candidate, int) and candidate > 0:
                return candidate
        return None

    @staticmethod
    def _extract_accepts_images(entry: dict[str, Any]) -> bool | None:
        architecture = entry.get("architecture")
        if not isinstance(architecture, dict):
            return None
        input_modalities = architecture.get("input_modalities")
        if isinstance(input_modalities, list):
            return any(
                isinstance(modality, str) and modality.lower() == "image"
                for modality in input_modalities
            )
        modality = architecture.get("modality")
        if isinstance(modality, str):
            input_side = modality.split("->", 1)[0]
            return "image" in input_side.lower()
        return None


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODELS_PATH",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "CredentialResolutionError",
    "OpenRouterProvider",
]
