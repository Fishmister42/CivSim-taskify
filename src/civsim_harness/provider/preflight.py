"""Chain preflight (T184).

contracts/model-provider-port.md P1 / V4 / FR-039 / SC-017: before turn 1, every model in
``model_config.primary + .fallbacks`` is ``describe()``-d, and any model with
``accepts_images=False``, ``confirmed=False``, or a context window too small for a *worst-case
decision step* fails the run closed, naming every failing model. This is what makes T185's
runtime no-image-drop rule (``provider/chain.py``) mostly theoretical in practice: a chain that
cannot carry the full context is refused here, before turn 1, rather than discovered mid-run.

This module has no access to game state, catalog declarations, or the observation assembler --
it cannot compute what "late-game structured state plus a step's declared views" actually sizes
to, so ``worst_case_context_tokens`` is a required argument the caller supplies (from
``agent/context.py`` or wherever that estimate is owned, both outside this task's file
ownership). ``preflight_chain`` only applies the comparison; it never invents the number.
"""

from __future__ import annotations

from dataclasses import dataclass

from civsim_harness.errors import HarnessError
from civsim_harness.models.common import ModelRef
from civsim_harness.models.config import ModelConfig
from civsim_harness.provider.port import ModelCapabilities, ModelProvider


class ChainPreflightError(HarnessError):
    """Chain preflight failed: one or more models cannot carry a worst-case decision step.

    Always describes *every* model in the chain before raising (never stops at the first
    failure), so ``detail["failing_models"]`` names every model an operator would need to
    change, not just the first one hit (FR-039, P1, V4, SC-017).
    """


@dataclass(frozen=True)
class ModelPreflightResult:
    """One model's chain-preflight outcome.

    Kept even for a passing model (``ok=True``, ``reasons=()``), so a caller has a full audit
    trail of exactly what preflight checked and confirmed, not only what failed.
    """

    model: ModelRef
    capabilities: ModelCapabilities
    ok: bool
    reasons: tuple[str, ...] = ()


def _model_name(model: ModelRef) -> str:
    return f"{model.provider}/{model.model}"


def _check_one(
    model: ModelRef,
    capabilities: ModelCapabilities,
    *,
    worst_case_context_tokens: int,
    requires_images: bool,
) -> ModelPreflightResult:
    reasons: list[str] = []

    if not capabilities.confirmed:
        reasons.append("capabilities could not be confirmed by the provider (confirmed=False)")

    if requires_images and not capabilities.accepts_images:
        reasons.append("model does not accept images (accepts_images=False)")

    if capabilities.max_context_tokens < worst_case_context_tokens:
        reasons.append(
            "context window too small for a worst-case decision step: "
            f"max_context_tokens={capabilities.max_context_tokens} < "
            f"required={worst_case_context_tokens}"
        )

    return ModelPreflightResult(
        model=model,
        capabilities=capabilities,
        ok=not reasons,
        reasons=tuple(reasons),
    )


def preflight_chain(
    provider: ModelProvider,
    model_config: ModelConfig,
    *,
    worst_case_context_tokens: int,
    requires_images: bool = True,
) -> list[ModelPreflightResult]:
    """``describe()`` every model in ``model_config.primary + .fallbacks`` before turn 1 (P1).

    *worst_case_context_tokens* is the caller's own sizing of "late-game structured state plus
    [a step's] declared views" (contract P1) -- this function does not compute that number, it
    only compares each model's reported ``max_context_tokens`` against it. *requires_images*
    defaults to ``True`` because every decision step's observation ordinarily carries screened
    captures; a run configured with no capture mechanism at all (``CapturePath.NONE``) may pass
    ``False``.

    Raises :class:`ChainPreflightError` naming *every* model that fails ``accepts_images``,
    ``confirmed``, or context sizing -- the run never starts, and images are never dropped to
    make an undersized model fit (FR-039, SC-017). Returns the full per-model result list, in
    chain order, when every model passes -- callers may log/audit it as evidence of what
    preflight actually confirmed.
    """
    chain = [model_config.primary, *model_config.fallbacks]
    results = [
        _check_one(
            model,
            provider.describe(model),
            worst_case_context_tokens=worst_case_context_tokens,
            requires_images=requires_images,
        )
        for model in chain
    ]

    failures = [result for result in results if not result.ok]
    if failures:
        raise ChainPreflightError(
            "chain preflight failed: one or more models in the configured chain cannot "
            "carry a worst-case decision step (FR-039, P1, SC-017)",
            detail={
                "failing_models": [
                    {"model": _model_name(result.model), "reasons": list(result.reasons)}
                    for result in failures
                ],
            },
        )
    return results


__all__ = [
    "ChainPreflightError",
    "ModelPreflightResult",
    "preflight_chain",
]
