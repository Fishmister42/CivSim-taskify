"""Fake `ModelProvider` test double (T057).

Implements `civsim_harness.provider.port.ModelProvider` with every outcome
contracts/model-provider-port.md names, scriptable per call: a single
decision, `empty_response`, `rate_limited`, `context_rejected`, `failed`,
and -- the one that is *not* an ordinary `CallOutcome` at all -- a
contract-violating multi-decision response.

**Why the multi-decision case raises instead of returning something.**
`DecisionResponse.decision` is singular (`RawDecision | None`); there is no
field a second decision could go in. P10 and "Adapter obligations" say a
provider that returns more than one decision for one call is a contract
violation the *adapter* must surface as a failure, never reconcile by
dropping or queuing the extra. `queue_multi_decision` reproduces exactly
that: it scripts `complete()` to behave the way a real adapter must when its
upstream wire response smuggles back two decisions for one request -- raise
`ProviderContractViolation`, not fabricate a `DecisionResponse`. This is
what T180 (`tests/contract/test_model_provider_port.py`, not part of this
task) asserts against.

**Scripting model.** A FIFO queue of outcomes, one consumed per `complete()`
call. `set_default_decision` / `set_default_decision_factory` supply a
fallback used whenever the queue is empty, for scenarios that want "just
keep answering" across many decision steps without scripting every single
one. `set_capabilities` overrides what `describe()` reports for a given
`ModelRef` -- including a tiny context or `accepts_images=False`, for the
T179 red-team case -- and every outcome-queuing method accepts an optional
`model_served` so a call can report a different model than the one
requested, for fallback-accounting tests (P3).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from civsim_harness.errors import HarnessError
from civsim_harness.models.common import Cost, ModelRef
from civsim_harness.models.records import CallOutcome
from civsim_harness.provider.port import (
    DecisionRequest,
    DecisionResponse,
    ModelCapabilities,
    RawDecision,
)

#: A generous, "everything works" default -- overridden per model via
#: `set_capabilities` for the T179 red-team case (tiny context / no images).
DEFAULT_CAPABILITIES = ModelCapabilities(
    accepts_images=True,
    max_context_tokens=1_000_000,
    max_images_per_request=None,
    confirmed=True,
)


class ProviderContractViolation(HarnessError):
    """Raised by the fake in place of a real adapter's own P10 enforcement.

    contracts/model-provider-port.md "Adapter obligations": a provider
    returning more than one decision for one request is a contract
    violation the adapter must surface, not reconcile. This is that
    surfacing, reproduced on demand by `queue_multi_decision` so the
    harness's refusal to accept it can be tested without a real adapter.
    """


# --------------------------------------------------------------------------
# Scripted outcomes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _ScriptedDecision:
    decision: RawDecision
    model_served: ModelRef | None = None
    latency_ms: int = 0
    cost: Cost = field(default_factory=Cost)
    retry_count: int = 0
    fallback_occurred: bool = False
    image_count: int | None = None  # None -> len(request.images), the correct default


@dataclass(frozen=True)
class _ScriptedFailure:
    outcome: CallOutcome  # EMPTY_RESPONSE, FAILED, RATE_LIMITED, or CONTEXT_REJECTED
    model_served: ModelRef | None = None
    latency_ms: int = 0
    cost: Cost = field(default_factory=Cost)
    retry_count: int = 0
    fallback_occurred: bool = False


@dataclass(frozen=True)
class _ScriptedMultiDecision:
    decisions: list[RawDecision]


_Scripted = _ScriptedDecision | _ScriptedFailure | _ScriptedMultiDecision


class FakeModelProvider:
    """Scriptable `ModelProvider` implementation (T057).

    Satisfies `civsim_harness.provider.port.ModelProvider` structurally
    (a `Protocol`, so no explicit inheritance is needed).
    """

    def __init__(self) -> None:
        self._capabilities: dict[tuple[str, str], ModelCapabilities] = {}
        self._queue: deque[_Scripted] = deque()
        self._default_decision: _ScriptedDecision | None = None
        self._default_decision_factory: Callable[[DecisionRequest], RawDecision] | None = None

        # Call logs, so a scripted scenario can also assert on what the
        # harness actually sent (e.g. that images were never dropped, P2).
        self.calls: list[DecisionRequest] = []
        self.describe_calls: list[ModelRef] = []

    # -- capability scripting (P1 chain preflight, T179) --------------------

    def set_capabilities(self, model: ModelRef, capabilities: ModelCapabilities) -> None:
        """Override what `describe(model)` reports.

        Used directly by the T179 red-team case: script a tiny
        `max_context_tokens` or `accepts_images=False` and assert the run
        fails closed at chain preflight, before any `complete()` call --
        never as a reduced-image call (P2).
        """
        self._capabilities[(model.provider, model.model)] = capabilities

    def describe(self, model: ModelRef) -> ModelCapabilities:
        self.describe_calls.append(model)
        return self._capabilities.get((model.provider, model.model), DEFAULT_CAPABILITIES)

    # -- outcome scripting ---------------------------------------------------

    def queue_decision(
        self,
        decision: RawDecision,
        *,
        model_served: ModelRef | None = None,
        latency_ms: int = 0,
        cost: Cost | None = None,
        retry_count: int = 0,
        fallback_occurred: bool = False,
        image_count: int | None = None,
    ) -> None:
        """Queue a successful `DECISION_RETURNED` outcome for the next `complete()` call.

        `model_served` defaults to the request's own model; pass a
        different `ModelRef` to simulate a fallback having served the call
        (P3, fallback-accounting tests). `image_count` defaults to the
        contractually correct `len(request.images)` (P2); it is exposed
        here only so a test can deliberately misconfigure it to prove a
        *caller* detects that defect -- the fake never misreports it on its
        own initiative.
        """
        self._queue.append(
            _ScriptedDecision(
                decision=decision,
                model_served=model_served,
                latency_ms=latency_ms,
                cost=cost if cost is not None else Cost(),
                retry_count=retry_count,
                fallback_occurred=fallback_occurred,
                image_count=image_count,
            )
        )

    def queue_empty_response(
        self,
        *,
        model_served: ModelRef | None = None,
        latency_ms: int = 0,
        cost: Cost | None = None,
        retry_count: int = 0,
        fallback_occurred: bool = False,
    ) -> None:
        """Queue `CallOutcome.EMPTY_RESPONSE` (P4: a successful call, no usable decision)."""
        self._queue_failure(
            CallOutcome.EMPTY_RESPONSE,
            model_served=model_served,
            latency_ms=latency_ms,
            cost=cost,
            retry_count=retry_count,
            fallback_occurred=fallback_occurred,
        )

    def queue_rate_limited(
        self,
        *,
        model_served: ModelRef | None = None,
        latency_ms: int = 0,
        cost: Cost | None = None,
        retry_count: int = 0,
        fallback_occurred: bool = False,
    ) -> None:
        """Queue `CallOutcome.RATE_LIMITED` (P5: a transient failure the chain layer retries)."""
        self._queue_failure(
            CallOutcome.RATE_LIMITED,
            model_served=model_served,
            latency_ms=latency_ms,
            cost=cost,
            retry_count=retry_count,
            fallback_occurred=fallback_occurred,
        )

    def queue_context_rejected(
        self,
        *,
        model_served: ModelRef | None = None,
        latency_ms: int = 0,
        cost: Cost | None = None,
        retry_count: int = 0,
        fallback_occurred: bool = False,
    ) -> None:
        """Queue `CallOutcome.CONTEXT_REJECTED` (a chain-level failure, never retried in place)."""
        self._queue_failure(
            CallOutcome.CONTEXT_REJECTED,
            model_served=model_served,
            latency_ms=latency_ms,
            cost=cost,
            retry_count=retry_count,
            fallback_occurred=fallback_occurred,
        )

    def queue_failed(
        self,
        *,
        model_served: ModelRef | None = None,
        latency_ms: int = 0,
        cost: Cost | None = None,
        retry_count: int = 0,
        fallback_occurred: bool = False,
    ) -> None:
        """Queue `CallOutcome.FAILED` (a generic failed call)."""
        self._queue_failure(
            CallOutcome.FAILED,
            model_served=model_served,
            latency_ms=latency_ms,
            cost=cost,
            retry_count=retry_count,
            fallback_occurred=fallback_occurred,
        )

    def queue_multi_decision(self, decisions: list[RawDecision]) -> None:
        """Queue the contract-violating case: `complete()` raises `ProviderContractViolation`.

        See the module docstring -- this is the fake "doing the wrong
        thing on demand" so the harness's refusal to accept it (T180) is
        testable. `decisions` must hold at least two entries; that is what
        makes it a violation rather than an ordinary decision.
        """
        if len(decisions) < 2:
            raise ValueError("queue_multi_decision needs at least two decisions to violate P10")
        self._queue.append(_ScriptedMultiDecision(decisions=list(decisions)))

    def set_default_decision(
        self,
        decision: RawDecision,
        *,
        model_served: ModelRef | None = None,
        latency_ms: int = 0,
        cost: Cost | None = None,
        retry_count: int = 0,
        fallback_occurred: bool = False,
        image_count: int | None = None,
    ) -> None:
        """Set a fixed fallback `DECISION_RETURNED` outcome used whenever the queue is empty."""
        self._default_decision_factory = None
        self._default_decision = _ScriptedDecision(
            decision=decision,
            model_served=model_served,
            latency_ms=latency_ms,
            cost=cost if cost is not None else Cost(),
            retry_count=retry_count,
            fallback_occurred=fallback_occurred,
            image_count=image_count,
        )

    def set_default_decision_factory(
        self, factory: Callable[[DecisionRequest], RawDecision]
    ) -> None:
        """Like `set_default_decision`, but builds a fresh `RawDecision` per call from the
        request (e.g. varying `parameters` by `request.step_index`) -- for a turn-cycle
        smoke test that wants to keep answering indefinitely without scripting every step.
        """
        self._default_decision = None
        self._default_decision_factory = factory

    def clear_default_decision(self) -> None:
        self._default_decision = None
        self._default_decision_factory = None

    # -- ModelProvider protocol -----------------------------------------------

    def complete(self, request: DecisionRequest) -> DecisionResponse:
        self.calls.append(request)
        outcome = self._next_outcome(request)

        if isinstance(outcome, _ScriptedMultiDecision):
            raise ProviderContractViolation(
                "Fake provider produced more than one decision for a single "
                "DecisionRequest -- a contract violation an adapter must "
                "surface as a failure, never reconcile (P10, FR-008)",
                detail={
                    "step_index": request.step_index,
                    "decision_count": len(outcome.decisions),
                },
            )

        if isinstance(outcome, _ScriptedFailure):
            return DecisionResponse(
                decision=None,
                model_served=outcome.model_served or request.model,
                latency_ms=outcome.latency_ms,
                cost=outcome.cost,
                retry_count=outcome.retry_count,
                fallback_occurred=outcome.fallback_occurred,
                image_count=len(request.images),
                outcome=outcome.outcome,
            )

        image_count = (
            outcome.image_count if outcome.image_count is not None else len(request.images)
        )
        return DecisionResponse(
            decision=outcome.decision,
            model_served=outcome.model_served or request.model,
            latency_ms=outcome.latency_ms,
            cost=outcome.cost,
            retry_count=outcome.retry_count,
            fallback_occurred=outcome.fallback_occurred,
            image_count=image_count,
            outcome=CallOutcome.DECISION_RETURNED,
        )

    # -- internals -------------------------------------------------------------

    def _queue_failure(
        self,
        outcome: CallOutcome,
        *,
        model_served: ModelRef | None,
        latency_ms: int,
        cost: Cost | None,
        retry_count: int,
        fallback_occurred: bool,
    ) -> None:
        self._queue.append(
            _ScriptedFailure(
                outcome=outcome,
                model_served=model_served,
                latency_ms=latency_ms,
                cost=cost if cost is not None else Cost(),
                retry_count=retry_count,
                fallback_occurred=fallback_occurred,
            )
        )

    def _next_outcome(self, request: DecisionRequest) -> _Scripted:
        if self._queue:
            return self._queue.popleft()
        if self._default_decision_factory is not None:
            return _ScriptedDecision(decision=self._default_decision_factory(request))
        if self._default_decision is not None:
            return self._default_decision
        raise AssertionError(
            f"FakeModelProvider.complete() was called for step_index="
            f"{request.step_index} with no outcome queued and no default "
            "decision set -- script one with queue_decision(...) or "
            "set_default_decision(...)/set_default_decision_factory(...) "
            "before driving this fake."
        )
