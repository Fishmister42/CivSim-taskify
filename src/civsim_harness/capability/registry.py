"""The capability registry (T036): lookup plus wrong-context execution refusal.

FR-022 and research R3 treat ``GameCore_Tuner`` and ``InGame`` as distinct,
declared execution contexts -- a catalog entry names the context it runs in,
and the client refuses to execute it in the other one. That refusal has to
live somewhere every dispatch path passes through, or it is trivially
bypassable by a caller that skips it; :class:`CapabilityRegistry` is that
one choke point. It wraps an already-:func:`~civsim_harness.capability.loader.load_catalog`-ed
:class:`~civsim_harness.capability.loader.Catalog` and answers exactly two
questions a dispatcher needs before it is allowed to touch the Nexus client:
"does this declaration_id exist", and "is *this* the context it was declared
for".

This module does not talk to Nexus itself (that is the client, T031-T034,
and a later dispatch wave) -- it is the gate the dispatcher calls immediately
before it does.
"""

from __future__ import annotations

from dataclasses import dataclass

from civsim_harness.capability.loader import Catalog
from civsim_harness.errors import CatalogError
from civsim_harness.models.catalog import IntegrationCapability, ParityDeclaration
from civsim_harness.models.common import CapabilityId, DeclarationId, LuaContext


class WrongContextError(CatalogError):
    """A declaration was requested for execution in a Lua context it was not declared for.

    A subclass of :class:`~civsim_harness.errors.CatalogError` rather than a
    sibling: this is still a parity-boundary/catalog-contract violation
    (research R3), just one detected at dispatch time instead of at load
    time -- callers that only care "was this a catalog problem" can still
    catch :class:`~civsim_harness.errors.CatalogError` and get this too.
    """


@dataclass(frozen=True)
class CapabilityRegistry:
    """Binds a loaded :class:`Catalog` to lookup and context-gated dispatch."""

    catalog: Catalog

    def resolve(self, declaration_id: DeclarationId) -> ParityDeclaration:
        """Return the declaration for *declaration_id*, or raise if it is not registered."""
        try:
            return self.catalog.declarations[declaration_id]
        except KeyError as exc:
            raise CatalogError(
                "declaration_id is not registered in the loaded catalog",
                detail={"declaration_id": str(declaration_id)},
            ) from exc

    def capability_for(self, declaration_id: DeclarationId) -> IntegrationCapability:
        """Return the :class:`IntegrationCapability` implementing *declaration_id*.

        The declaration's own ``capability_id`` is guaranteed to resolve --
        :func:`~civsim_harness.capability.loader.load_catalog` already
        rejected any catalog where it did not (validation 2) -- so a lookup
        miss here would indicate a bug in the loader, not a legitimate
        runtime condition.
        """
        declaration = self.resolve(declaration_id)
        capability_id: CapabilityId = declaration.capability_id
        try:
            return self.catalog.capabilities[capability_id]
        except KeyError as exc:  # pragma: no cover - loader guarantees resolution
            raise CatalogError(
                "capability_id did not resolve despite passing catalog load",
                detail={"declaration_id": str(declaration_id), "capability_id": str(capability_id)},
            ) from exc

    def authorize(self, declaration_id: DeclarationId, context: LuaContext) -> ParityDeclaration:
        """Resolve *declaration_id* and refuse it unless it is declared for *context*.

        Call this immediately before dispatching to the Nexus client
        (FR-022, research R3). An entry declared for ``GameCore_Tuner`` must
        not execute in ``InGame`` and vice versa: raises
        :class:`WrongContextError` (a :class:`~civsim_harness.errors.CatalogError`)
        on a mismatch, and never returns a declaration for the wrong
        context.
        """
        declaration = self.resolve(declaration_id)
        if declaration.context != context:
            raise WrongContextError(
                "declaration is not executable in the requested Lua context",
                detail={
                    "declaration_id": str(declaration_id),
                    "declared_context": str(declaration.context),
                    "requested_context": str(context),
                },
            )
        return declaration
