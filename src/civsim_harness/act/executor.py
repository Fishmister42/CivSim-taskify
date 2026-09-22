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

**CLOSED 2026-09-22: the gap above is no longer open for an action that declares its arguments.**
That convention could express exactly ONE argument in practice. Re-derived from the store (1600
action records, snapshot 23:27Z): every decision's ``parameters`` key-set is ``('target',)`` or
``()`` and nothing else, so ``extra`` was empty on every dispatch ever made -- because
``agent/context.py`` told the model to name its subject in ``parameters.target`` "and nothing
else". Five shipped actions dispatch to a Lua function taking two or three arguments and could
not work at all: ``policies.slot_policy`` (71 of 71 ``unknown_policy``),
``religion.found_religion`` (40 of 40 ``unknown_religion_or_belief``), and
``congress.cast_vote`` / ``espionage.assign_mission`` / ``policies.assign_governor``, never
attempted and refused by their own Lua on the nil argument.
``ParityDeclaration.lua_arguments`` now states a declaration's real positional shape and
:func:`_build_arguments` builds the call from it; the agent's action listing renders the extra
parameter names from that same field, so the two cannot drift. A declaration that does not say
marshals exactly as described above, which is what keeps the five per-action Lua shims
(``units.move_to``, ``units.build_improvement``, ``units.promote``, ``camera.move``, the
``cities.*`` family) working untouched -- and ``tests/unit/test_declared_lua_arguments.py``
ratchets that no *new* multi-argument order is written as a sixth shim.

**The one declaration-id-shaped exception: prompt responses.** Every ``prompts.*`` action shares one
generic Lua function, ``lua/ingame/screens.lua``'s ``CivSim_Screens.respond(promptType, optionId)``
(``catalogs/actions/prompts.yaml``'s own header) -- ``promptType`` is the screen id, matching
``game.screen_state``'s own ``screen`` field naming, so it is supplied here rather than left to the
generic parameter-marshaling rule above.

MEASURED LIVE 2026-09-22 (Linux client, game turn 56, the board wedged on Cyrus's greeting): that
derivation used to be an inline prefix swap here -- ``str(declaration_id).removeprefix("prompts.")``
re-prefixed with ``"prompt."`` -- which is right for twelve of the thirteen prompt declarations and
wrong for the thirteenth. ``prompts.ai_diplomatic_approach`` answers the screen
``prompt.diplomatic_approach``: the catalog names the action for what the human does (answer an
*AI's* approach) and the screen for what is on screen, and that one mismatch is already recorded,
in both directions, in :data:`civsim_harness.act.prompts.PROMPT_ACTION_BY_SCREEN`. The inline swap
did not consult it, so every answer to that prompt went out as ``prompt.ai_diplomatic_approach``,
a key ``CIVSIM_KNOWN_SCREENS`` does not contain, and ``CivSim_Screens_RespondToPrompt``'s first
guard rejected it. Proven on the live client, both directions, non-destructively::

    respond("prompt.ai_diplomatic_approach", "Goodbye")
        -> {"reason": "unknown_prompt", "ok": false}
    respond("prompt.diplomatic_approach", "__not_an_option__")
        -> {"ok": false, "reason": "option_not_offered", "offered": ["Goodbye"],
            "prompt": "prompt.diplomatic_approach"}

The second call is the proof that only the *key* was wrong: with the right one the guard passes and
the real handler (``CivSim_Screens_AnswerDiplomaticApproach``, commit ``e0e82f0``) is reached and
answers about the option instead. The cost of that one wrong key was total -- an unanswerable prompt
keeps ``game.has_blocking_prompt`` true, so ``turn.end_turn`` is refused ``unavailable_to_human_now``
and **no run on that board can advance a turn at all**. So the inverse mapping is no longer
re-derived here: this module calls
:func:`civsim_harness.act.prompts.prompt_screen_for_declaration_id`, the function that already owns
the exception table, which is what makes the two directions incapable of disagreeing again. That
helper existed and was tested when this bug shipped; it simply had no caller in ``src/``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from civsim_harness.act.prompts import prompt_screen_for_declaration_id
from civsim_harness.capability.executor import CapabilityExecutor
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError
from civsim_harness.models.catalog import (
    IntegrationCapability,
    ParityDeclaration,
    lua_argument_parameter_name,
)
from civsim_harness.models.common import CapabilityId, DeclarationId

#: catalogs/actions/prompts.yaml's own capability_id -- the one family of declarations dispatched
#: through a single, generic, declaration_id-parametrized Lua function rather than one function
#: each (see module docstring).
_PROMPT_CAPABILITY_ID = CapabilityId("prompts.orders")


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
            declaration=declaration,
            capability=capability,
            parameters=parameters,
            target=target,
        )
        result = await self._executor.execute(
            declaration_id, context=declaration.context, arguments=arguments
        )
        return result.value


def missing_declared_arguments(
    declaration: ParityDeclaration, parameters: Mapping[str, Any]
) -> list[str]:
    """Which of *declaration*'s declared ``lua_arguments`` this decision did not supply.

    ``[]`` for a declaration that declares none -- it takes whatever the legacy convention
    produces, exactly as before. Used by :mod:`civsim_harness.act.dispatch` to refuse the decision
    before anything is executed, and re-asked by :func:`_build_arguments` at the point of use.
    """
    if declaration.lua_arguments is None:
        return []
    missing: list[str] = []
    for entry in declaration.lua_arguments:
        name = lua_argument_parameter_name(entry)
        key = "target" if name is None else name
        if parameters.get(key) is None:
            missing.append(key)
    return missing


def _build_arguments(
    *,
    declaration: ParityDeclaration,
    capability: IntegrationCapability,
    parameters: Mapping[str, Any],
    target: Any,
) -> tuple[Any, ...]:
    """Turn one decision's ``parameters``/``target`` into positional Lua call arguments.

    **Declared arguments first (the mechanism that replaced the per-action shim).** When the
    declaration carries ``lua_arguments``, the call is built in exactly that order: ``target``
    takes the value the agent chose, and ``parameters.<name>`` takes that key from the decision's
    own ``parameters``. Nothing is inferred from key ordering and nothing is optional -- a
    declared argument the decision did not supply raises here rather than sending a shorter call,
    because "silently pass fewer" is precisely how ``policies.slot_policy`` spent 71 dispatches
    answering ``unknown_policy`` with the policy name sitting in the slot-index parameter. The
    production path refuses earlier still, at ``act/dispatch.py``; this is the re-check at the
    point of use that the rest of this codebase already applies (see :class:`ActionExecutor`'s
    own docstring on defense in depth).

    **A declaration that says nothing behaves exactly as it always did.** See the module
    docstring's "genuinely open design surface" note: the fallback below is the original
    explicit, documented convention -- every non-``target`` parameter value in sorted-key order,
    then ``target`` -- left byte-for-byte alone, because five shipped actions (``units.move_to``,
    ``units.build_improvement``, ``units.promote``, ``camera.move`` and the ``cities.*`` family)
    carry their own Lua-side normalisation written against it. ``prompts.orders`` is the one
    capability-level exception, and it is evaluated first so no prompt declaration can be
    re-routed by declaring arguments.
    """
    if capability.capability_id == _PROMPT_CAPABILITY_ID:
        # NOT a prefix swap: `act/prompts.py` owns the screen-id <-> declaration-id mapping in both
        # directions, exception table included. Deriving it a second time here is what wedged the
        # 2026-09-22 board -- see the module docstring's MEASURED LIVE note.
        return (prompt_screen_for_declaration_id(declaration.declaration_id), target)

    if declaration.lua_arguments is not None:
        missing = missing_declared_arguments(declaration, parameters)
        if missing:
            raise CatalogError(
                "this action's Lua takes arguments the decision did not supply, so the call "
                "cannot be built -- it is refused rather than dispatched with fewer",
                detail={
                    "declaration_id": str(declaration.declaration_id),
                    "lua_arguments": list(declaration.lua_arguments),
                    "missing": missing,
                    "supplied": sorted(parameters),
                },
            )
        built: list[Any] = []
        for entry in declaration.lua_arguments:
            name = lua_argument_parameter_name(entry)
            built.append(target if name is None else parameters[name])
        return tuple(built)

    extra = tuple(value for key, value in sorted(parameters.items()) if key != "target")
    if target is None:
        return extra
    return (*extra, target)
