"""The structural parity filter (T127, research R9 design rule 1).

Principle I's enforcement only works if the boundary is a *type*, not a
convention a future contributor might forget. This module is that type: it
defines :class:`CapabilityResult` as the shape parity filtering accepts, and
:func:`filter_to_entries` as a producer of :class:`~civsim_harness.models.
turn.ObservationEntry` values from it -- there is no other function in this
package (and there must be none anywhere else in the harness) that builds an
``ObservationEntry`` from a bare Lua table, a raw Nexus payload string, or an
unattributed dict.

**One definition, and it is this one (T231).** ``observe/assemble.py``
(T095) used to re-implement this exact guarantee inline for its own
composition job -- resolve the declaration, reject a non-observation/view
kind, key the entry by the declaration id, carry the declaration's own
``context`` -- which meant the copy living *here*, in the package whose own
docstring calls itself "Principle I's enforcement point", was the dead one:
a reviewer hardening the parity boundary would have edited a module the
agent's data never passed through. ``assemble_observation`` now calls
:func:`resolve_observable` for the resolution-and-kind gate and builds its
entries the one way this module defines, adding only what is genuinely its
own (validating each value against the declaration's ``output_schema``, and
composing many results into one ``Observation``). Its local
``CapabilityResult`` -- ``{declaration_id, value}`` -- remains the identical
shape :class:`CapabilityResult` below exports, for the identical reason
(contracts/capability-catalog.md "Context assembly": "the assembler accepts
capability results only; there is no raw-Lua input type"); the two types
stay separate so neither package has to import the other's model, but there
is no longer two implementations of the *rule*. This module is also the type
:mod:`civsim_harness.parity.forbidden` and
:mod:`civsim_harness.parity.screening` are documented against.

Why the guarantee is structural rather than conventional:

- :class:`CapabilityResult` is a frozen ``dataclass`` requiring a
  ``declaration_id`` up front. There is no constructor for it that accepts a
  bare string or an unattributed mapping -- a caller holding one has to
  *already* know which catalog declaration answers it before this type will
  accept it at all.
- :func:`filter_to_entries` immediately calls :func:`resolve_observable`,
  which raises :class:`~civsim_harness.errors.ObservationAssemblyError` the
  moment ``declaration_id`` does not resolve in the loaded catalog. There is
  no code path here that skips this -- it is the first thing the function
  does.
- Only ``observation`` and ``view`` declarations may pass (matching
  ``observe/assemble.py``'s own rule) -- an ``action`` declaration never
  produces an observation entry, and routing one through here raises
  :class:`~civsim_harness.errors.ObservationAssemblyError` instead of
  silently producing an entry that misrepresents what kind of capability
  answered it.

**The consequence for a developer wanting new data**: the only way to get a
new value into the agent's observation is to author a catalog declaration
(so it has a ``declaration_id`` this type can name and a registry can
resolve), implement the capability behind it, and route the result through
this function (or ``observe/assemble.py``, which routes through it). There is no shortcut
that also works -- not because the codebase asks nicely, but because every
producer of ``ObservationEntry`` in the harness is typed against exactly
this shape, and this shape cannot be built from an unattributed value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import CatalogError, ObservationAssemblyError
from civsim_harness.models.catalog import DeclarationKind, ParityDeclaration
from civsim_harness.models.common import DeclarationId
from civsim_harness.models.turn import ObservationEntry

# --------------------------------------------------------------------------
# The sole input type
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityResult:
    """The result of dispatching exactly one declared capability.

    This is parity filtering's only accepted input shape (research R9 rule
    1). It is produced by whatever executes a capability against Nexus (or a
    bespoke integration) and JSON-decodes the response -- never assembled by
    hand from a raw string. ``value`` is whatever that decode produced (a
    dict, a list, a scalar): exactly what
    :meth:`~civsim_harness.nexus.client.NexusClient.execute_command` hands
    back, or an equivalent fixture/fake value in tests.
    """

    declaration_id: DeclarationId
    value: Any


# --------------------------------------------------------------------------
# A producer of ObservationEntry values
# --------------------------------------------------------------------------


def filter_to_entries(
    result: CapabilityResult,
    *,
    registry: CapabilityRegistry,
) -> list[ObservationEntry]:
    """Turn one :class:`CapabilityResult` into its attributed observation entry.

    1. Resolves ``result.declaration_id`` against *registry* (see
       :func:`resolve_observable`), refusing (via
       :class:`~civsim_harness.errors.ObservationAssemblyError`) a
       ``declaration_id`` that is not registered in the loaded catalog
       (FR-023).
    2. Refuses (via :class:`~civsim_harness.errors.ObservationAssemblyError`)
       a declaration that is not ``kind in {observation, view}`` -- an
       action never produces an observation entry.
    3. Returns exactly one :class:`~civsim_harness.models.turn.ObservationEntry`,
       keyed by the declaration id itself and carrying the declaration's own
       ``context`` (never a caller-supplied one, so there is no path for a
       caller to mislabel which Lua context a value came from) -- so every
       entry is attributable back to the exact catalog entry that produced
       it (FR-016, SC-007).
    """
    declaration = resolve_observable(registry, result.declaration_id)

    return [
        ObservationEntry(
            declaration_id=declaration.declaration_id,
            key=str(declaration.declaration_id),
            value=result.value,
            context=declaration.context,
        )
    ]


def resolve_observable(
    registry: CapabilityRegistry, declaration_id: DeclarationId
) -> ParityDeclaration:
    """Resolve *declaration_id* to the declaration that may answer an observation (T231).

    Steps 1 and 2 of :func:`filter_to_entries`, split out because
    :func:`~civsim_harness.observe.assemble.assemble_observation` needs the resolved declaration
    itself (to validate the value against its ``output_schema``) and must not re-implement the
    resolution to get it. **One definition, and it lives here** -- ``parity/``'s own package
    docstring calls itself "Principle I's enforcement point", and until T231 the copy living here
    was the dead one while ``observe/assemble.py`` carried an inline twin, so a reviewer hardening
    the parity boundary would have edited a module the agent's data never passed through.

    Raises :class:`~civsim_harness.errors.ObservationAssemblyError` -- never a bare
    :class:`~civsim_harness.errors.CatalogError` -- both for a ``declaration_id`` absent from the
    loaded catalog (FR-023) and for one naming an ``action``, which never produces an observation
    entry. One error type for "this result may not become an observation entry" is what lets the
    decision loop's own T096 handling treat every such failure identically.
    """
    try:
        declaration = registry.resolve(declaration_id)
    except CatalogError as exc:
        raise ObservationAssemblyError(
            "capability result names a declaration_id absent from the loaded catalog",
            detail={"declaration_id": str(declaration_id), "reason": exc.message},
        ) from exc
    if declaration.kind not in (DeclarationKind.OBSERVATION, DeclarationKind.VIEW):
        raise ObservationAssemblyError(
            "capability result names an action declaration, not an observation or view",
            detail={"declaration_id": str(declaration_id), "kind": str(declaration.kind)},
        )
    return declaration
