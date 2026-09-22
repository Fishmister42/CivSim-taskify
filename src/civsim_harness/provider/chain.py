"""Retry, fallback, and the no-image-drop rule for the provider chain (T155, T156, T185).

contracts/model-provider-port.md P5/P6/P2/P11 are what this module discharges, wrapping a
single :class:`~civsim_harness.provider.port.ModelProvider` adapter (which can reach *any*
model by name -- OpenRouter's one endpoint serves every vendor it lists) with the harness's
own retry-then-fallback policy, since P10's "Adapter obligations" forbids an adapter from
retrying or falling back on its own.

**P5 -- retry then fall back, every attempt recorded.** A transient failure
(``CallOutcome.RATE_LIMITED``, ``FAILED``, or ``EMPTY_RESPONSE``) retries the *same* model with
exponential backoff and jitter up to :attr:`RetryPolicy.max_attempts_per_model` times, then
falls back to the next model in ``model_config.fallbacks``. ``CallOutcome.CONTEXT_REJECTED`` is
different in kind: retrying the same model can never fix a context-length or modality refusal,
so it is never retried in place -- it moves straight to fallback (or exhaustion), exactly as the
contract's "OpenRouter adapter specifics" section describes. Fallback is decided **per call**
(``complete_step`` always starts over at ``model_config.primary``), so a single turn may
legitimately be served by more than one model across its steps -- this class carries no memory
of a previous step's outcome, matching "Adapter obligations"'s "no state carried between calls".

**P6 -- exhaustion has no escape hatch.** Once every model in the chain has failed,
``complete_step`` records a ``model_chain_exhausted`` event and raises
:class:`~civsim_harness.errors.ProviderChainExhausted`. There is no fabricate, skip, or
default-move path anywhere in this module -- the run pausing in a recorded state is
``run/runner.py``'s job (a concurrent wave, T156's other half), reached only by letting this
exception propagate uncaught.

**P2 / T185 -- no image is ever dropped to make a call fit.** ``_assert_image_count`` runs after
*every* ``provider.complete()`` call, success or failure, before any retry/fallback decision is
made: ``response.image_count != len(request.images)`` raises :class:`ImageCountMismatch`
immediately, uncaught by anything in this module. There is no code path here -- or anywhere --
that could reduce ``request.images`` and retry; the request handed to each attempt is the exact
same request, only ``model`` varies (via :func:`dataclasses.replace`).

**P11 -- no wall-clock coupling to the turn.** The only clock touched here is the delay *between
retries of the same call*, driven entirely by ``retry_policy``. ``complete_step`` takes a
``run_id``/``turn_number`` for event attribution only, never a deadline; nothing here measures
how long the enclosing turn has been running, and nothing here could cancel a call on that basis.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from civsim_harness.errors import HarnessError, ProviderChainExhausted
from civsim_harness.models.common import EventId, ModelRef, RunId, Timestamp
from civsim_harness.models.config import ModelConfig
from civsim_harness.models.records import CallOutcome, RunEvent, RunEventType
from civsim_harness.provider.liveness import (
    PROVIDER_CHAIN_RETRY,
    emit_provider_liveness,
)
from civsim_harness.provider.port import DecisionRequest, DecisionResponse, ModelProvider


def _utcnow() -> Timestamp:
    return datetime.now(UTC)


def _model_name(model: ModelRef) -> str:
    return f"{model.provider}/{model.model}"


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------


class RunEventSink(Protocol):
    """What :class:`ProviderChain` needs to record retry/fallback/exhaustion events.

    A narrower seam than the full ``MatchStore`` port -- mirrors
    ``resilience.recovery.SaveLoader``'s pattern of depending on only the one method actually
    needed, not the whole store, so this module has no import-time coupling to
    ``store/port.py``'s full surface. A real ``MatchStore`` satisfies this structurally (its
    ``write_run_event`` has exactly this shape).
    """

    def write_run_event(self, event: RunEvent) -> EventId: ...


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ImageCountMismatch(HarnessError):
    """A provider response's ``image_count`` did not equal ``len(request.images)`` (P2, T185).

    This is always a defect, never a degradation: there is no code path anywhere in this module,
    or in the harness at large, that removes images to make a call fit a context window. Raised
    immediately and uncaught -- a chain that cannot carry the full context must fail the run at
    preflight (``provider/preflight.py``, T184), not silently degrade a call after the fact
    (FR-039, invariant I7).
    """


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with jitter, applied per model (P5, FR-041).

    ``max_attempts_per_model`` counts the first attempt too: a value of ``3`` means up to two
    retries (three total tries) against one model before moving to the next. ``delay_for`` only
    computes a delay -- it never sleeps -- so a test can inject a ``sleep`` that records calls
    instead of actually blocking (see :class:`ProviderChain`'s ``sleep`` parameter).
    """

    max_attempts_per_model: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 20.0
    multiplier: float = 2.0
    jitter_s: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts_per_model < 1:
            raise ValueError("max_attempts_per_model must be >= 1")
        if self.base_delay_s < 0 or self.max_delay_s < 0 or self.jitter_s < 0:
            raise ValueError("delay parameters must be non-negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")

    def delay_for(self, attempt: int, *, rand: Callable[[], float]) -> float:
        """The backoff delay to wait after *attempt* (1-based) has just failed."""
        raw = self.base_delay_s * (self.multiplier ** (attempt - 1))
        capped = min(raw, self.max_delay_s)
        return capped + rand() * self.jitter_s


# --------------------------------------------------------------------------
# The chain
# --------------------------------------------------------------------------


class ProviderChain:
    """Retries and falls back across ``model_config.primary`` + ``.fallbacks`` for one
    decision step at a time.

    ``complete_step``'s returned ``DecisionResponse.retry_count`` is the total number of failed
    attempts across the *whole chain* before the call that finally succeeded (not merely the
    attempts against the model that ultimately served it) -- the most informative number for
    per-call accounting (T186), since it reflects everything this one decision step actually
    cost, not just its last leg. ``fallback_occurred`` is ``True`` whenever the serving attempt
    was not against ``model_config.primary``, regardless of what ``DecisionResponse.model_served``
    itself reports (which may additionally differ by version string, e.g. a dated snapshot --
    that is P3's concern, not P5's).
    """

    def __init__(
        self,
        provider: ModelProvider,
        model_config: ModelConfig,
        *,
        event_sink: RunEventSink,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        clock: Callable[[], Timestamp] = _utcnow,
    ) -> None:
        self._provider = provider
        self._model_config = model_config
        self._event_sink = event_sink
        self._retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self._sleep = sleep
        self._rand = rand
        self._clock = clock

    @property
    def models(self) -> list[ModelRef]:
        """The full chain in try-order: primary, then each fallback, in order."""
        return [self._model_config.primary, *self._model_config.fallbacks]

    def complete_step(
        self,
        request: DecisionRequest,
        *,
        run_id: RunId,
        turn_number: int | None = None,
    ) -> DecisionResponse:
        """Serve *request*'s one decision step, retrying and falling back per P5.

        Raises :class:`ImageCountMismatch` (P2) the instant any attempt's reported
        ``image_count`` disagrees with ``len(request.images)``, and
        :class:`~civsim_harness.errors.ProviderChainExhausted` (P6) once every model in the
        chain has failed -- both uncaught by anything in this method.
        """
        chain_models = self.models
        total_prior_attempts = 0
        failures: list[dict[str, Any]] = []

        for model_position, model in enumerate(chain_models):
            is_last_model = model_position == len(chain_models) - 1

            for attempt in range(1, self._retry_policy.max_attempts_per_model + 1):
                call_request = replace(request, model=model)
                response = self._provider.complete(call_request)
                self._assert_image_count(request, response)

                if response.outcome == CallOutcome.DECISION_RETURNED:
                    return replace(
                        response,
                        retry_count=total_prior_attempts,
                        fallback_occurred=model_position > 0,
                    )

                total_prior_attempts += 1
                failure_detail: dict[str, Any] = {
                    "model": _model_name(model),
                    "attempt": attempt,
                    "outcome": response.outcome.value,
                    "step_index": request.step_index,
                }
                failures.append(failure_detail)
                self._record_event(
                    RunEventType.PROVIDER_FAILURE,
                    run_id=run_id,
                    turn_number=turn_number,
                    step_index=request.step_index,
                    detail=failure_detail,
                )

                if response.outcome == CallOutcome.CONTEXT_REJECTED:
                    # A chain-level failure, never a retry (contract "OpenRouter adapter
                    # specifics") -- retrying the same model cannot fix a context or
                    # modality refusal.
                    self._record_event(
                        RunEventType.CONTEXT_REJECTED,
                        run_id=run_id,
                        turn_number=turn_number,
                        step_index=request.step_index,
                        detail=failure_detail,
                    )
                    break

                if attempt < self._retry_policy.max_attempts_per_model:
                    delay_s = self._retry_policy.delay_for(attempt, rand=self._rand)
                    self._record_event(
                        RunEventType.PROVIDER_RETRY,
                        run_id=run_id,
                        turn_number=turn_number,
                        step_index=request.step_index,
                        detail={**failure_detail, "delay_s": delay_s},
                    )
                    # T290: `_record_event` reaches the *store*, which a watchdog polling the
                    # driver log cannot see. This chain can legitimately spend several
                    # 120 s attempts in a row, so the log gets the same fact as well --
                    # harness telemetry only, no prompt or response content.
                    emit_provider_liveness(
                        PROVIDER_CHAIN_RETRY, {**failure_detail, "delay_s": delay_s}
                    )
                    self._sleep(delay_s)
                    # else: retries exhausted for this model; fall through to fallback.

            if not is_last_model:
                next_model = chain_models[model_position + 1]
                self._record_event(
                    RunEventType.PROVIDER_FALLBACK,
                    run_id=run_id,
                    turn_number=turn_number,
                    step_index=request.step_index,
                    detail={
                        "from_model": _model_name(model),
                        "to_model": _model_name(next_model),
                        "step_index": request.step_index,
                    },
                )

        exhaustion_detail: dict[str, Any] = {
            "step_index": request.step_index,
            "chain": [_model_name(model) for model in chain_models],
            "failures": failures,
        }
        self._record_event(
            RunEventType.MODEL_CHAIN_EXHAUSTED,
            run_id=run_id,
            turn_number=turn_number,
            step_index=request.step_index,
            detail=exhaustion_detail,
        )
        raise ProviderChainExhausted(
            "every model in the configured primary + fallback chain failed for this "
            "decision step (FR-042, P6, SC-012) -- there is no fabricate, skip, or "
            "default-move path to take instead",
            detail=exhaustion_detail,
        )

    # ----------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------

    def _assert_image_count(self, request: DecisionRequest, response: DecisionResponse) -> None:
        expected = len(request.images)
        if response.image_count != expected:
            raise ImageCountMismatch(
                "provider response image_count does not match the request's image count -- "
                "there is no code path that drops images to make a call fit; a mismatch is a "
                "defect, not a degradation (FR-039, P2, invariant I7)",
                detail={
                    "step_index": request.step_index,
                    "expected_image_count": expected,
                    "reported_image_count": response.image_count,
                },
            )

    def _record_event(
        self,
        event_type: RunEventType,
        *,
        run_id: RunId,
        turn_number: int | None,
        step_index: int | None,
        detail: dict[str, Any],
    ) -> None:
        self._event_sink.write_run_event(
            RunEvent(
                event_id=EventId(uuid.uuid4().hex),
                run_id=run_id,
                turn_number=turn_number,
                step_index=step_index,
                event_type=event_type,
                occurred_at=self._clock(),
                detail=detail,
            )
        )


__all__ = [
    "ImageCountMismatch",
    "ProviderChain",
    "RetryPolicy",
    "RunEventSink",
]
