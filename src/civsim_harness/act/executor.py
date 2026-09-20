"""The production :class:`ActionExecutor` (T208).

``run/decision_loop.py`` already defines ``ActionExecutor`` as an async ``Protocol`` --
``async def __call__(self, declaration_id, parameters, target) -> Any`` -- called exactly once per
authorized decision, its return value never consumed by the loop: "the effect is confirmed by
re-observing and re-verifying afterward, never by trusting this call's own return" (that module's
own docstring). This module is the first real implementation of that Protocol, dispatching through
:class:`~civsim_harness.capability.executor.CapabilityExecutor` (T206) -- the same executor T207
uses for observations -- and otherwise does nothing else: :mod:`civsim_harness.act.dispatch`
already decided the action may proceed (availability + context authorization) before this is ever
called, and :mod:`civsim_harness.act.verify` is what turns the *next* fresh observation into
``applied``/``rejected`` afterward. **This module's own return value is not, and must never become,
a source of truth for that outcome** -- FR-011 and invariant I4 hold structurally here because
``run/decision_loop.py`` (unmodified by this task) simply never reads what ``execute_action``
returns; the concrete case this whole rule exists for is spelled out in
``lua/ingame/turn_control.lua``'s own header: ``UI.RequestAction`` returns ``nil`` unconditionally,
so a swallowed end-turn and a successful one are indistinguishable at this call site, and only the
turn-number readback (a fresh observation, re-verified) is real evidence.

**Building the Lua call's arguments is a genuinely open design surface, not an existing contract.**
Every action's own ``availability_predicate``/``verification_predicate`` binds exactly one
caller-supplied value, ``target`` (``act/predicates.py``'s own ``build_predicate_bindings`` --
see its module docstring's explicitly "documented simplification": even a unit/city action's own
*subject* is resolved against that same single ``target``, not a second, separately-named
parameter). ``Decision.parameters`` is otherwise a free-form ``dict`` the agent itself populates,
with no per-action parameter schema declared anywhere in this catalog today.
:func:`_build_arguments` below is this module's own, explicitly best-effort answer to "how do
those free-form values become positional Lua call arguments": every non-``target`` parameter
value, in sorted-key order (the only deterministic order available without a declared schema),
followed by ``target`` itself when present. This mirrors, rather than papers over, the same kind
of gap ``act/predicates.py`` already
documents for subject resolution -- it is not a verified, per-action argument mapping (this
environment has no live Civ VI client or Lua interpreter to verify one against), and is recorded
here with the same honesty this catalog's own Lua files use for their ``UNVERIFIED`` call shapes.

**The one declaration-id-shaped exception: prompt responses.** Every ``prompts.*`` action shares one
generic Lua function, ``lua/ingame/screens.lua``'s ``CivSim_Screens.respond(promptType, optionId)``
(``catalogs/actions/prompts.yaml``'s own header) -- ``promptType`` is derived directly from the
declaration_id (``prompts.unit_promotion`` -> ``prompt.unit_promotion``, matching
``game.screen_state``'s own ``screen`` field naming, not guessed at), so it is supplied here rather
than left to the generic parameter-marshaling rule above.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from civsim_harness.capability.executor import CapabilityExecutor
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.models.catalog import IntegrationCapability
from civsim_harness.models.common import CapabilityId, DeclarationId

#: catalogs/actions/prompts.yaml's own capability_id -- the one family of declarations dispatched
#: through a single, generic, declaration_id-parametrized Lua function rather than one function
#: each (see module docstring).
_PROMPT_CAPABILITY_ID = CapabilityId("prompts.orders")
#: The declaration_id domain prefix every prompts.* declaration carries ("prompts.unit_promotion",
#: ...) versus the screen-identity id lua/ingame/screens.lua's CIVSIM_KNOWN_SCREENS actually uses
#: for the same prompt family ("prompt.unit_promotion", singular). Both are catalog-authoring
#: conventions, not values either file states outright, so the mapping is spelled out here.
_PROMPT_DECLARATION_PREFIX = "prompts."
_PROMPT_SCREEN_PREFIX = "prompt."


class ActionExecutor:
    """Implements ``run.decision_loop.ActionExecutor``'s Protocol against a real
    :class:`~civsim_harness.capability.executor.CapabilityExecutor`.
    """

    def __init__(self, *, executor: CapabilityExecutor, registry: CapabilityRegistry) -> None:
        self._executor = executor
        self._registry = registry

    async def __call__(
        self, declaration_id: DeclarationId, parameters: Mapping[str, Any], target: Any
    ) -> Any:
        """Dispatch *declaration_id* via T206 and return whatever the Lua call reported.

        The caller (``run/decision_loop.py``) never inspects this return value -- see the module
        docstring. ``act/dispatch.py`` has already authorized this exact ``(declaration_id,
        context)`` pair before this is ever invoked; ``CapabilityExecutor.execute`` re-checks it
        anyway (defense in depth -- the same "re-check at the point of use" discipline
        ``act/predicates.py`` already applies elsewhere in this codebase), which is what makes
        this module's own context lookup below safe to pass straight through.
        """
        declaration = self._registry.resolve(declaration_id)
        capability = self._registry.capability_for(declaration_id)
        arguments = _build_arguments(
            declaration_id=declaration_id,
            capability=capability,
            parameters=parameters,
            target=target,
        )
        result = await self._executor.execute(
            declaration_id, context=declaration.context, arguments=arguments
        )
        return result.value


def _build_arguments(
    *,
    declaration_id: DeclarationId,
    capability: IntegrationCapability,
    parameters: Mapping[str, Any],
    target: Any,
) -> tuple[Any, ...]:
    """Turn one decision's free-form ``parameters``/``target`` into positional Lua call arguments.

    See the module docstring's "genuinely open design surface" note: this is an explicit,
    documented convention, not a verified per-action mapping. ``prompts.orders`` is the one
    exception with a real, non-guessed shape (see below).
    """
    if capability.capability_id == _PROMPT_CAPABILITY_ID:
        prompt_type = _PROMPT_SCREEN_PREFIX + str(declaration_id).removeprefix(
            _PROMPT_DECLARATION_PREFIX
        )
        return (prompt_type, target)

    extra = tuple(value for key, value in sorted(parameters.items()) if key != "target")
    if target is None:
        return extra
    return (*extra, target)
