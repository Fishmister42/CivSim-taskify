# Contract: Model Provider Port

**Feature**: `002-civ-playing-harness` | **Schema version**: 1
**Default adapter**: OpenRouter over HTTP. No vendor SDK may appear on the decision path (FR-037).

Principle VII's promise is that changing which model plays is a change to run configuration and
nothing else. That only holds if the seam is specified, so it is specified here.

## Operations

```python
class ModelProvider(Protocol):
    def describe(self, model: ModelRef) -> ModelCapabilities: ...
    def complete(self, request: DecisionRequest) -> DecisionResponse: ...
```

```python
@dataclass
class ModelCapabilities:
    accepts_images: bool
    max_context_tokens: int
    max_images_per_request: int | None
    confirmed: bool          # False when the provider could not confirm — fails closed

@dataclass
class DecisionRequest:
    model: ModelRef
    system: str              # role + out-of-game guidance (FR-021)
    observation: str         # parity-filtered structured state for THIS decision step
    images: list[Image]      # this step's screened captures — never silently dropped
    step_index: int          # which step of the turn this call serves
    response_schema: dict    # expected shape of a SINGLE decision

@dataclass
class DecisionResponse:
    decision: RawDecision | None   # exactly one, with its reasoning; None only on a failed outcome
    model_served: ModelRef
    latency_ms: int
    cost: Cost                     # from provider-reported usage
    retry_count: int
    fallback_occurred: bool
    image_count: int               # must equal len(request.images)
    outcome: CallOutcome
```

**`decision` is singular, and that is the contract's most important shape.** FR-008 forbids asking
the agent to commit to a later decision before it has seen the result of the earlier one, so the
harness calls the provider once per decision step. A `list[RawDecision]` response — which is what
this contract said in revision 1 — would make batching the path of least resistance and put a
parity-adjacent correctness rule at the mercy of whoever writes the next caller. A turn contains as
many calls as it contained steps.

## Behavioural requirements

| # | Requirement | Requirement ref |
|---|---|---|
| **P1** | **Chain preflight.** Before turn 1, `describe()` every model in `primary + fallbacks`. Any model with `accepts_images=False`, `confirmed=False`, or context insufficient for a worst-case *decision step* fails the run | FR-039, SC-017 |
| **P2** | **No image dropping.** There is no code path that removes images to make a call fit. `image_count != len(images)` is a defect, not a degradation | FR-039, SC-017 |
| **P3** | **Served model recorded.** Every response names the model that actually served it. Since a call serves one step, a turn may be served by more than one model; distinguishability resolves per step and rolls up | FR-040, SC-016 |
| **P4** | **Empty is failure.** A successful HTTP response containing no usable decision is `CallOutcome.empty_response` — a failed call, never a decision to do nothing | Spec edge case |
| **P5** | **Retry then fall back.** Transient failures retry with exponential backoff and jitter, then move to the next model in the chain. Every attempt is a recorded run event | FR-041 |
| **P6** | **Exhaustion pauses.** When the chain is exhausted, the provider layer raises. The harness pauses the run in a recorded state. It has no fabricate, skip, or default-move path | FR-042, SC-012 |
| **P7** | **Cost from usage.** Cost is read from provider-reported usage, not independently priced | Spec assumption |
| **P8** | **No credentials anywhere.** Keys resolve from environment or a secrets file at call time and appear in no request record, log line, error message, or exception trace | FR-043, SC-018 |
| **P9** | **Model choice is configuration.** Changing the model changes `RunConfiguration.model_config` only — no code, no new integration | FR-038, SC-015 |
| **P10** | **One call, one decision.** A response carries exactly one decision or a failed outcome. A provider returning several for one request is a contract violation the adapter must surface, not reconcile | FR-008 |
| **P11** | **No wall-clock coupling to the turn.** The only clock on a call is the request timeout and its retry ladder. Nothing at the turn level may cancel a call because the turn has run long — there is no turn budget | FR-014, spec edge case |

## Adapter obligations

An adapter is responsible for its own wire format and for mapping provider errors onto
`CallOutcome`. It is **not** permitted to:

- Modify the images or observation it was handed.
- Substitute a different model than requested without reporting it in `model_served`.
- Swallow an error and return `decision=None` with a successful outcome — that is P4's
  `empty_response`, explicitly.
- Return more than one decision, or concatenate several into one (P10).
- Carry state between calls, or infer the turn's earlier steps. Each call is independent; the loop's
  continuity lives in the observation the harness assembles, not in the adapter.
- Read run configuration or game state directly; it receives a `DecisionRequest` and nothing else.

## OpenRouter adapter specifics

- OpenAI-compatible chat completions shape; images as `image_url` content parts.
- `describe()` reads the models endpoint for modality and context length rather than hard-coding
  them — the roster moves faster than this repo (R10). Unconfirmable ⇒ `confirmed=False` ⇒ P1 fails
  the run closed.
- Usage and cost come from the response's usage/generation data.
- Rate limiting (HTTP 429) and transient 5xx map to retry; context-length and modality refusals map
  to `context_rejected`, which is a chain-level failure rather than a retry.

## Conformance tests

`tests/contract/test_model_provider_port.py` runs against the OpenRouter adapter and a fake, and
asserts P1–P11. Three are red-team style rather than ordinary assertions, because they are the ones a
well-meaning change would break: **P2** (a fake provider advertising a tiny context must produce a
failed run, never a reduced-image call), **P8** (a key planted in the environment must not appear in
any serialized record, log, or raised exception), and **P10** (a fake returning two decisions for one
request must raise, not have the extras dropped or queued — silently discarding the second is as
wrong as executing it).

## Cost note

One image-carrying call per decision step, with no cap on steps per turn, makes this port the
dominant cost term in the system. That is the accepted consequence of FR-008 and is recorded in the
spec's Assumptions. The port's obligation is to report per-call cost accurately so the trade-off
stays visible; it has no budget-management role, and adding one would mean truncating turns, which
FR-014 forbids.
