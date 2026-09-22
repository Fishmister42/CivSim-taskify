"""A decision step's TOTAL provider attempts are bounded, across every re-drive of the chain.

The defect these tests pin, observed live on 2026-09-22 and re-derived from the code: each
OpenRouter request was bounded (`openrouter.DEFAULT_REQUEST_TIMEOUT_S`), and each *invocation*
of `ProviderChain.complete_step` was bounded (`len(models) x max_attempts_per_model`), but the
bound lived in a local variable -- `total_prior_attempts = 0` at the top of the method. Anything
that called `complete_step` a second time for the same decision step therefore got a complete,
fresh ladder with no memory that the step had already spent one. The ladder had a bound; the
step had none.

**The axis these fixtures vary is the one the rule constrains.** The rule is about *how many
attempts a step may cost* and *which outcome class it costs them on* -- so the fixtures hold
model identity, prompt text, images and response schema fixed and vary only (a) the number of
calls, (b) the `CallOutcome` the provider returns, and (c) which `step_index` the request names.
A fixture set that varied model names or prompt content would encode the fixtures rather than
the contract: the chain would look "tested" while the number of attempts went unchecked.

**The test that would have caught the 58 minutes** is
`test_a_step_re_driven_on_an_outcome_that_can_never_succeed_issues_no_further_attempts`: a fake
provider that returns a retryable-but-hopeless outcome forever, re-driven fifty times, asserting
that the provider is never called again past the budget and that the step gives up with
`exhaustion_kind="step_attempt_budget"` recorded. A step retried forever writes nothing, so the
record is the only place the difference between "never reached" and "gave up after N" can live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from civsim_harness.errors import ProviderChainExhausted
from civsim_harness.models.common import Cost, EventId, ModelRef, RunId
from civsim_harness.models.config import ModelConfig
from civsim_harness.models.records import CallOutcome, RunEvent, RunEventType
from civsim_harness.provider.chain import ProviderChain, RetryPolicy, StepAttemptBudget
from civsim_harness.provider.port import (
    DecisionRequest,
    DecisionResponse,
    RawDecision,
)

_RUN_ID = RunId("run-step-budget")
_CLOCK = datetime(2026, 9, 22, tzinfo=UTC)

#: Every outcome class the layers below `ProviderChain` can hand it that is NOT a decision.
#: `CONTEXT_REJECTED` is the odd one out -- never retried in place, straight to fallback -- so it
#: is called out separately where that distinction matters.
_RETRIED_IN_PLACE = (
    CallOutcome.FAILED,
    CallOutcome.RATE_LIMITED,
    CallOutcome.EMPTY_RESPONSE,
)


def _model(name: str) -> ModelRef:
    return ModelRef(provider="openrouter", model=name)


_PRIMARY = _model("primary")
_FALLBACK = _model("fallback")


def _raw_decision() -> RawDecision:
    return RawDecision(action_declaration_id="units.move_to", reasoning="advance", parameters={})


def _request(*, step_index: int) -> DecisionRequest:
    """One decision step's request. Identical for every test except `step_index` -- the axis."""
    return DecisionRequest(
        model=_PRIMARY,
        system="You are the sole player of Civilization VI.",
        observation="Current screen: WorldScreen",
        images=[],
        step_index=step_index,
        response_schema={"type": "object"},
    )


@dataclass
class _CountingProvider:
    """Returns *outcome* on every call, except the *succeed_on_call*-th, which returns a decision.

    Holds everything but the outcome fixed, and counts calls -- the number of provider attempts
    is the quantity under test, so it is the thing this double measures.
    """

    outcome: CallOutcome
    succeed_on_call: int | None = None
    calls: list[ModelRef] = field(default_factory=list)

    def describe(self, model: ModelRef) -> object:  # pragma: no cover - never called here
        raise NotImplementedError

    def complete(self, request: DecisionRequest) -> DecisionResponse:
        self.calls.append(request.model)
        succeeded = self.succeed_on_call is not None and len(self.calls) >= self.succeed_on_call
        return DecisionResponse(
            decision=_raw_decision() if succeeded else None,
            model_served=request.model,
            latency_ms=0,
            cost=Cost(),
            retry_count=0,
            fallback_occurred=False,
            image_count=len(request.images),
            outcome=CallOutcome.DECISION_RETURNED if succeeded else self.outcome,
        )


@dataclass
class _EventSink:
    events: list[RunEvent] = field(default_factory=list)

    def write_run_event(self, event: RunEvent) -> EventId:
        self.events.append(event)
        return event.event_id

    def exhaustions_for_step(self, step_index: int) -> list[RunEvent]:
        return [
            e
            for e in self.events
            if e.event_type == RunEventType.MODEL_CHAIN_EXHAUSTED and e.step_index == step_index
        ]


def _chain(
    provider: _CountingProvider,
    sink: _EventSink,
    *,
    attempts_per_model: int = 2,
    fallbacks: tuple[ModelRef, ...] = (_FALLBACK,),
) -> ProviderChain:
    return ProviderChain(
        provider,  # type: ignore[arg-type]
        ModelConfig(primary=_PRIMARY, fallbacks=list(fallbacks)),
        event_sink=sink,
        retry_policy=RetryPolicy(max_attempts_per_model=attempts_per_model),
        sleep=lambda _delay: None,
        rand=lambda: 0.0,
        clock=lambda: _CLOCK,
    )


def _drive(chain: ProviderChain, step_index: int) -> ProviderChainExhausted:
    with pytest.raises(ProviderChainExhausted) as excinfo:
        chain.complete_step(_request(step_index=step_index), run_id=_RUN_ID, turn_number=1)
    return excinfo.value


# --------------------------------------------------------------------------
# The bound itself: total attempts per step, across re-drives
# --------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", _RETRIED_IN_PLACE)
def test_a_step_re_driven_on_an_outcome_that_can_never_succeed_issues_no_further_attempts(
    outcome: CallOutcome,
) -> None:
    """The 58-minute test: a provider stuck on a hopeless outcome must stop costing requests.

    Two models x two attempts is a budget of four. The step is then re-driven fifty times --
    far more than any retry ladder would ever allow -- and the provider must not be called
    once more. Before the budget existed, each of those fifty re-drives bought four fresh
    requests: 204 provider calls for one decision step that could never succeed.
    """
    provider = _CountingProvider(outcome=outcome)
    sink = _EventSink()
    chain = _chain(provider, sink)

    first = _drive(chain, step_index=1)
    assert len(provider.calls) == 4, "the first call spends exactly the ladder: 2 models x 2"
    assert first.detail["exhaustion_kind"] == "model_chain"
    assert first.detail["attempts_this_step"] == 4

    for _ in range(50):
        again = _drive(chain, step_index=1)
        assert again.detail["exhaustion_kind"] == "step_attempt_budget"
        assert again.detail["attempts_this_call"] == 0

    assert len(provider.calls) == 4, (
        "fifty re-drives of an exhausted step must buy zero further provider attempts -- "
        "this is the assertion that fails if the counter ever goes back to a local variable"
    )


def test_the_budget_is_per_step_so_the_next_step_is_served_normally() -> None:
    """Positive twin along the axis the rule constrains: same provider, same models, same
    prompt, same hopeless outcome -- only `step_index` differs. Each step gets its own full
    ladder, which is what makes this a per-step attempt bound and NOT a cap on how many
    decision steps a turn may take (T110/FR-008: the step count is unbounded by design).
    """
    provider = _CountingProvider(outcome=CallOutcome.FAILED)
    sink = _EventSink()
    chain = _chain(provider, sink)

    _drive(chain, step_index=1)
    assert len(provider.calls) == 4

    _drive(chain, step_index=2)
    assert len(provider.calls) == 8, "a fresh step gets a fresh budget"

    _drive(chain, step_index=1)
    assert len(provider.calls) == 8, "the earlier step's budget is still gone"


def test_an_outcome_that_skips_retries_still_accumulates_across_re_drives() -> None:
    """`CONTEXT_REJECTED` is never retried in place, so one call spends only one attempt per
    model -- two of a four-attempt budget. The step is therefore NOT exhausted, and a second
    call is served. That second call spends the remaining two and only then gives up.

    This is the precise behaviour the old per-invocation counter could not produce: it reset to
    zero, so `CONTEXT_REJECTED` could be re-driven for ever at two requests a go.
    """
    provider = _CountingProvider(outcome=CallOutcome.CONTEXT_REJECTED)
    sink = _EventSink()
    chain = _chain(provider, sink)

    first = _drive(chain, step_index=7)
    assert len(provider.calls) == 2
    assert first.detail["attempts_this_step"] == 2
    assert first.detail["attempts_remaining_for_step"] == 2

    second = _drive(chain, step_index=7)
    assert len(provider.calls) == 4
    assert second.detail["attempts_this_step"] == 4
    assert second.detail["attempts_remaining_for_step"] == 0

    third = _drive(chain, step_index=7)
    assert len(provider.calls) == 4, "nothing left to spend"
    assert third.detail["exhaustion_kind"] == "step_attempt_budget"


def test_a_step_that_succeeds_within_its_budget_is_untouched() -> None:
    """The other half of the twin: vary the outcome class, hold the attempt axis's shape. A
    step that recovers on its third attempt must still be served, must report the failures it
    cost, and must leave the budget with what it did not spend.
    """
    provider = _CountingProvider(outcome=CallOutcome.RATE_LIMITED, succeed_on_call=3)
    sink = _EventSink()
    chain = _chain(provider, sink)

    response = chain.complete_step(_request(step_index=1), run_id=_RUN_ID, turn_number=1)

    assert response.outcome is CallOutcome.DECISION_RETURNED
    assert len(provider.calls) == 3
    assert response.retry_count == 2, "two failed attempts preceded the one that served it"
    assert response.fallback_occurred is True, "the third attempt was against the fallback"
    assert sink.exhaustions_for_step(1) == []


def test_the_budget_is_derived_from_the_policy_rather_than_configured() -> None:
    """No new number: the budget is the ladder's own length, so a chain whose retry policy or
    fallback list changes gets a budget that changes with it. Nothing to tune and nothing a
    caller has to pass -- which is why there is no production call site to keep in step.
    """
    sink = _EventSink()
    provider = _CountingProvider(outcome=CallOutcome.FAILED)

    assert _chain(provider, sink, attempts_per_model=2).attempts_per_step == 4
    assert _chain(provider, sink, attempts_per_model=5).attempts_per_step == 10
    assert _chain(provider, sink, attempts_per_model=3, fallbacks=()).attempts_per_step == 3


def test_a_budget_cannot_be_minted_twice_for_the_same_step() -> None:
    """The structural guarantee, asserted on the object graph rather than on behaviour: the
    only path that constructs a `StepAttemptBudget` is the get-or-create, and it returns the
    *same object* every time. There is no edge by which a re-drive could receive a fresh one.
    """
    provider = _CountingProvider(outcome=CallOutcome.FAILED)
    chain = _chain(provider, _EventSink())

    first = chain._budget_for(4)
    first.spend()
    second = chain._budget_for(4)

    assert second is first
    assert second.spent == 1


# --------------------------------------------------------------------------
# Absence vs exhaustion in the record
# --------------------------------------------------------------------------


def test_a_step_never_reached_and_a_step_that_gave_up_are_not_the_same_record() -> None:
    """The distinction the record must carry. Step 1 exhausts; step 2 is never attempted. If
    these ever collapse into one representation -- both absent, or both a bare `failures: []` --
    this test fails, because it asserts on both sides at once rather than on the present one
    only.
    """
    provider = _CountingProvider(outcome=CallOutcome.FAILED)
    sink = _EventSink()
    chain = _chain(provider, sink)

    _drive(chain, step_index=1)

    exhausted = sink.exhaustions_for_step(1)
    never_reached = sink.exhaustions_for_step(2)

    assert len(exhausted) == 1, "a step that gave up says so"
    assert never_reached == [], "a step never reached writes nothing at all"

    detail = exhausted[0].detail
    assert detail["attempts_this_step"] == 4
    assert detail["attempts_budgeted_for_step"] == 4
    assert detail["exhaustion_kind"] == "model_chain"
    # The two representations must not be reconstructible from each other: the exhaustion
    # record carries a positive attempt count, which absence structurally cannot.
    assert detail["attempts_this_step"] > 0


def test_a_re_driven_step_names_the_attempts_it_cannot_describe() -> None:
    """The `government_rows_without_hash` precedent (882758e): when the record cannot answer,
    it says which part it could not answer instead of returning a silent `[]`.

    On the re-drive, `failures` is empty -- not because nothing failed, but because the four
    failures happened in an earlier call this record never saw. A bare empty list would read as
    "no failures", which is the exact lie. `attempts_without_detail` and `failures_reason` make
    the record say "I do not know" out loud.
    """
    provider = _CountingProvider(outcome=CallOutcome.FAILED)
    sink = _EventSink()
    chain = _chain(provider, sink)

    first = _drive(chain, step_index=1)
    assert first.detail["attempts_without_detail"] == 0
    assert "failures_reason" not in first.detail, "nothing was unknown on the first call"
    assert len(first.detail["failures"]) == 4

    second = _drive(chain, step_index=1)
    assert second.detail["failures"] == []
    assert second.detail["attempts_without_detail"] == 4
    assert second.detail["failures_reason"] == "attempts_from_earlier_calls_not_detailed"
    assert second.detail["attempts_this_step"] == 4, (
        "the count survives even though the per-attempt detail does not -- "
        "an unknown that is named is not the same as an absence"
    )


def test_every_recorded_failure_says_which_attempt_of_the_step_it_was() -> None:
    """`attempt` restarts at 1 for each model, so it cannot order a step's attempts. With a
    budget that spans calls, the record needs a number that never restarts.
    """
    provider = _CountingProvider(outcome=CallOutcome.FAILED)
    sink = _EventSink()
    chain = _chain(provider, sink)

    _drive(chain, step_index=1)

    failures = [
        e.detail
        for e in sink.events
        if e.event_type == RunEventType.PROVIDER_FAILURE and e.step_index == 1
    ]
    assert [f["attempt"] for f in failures] == [1, 2, 1, 2], "per-model, restarts"
    assert [f["step_attempt"] for f in failures] == [1, 2, 3, 4], "per-step, never restarts"


def test_the_budget_is_charged_before_the_attempt_not_after_it() -> None:
    """A provider attempt that never returns is still an attempt. A budget that only counted
    completed calls would be blind to exactly the hang it exists to bound, so `spend()` happens
    ahead of the request.
    """
    assert StepAttemptBudget(step_index=1, budgeted=2).remaining == 2

    class _Exploding:
        def describe(self, model: ModelRef) -> object:  # pragma: no cover
            raise NotImplementedError

        def complete(self, request: DecisionRequest) -> DecisionResponse:
            raise RuntimeError("the socket went away")

    chain = ProviderChain(
        _Exploding(),  # type: ignore[arg-type]
        ModelConfig(primary=_PRIMARY, fallbacks=[]),
        event_sink=_EventSink(),
        retry_policy=RetryPolicy(max_attempts_per_model=2),
        sleep=lambda _delay: None,
        rand=lambda: 0.0,
        clock=lambda: _CLOCK,
    )

    with pytest.raises(RuntimeError):
        chain.complete_step(_request(step_index=1), run_id=_RUN_ID, turn_number=1)

    assert chain._budget_for(1).spent == 1, "the attempt that died was still paid for"
