"""Per-call accounting (T186).

contracts/model-provider-port.md P3 / P7, FR-040: every provider call -- whether it produced a
decision or ended in ``failed`` / ``rate_limited`` / ``empty_response`` / ``context_rejected`` --
gets its own durable :class:`~civsim_harness.models.records.ModelCall`, carrying
``decision_step_id``, ``model_requested``, ``model_served``, ``latency_ms``, provider-reported
``cost``, ``retry_count``, ``fallback_occurred``, ``image_count``, and ``outcome`` untouched from
the call that produced them. Per-turn cost is unbounded by construction (contract "Cost note");
this module's whole job is to keep that trade-off visible **per call**, never smoothed into a
per-turn or per-run aggregate that would hide which one call was expensive.
"""

from __future__ import annotations

from typing import Protocol

from civsim_harness.errors import HarnessError
from civsim_harness.models.common import DecisionStepId, ModelCallId, ModelRef, RunId, TurnCycleId
from civsim_harness.models.records import ModelCall
from civsim_harness.provider.port import DecisionRequest, DecisionResponse


class ImageCountAccountingMismatch(HarnessError):
    """*response* disagreed with *request* on image count when accounting for the call (P2, T185).

    A defensive re-check, independent of ``provider.chain.ImageCountMismatch``: that check
    guards ``ProviderChain.complete_step`` specifically, while this one guards
    :func:`record_model_call` for any caller that assembled a ``DecisionResponse`` without going
    through the chain at all. Either path refuses to record accounting for a call that may have
    silently dropped images, rather than writing a ``ModelCall`` whose own ``image_count`` field
    cannot be trusted (FR-039, invariant I7).
    """


class ModelCallSink(Protocol):
    """What :func:`record_model_call` needs from the store: just the one write
    (``MatchStore.write_model_call``) -- a narrower seam than the full store port, mirroring
    ``provider.chain.RunEventSink``. A real ``MatchStore`` satisfies this structurally.
    """

    def write_model_call(self, call: ModelCall) -> ModelCallId: ...


def build_model_call(
    *,
    model_call_id: ModelCallId,
    run_id: RunId,
    turn_cycle_id: TurnCycleId,
    decision_step_id: DecisionStepId,
    model_requested: ModelRef,
    request: DecisionRequest,
    response: DecisionResponse,
) -> ModelCall:
    """Assemble one :class:`ModelCall` from a completed provider call (FR-040, P3, P7).

    Every accounting field is carried through **untouched** from *response* -- ``latency_ms``,
    ``cost``, ``retry_count``, ``fallback_occurred``, ``image_count``, ``outcome`` -- this
    function performs no rounding, pricing, or aggregation of its own; it only binds those
    already-correct values to the ids that place this call on the run's timeline.

    Raises :class:`ImageCountAccountingMismatch` before constructing anything if *response*'s
    ``image_count`` disagrees with ``len(request.images)`` (P2).
    """
    if response.image_count != len(request.images):
        raise ImageCountAccountingMismatch(
            "response.image_count does not match request.images -- refusing to record "
            "accounting for a call that may have silently dropped images (FR-039, P2, "
            "invariant I7)",
            detail={
                "decision_step_id": decision_step_id,
                "expected_image_count": len(request.images),
                "reported_image_count": response.image_count,
            },
        )

    return ModelCall(
        model_call_id=model_call_id,
        run_id=run_id,
        turn_cycle_id=turn_cycle_id,
        decision_step_id=decision_step_id,
        model_requested=model_requested,
        model_served=response.model_served,
        latency_ms=response.latency_ms,
        cost=response.cost,
        retry_count=response.retry_count,
        fallback_occurred=response.fallback_occurred,
        image_count=response.image_count,
        outcome=response.outcome,
    )


def record_model_call(
    sink: ModelCallSink,
    *,
    model_call_id: ModelCallId,
    run_id: RunId,
    turn_cycle_id: TurnCycleId,
    decision_step_id: DecisionStepId,
    model_requested: ModelRef,
    request: DecisionRequest,
    response: DecisionResponse,
) -> ModelCall:
    """Build one :class:`ModelCall` (see :func:`build_model_call`) and durably write it through
    *sink* (``MatchStore.write_model_call``).

    Called for *every* completed provider call, independent of whether it produced a decision
    step -- a ``failed``, ``rate_limited``, ``empty_response``, or ``context_rejected`` call is
    still one call and still gets its own accounting record, never rolled into the eventual
    successful call's numbers.
    """
    call = build_model_call(
        model_call_id=model_call_id,
        run_id=run_id,
        turn_cycle_id=turn_cycle_id,
        decision_step_id=decision_step_id,
        model_requested=model_requested,
        request=request,
        response=response,
    )
    sink.write_model_call(call)
    return call


__all__ = [
    "ImageCountAccountingMismatch",
    "ModelCallSink",
    "build_model_call",
    "record_model_call",
]
