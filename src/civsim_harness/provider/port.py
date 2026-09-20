"""The ``ModelProvider`` port (T043).

contracts/model-provider-port.md is the normative contract; this module defines
the seam it specifies -- the Protocol plus its request/response value types --
and nothing else. No adapter, no HTTP, no retry/fallback logic: those are
``provider/openrouter.py`` and ``provider/chain.py``, both later waves, built
against this module.

**``DecisionResponse.decision`` is singular, not a list, and that is the
contract's most important shape.** FR-008 forbids asking the agent to commit to
a later decision before it has seen the result of the earlier one, so the
harness calls the provider exactly once per decision step
(``DecisionRequest.step_index`` scopes the call to that one step). A provider
that returns more than one decision for a single call has violated the
contract (P10) -- the adapter must surface that as a failure, not reconcile it
by dropping or queuing the extras; this module carries no logic of its own to
enforce P10 (that is the adapter's and the conformance suite's job), it only
shapes the types so that a *correct* response can hold exactly one decision and
an *incorrect* one has nowhere to put a second.

These are wire-boundary value types, not persisted records -- unlike
``civsim_harness.models.*``, they are never written to the match-tracking
store, so they are plain (frozen) ``dataclasses`` rather than
``HarnessModel``/pydantic, matching the contract's own ``@dataclass``
pseudocode literally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from civsim_harness.errors import HarnessError
from civsim_harness.models.common import Cost, DeclarationId, ModelRef
from civsim_harness.models.records import CallOutcome

# --------------------------------------------------------------------------
# Supporting value types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Image:
    """One screened capture handed to a provider call, in wire-ready form.

    Opaque to the adapter: "Adapter obligations" in the contract forbids
    modifying the images a call was handed, so this is treated as an immutable
    blob the adapter forwards into whatever its wire format wants (e.g. a
    base64 ``image_url`` content part for the OpenRouter adapter) without
    inspecting or altering *data*. Distinct from the persisted
    ``models.turn.ScreenCapture`` record: this is the bytes actually sent on
    one call, not the audit record of how/why a capture was taken.
    """

    media_type: str
    data: bytes


@dataclass(frozen=True)
class RawDecision:
    """The model's raw decision content for one decision step (P10, FR-008).

    Everything the model itself supplies for a single decision step, before
    the harness attaches record identity and an execution/verification
    outcome. Compare ``models.decision.Decision``, which wraps this content
    with ``decision_id``, ``decision_step_id``, ``model_call_id``, and
    ``execution`` once the harness has dispatched and verified it -- this type
    is what a provider call itself is capable of producing, nothing more.
    """

    action_declaration_id: DeclarationId
    reasoning: str
    parameters: dict[str, Any] = field(default_factory=dict)
    is_end_turn: bool = False
    prompt_type: str | None = None


# --------------------------------------------------------------------------
# Port operations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCapabilities:
    """What ``describe()`` reports about one model (P1 chain preflight, FR-039)."""

    accepts_images: bool
    max_context_tokens: int
    max_images_per_request: int | None
    confirmed: bool  # False when the provider could not confirm -- fails closed


@dataclass(frozen=True)
class DecisionRequest:
    """One call's worth of input: exactly one decision step, never a batch (FR-008)."""

    model: ModelRef
    system: str  # role + out-of-game guidance (FR-021)
    observation: str  # parity-filtered structured state for THIS decision step
    images: list[Image]  # this step's screened captures -- never silently dropped (P2)
    step_index: int  # which step of the turn this call serves
    response_schema: dict[str, Any]  # expected shape of a SINGLE decision


@dataclass(frozen=True)
class DecisionResponse:
    """One call's outcome. ``decision`` is singular -- see module docstring and P10."""

    decision: RawDecision | None  # exactly one, with its reasoning; None only on a failed outcome
    model_served: ModelRef  # the model that actually served the call (P3)
    latency_ms: int
    cost: Cost  # from provider-reported usage, never independently priced (P7)
    retry_count: int
    fallback_occurred: bool
    image_count: int  # must equal len(request.images) (P2)
    outcome: CallOutcome


class ModelProvider(Protocol):
    """The seam between the harness and whichever LLM serves decisions.

    Principle VII's promise is that changing which model plays is a change to
    run configuration only (P9) -- that only holds if this seam is specified,
    which is what this Protocol and its value types do. An adapter
    implementing it owns its own wire format and error mapping, and is *not*
    permitted to (contract "Adapter obligations"): modify the images or
    observation it was handed; substitute a different model without reporting
    it in ``model_served``; swallow an error as ``decision=None`` with a
    successful outcome (that is ``CallOutcome.empty_response``, P4,
    explicitly); return more than one decision, or concatenate several into
    one (P10); carry state between calls or infer earlier steps; or read run
    configuration or game state directly.
    """

    def describe(self, model: ModelRef) -> ModelCapabilities:
        """Report *model*'s capabilities, used for chain preflight (P1)."""
        ...

    def complete(self, request: DecisionRequest) -> DecisionResponse:
        """Serve exactly one decision for *request*'s single decision step."""
        ...


# --------------------------------------------------------------------------
# Adapter obligation enforcement (T188)
# --------------------------------------------------------------------------


class AdapterObligationViolation(HarnessError):
    """An adapter broke one of contracts/model-provider-port.md's "Adapter obligations".

    Raised by :class:`ObligationEnforcingProvider`, never by a well-behaved adapter itself --
    this is a defensive boundary check, not something an adapter is expected to raise about its
    own behaviour.
    """


class ObligationEnforcingProvider:
    """Wraps any :class:`ModelProvider` and enforces the contract's "Adapter obligations" at
    the port boundary, defensively, at runtime.

    Two of the six obligations are already structurally impossible to violate through this
    seam and so need no runtime check here: an adapter cannot "carry state between calls, or
    infer the turn's earlier steps" when ``complete()``'s only input is one self-contained
    ``DecisionRequest``, and it cannot "read run configuration or game state directly" when
    neither is reachable from that same signature. "Return more than one decision" is likewise
    ruled out by ``DecisionResponse.decision`` being a single ``RawDecision | None`` field with
    nowhere to put a second one -- an adapter that tries must raise instead (P10), which is a
    property of the *adapter's* implementation (see ``provider/openrouter.py`` and
    ``tests/fakes/fake_provider.py``), not something this generic wrapper can detect from the
    outside.

    What *is* checked here, because nothing about the ``DecisionRequest``/``DecisionResponse``
    shapes prevents an adapter from getting them wrong: that ``request.observation`` and
    ``request.images`` come back unmodified (``images`` is a plain mutable ``list`` even though
    the dataclass itself is frozen, so an adapter mutating it in place would otherwise go
    unnoticed), that ``model_served`` is always populated, and that a ``decision`` is present if
    and only if ``outcome == CallOutcome.DECISION_RETURNED`` (P4's "swallow an error and return
    ``decision=None`` with a successful outcome" and its mirror image, both forbidden).
    """

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    def describe(self, model: ModelRef) -> ModelCapabilities:
        return self._provider.describe(model)

    def complete(self, request: DecisionRequest) -> DecisionResponse:
        observation_before = request.observation
        images_before = list(request.images)

        response = self._provider.complete(request)

        if request.observation != observation_before:
            raise AdapterObligationViolation(
                "adapter modified request.observation -- adapters must not alter the "
                "images or observation they were handed",
                detail={"step_index": request.step_index},
            )
        if list(request.images) != images_before:
            raise AdapterObligationViolation(
                "adapter modified request.images -- adapters must not alter the images "
                "or observation they were handed (P2, FR-039)",
                detail={"step_index": request.step_index},
            )
        if response.model_served is None:  # pragma: no cover - guards a broken adapter
            raise AdapterObligationViolation(
                "adapter did not report model_served -- a substituted model must always "
                "be reported (P3, FR-040)",
                detail={"step_index": request.step_index},
            )
        if response.decision is None and response.outcome == CallOutcome.DECISION_RETURNED:
            raise AdapterObligationViolation(
                "adapter reported outcome=DECISION_RETURNED with decision=None -- an "
                "empty successful response must be CallOutcome.EMPTY_RESPONSE, never a "
                "decision to do nothing (P4)",
                detail={"step_index": request.step_index},
            )
        if response.decision is not None and response.outcome != CallOutcome.DECISION_RETURNED:
            raise AdapterObligationViolation(
                "adapter returned a decision without outcome=DECISION_RETURNED",
                detail={"step_index": request.step_index, "outcome": response.outcome.value},
            )
        return response
