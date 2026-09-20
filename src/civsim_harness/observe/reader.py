"""The production :class:`ObservationReader` (T207).

``run/decision_loop.py`` already defines ``ObservationReader`` as an async ``Protocol`` --
``async def __call__(self) -> tuple[Sequence[CapabilityResult], str]`` -- and calls it exactly once
per decision step, never caching or reusing its result (FR-008, invariant I14). Until this module,
no class implemented that Protocol against a real
:class:`~civsim_harness.capability.executor.CapabilityExecutor`; every existing caller was a test
fake (``tests/integration/test_decision_loop.py``'s ``_FakeGame.read``, and similar).

:class:`ObservationReader` closes that gap: for every ``kind: observation`` declaration the run
reads each step, it calls :meth:`~civsim_harness.capability.executor.CapabilityExecutor.execute`
(T206) -- which is what actually reads ``IntegrationCapability.implementation_ref``, loads the
``lua/`` file, and dispatches it -- validates the produced value against that declaration's own
``output_schema``, and returns the raw ``CapabilityResult``\\ s plus a plain ``screen_identity``
string for :func:`~civsim_harness.observe.assemble.assemble_observation` (called by the run loop,
not by this class -- see that module's own docstring: it "never calls Nexus" and accepts only
already-executed results).

**Why only ``kind: observation``, never ``kind: view``.** A view's camera state, screening, and
capture are a separate concern with a separate call site (``observe/capture.py``'s
``capture_for_step``, driven straight from ``run/decision_loop.py``'s own ``camera_state_provider``)
-- nothing about assembling *this* reader's ``CapabilityResult`` list needs or produces a capture.
Restricting this reader to observations keeps it symmetric with that split rather than reaching into
a concern this class was never asked to own.

**Schema validation happens twice, on purpose.** ``observe.assemble._validate_output`` is the
exact function ``assemble_observation`` itself uses once these results reach it a moment
later in ``run/decision_loop.py``'s own ``_observe``; calling it here too means a malformed
capability result is attributed to *this* declaration_id's read, at the point it was produced,
rather than only surfacing once assembly re-derives the same failure downstream -- and the two
checks can never disagree, since they are the same function. Reusing it (rather than a second,
drifting copy) is deliberate: see ``models/common.py``'s own precedent for when duplicating a
small helper is right and when reusing the original is right -- this is the latter, since the
validator is neither small nor safe to fork.
"""

from __future__ import annotations

from collections.abc import Sequence

from civsim_harness.capability.executor import CapabilityExecutor
from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.models.catalog import DeclarationKind
from civsim_harness.models.common import DeclarationId
from civsim_harness.observe.assemble import CapabilityResult, _validate_output
from civsim_harness.observe.screen_identity import UNKNOWN_SCREEN, interpret_screen_state

#: catalogs/observations/game.yaml's own declaration_id -- the same well-known id
#: ``run/decision_loop.py`` uses to route prompts (its own ``SCREEN_STATE_DECLARATION_ID``).
#: Restated here rather than imported: this module has no reason to depend on the decision loop
#: module for a single literal, and the literal itself is catalog data, not decision-loop logic.
SCREEN_STATE_DECLARATION_ID = DeclarationId("game.screen_state")


class ObservationReader:
    """Implements ``run.decision_loop.ObservationReader``'s Protocol against a real
    :class:`~civsim_harness.capability.executor.CapabilityExecutor`.

    *declaration_ids* is the fixed set of ``kind: observation`` declarations this run reads every
    decision step. When omitted, defaults to *every* ``kind: observation`` declaration in
    *registry*'s loaded catalog -- a reasonable default for "the run uses everything the catalog
    declares", while still letting a caller (composition root, or a narrower test) supply an
    explicit subset instead.
    """

    def __init__(
        self,
        *,
        executor: CapabilityExecutor,
        registry: CapabilityRegistry,
        declaration_ids: Sequence[DeclarationId] | None = None,
    ) -> None:
        self._executor = executor
        self._registry = registry
        self._declaration_ids: tuple[DeclarationId, ...] = (
            tuple(declaration_ids)
            if declaration_ids is not None
            else _every_observation_declaration_id(registry)
        )

    async def __call__(self) -> tuple[Sequence[CapabilityResult], str]:
        """One fresh read of every configured observation declaration (FR-008, invariant I14).

        Never caches or reuses a prior call's results -- each call resolves every declaration's
        own ``context`` afresh from *registry* and dispatches through
        :class:`~civsim_harness.capability.executor.CapabilityExecutor`, which itself resolves the
        Lua state index by name on every dispatch (see that module's own docstring).
        """
        results: list[CapabilityResult] = []
        screen_identity = UNKNOWN_SCREEN

        for declaration_id in self._declaration_ids:
            declaration = self._registry.resolve(declaration_id)
            result = await self._executor.execute(declaration_id, context=declaration.context)
            _validate_output(declaration, result.value)
            results.append(result)

            if declaration_id == SCREEN_STATE_DECLARATION_ID:
                screen_identity = interpret_screen_state(result.value).screen

        return results, screen_identity


def _every_observation_declaration_id(registry: CapabilityRegistry) -> tuple[DeclarationId, ...]:
    return tuple(
        declaration.declaration_id
        for declaration in registry.catalog.declarations.values()
        if declaration.kind is DeclarationKind.OBSERVATION
    )
