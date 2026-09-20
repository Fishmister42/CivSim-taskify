"""The structural parity filter (T127, research R9 design rule 1).

Principle I's enforcement only works if the boundary is a *type*, not a
convention a future contributor might forget. This module is that type: it
defines :class:`CapabilityResult` as the shape parity filtering accepts, and
:func:`filter_to_entries` as a producer of :class:`~civsim_harness.models.
turn.ObservationEntry` values from it -- there is no other function in this
package (and there must be none anywhere else in the harness) that builds an
``ObservationEntry`` from a bare Lua table, a raw Nexus payload string, or an
unattributed dict.

**Cross-reference, not a fork.** ``observe/assemble.py`` (T095, already
landed as part of an earlier wave) independently implements this exact
guarantee inline for its own composition job (building a whole
``Observation`` from many results, with schema validation against each
declaration's ``output_schema``): its own local ``CapabilityResult`` --
``{declaration_id, value}`` -- is the identical shape this module exports,
for the identical reason (contracts/capability-catalog.md "Context
assembly": "the assembler accepts capability results only; there is no
raw-Lua input type"). :class:`CapabilityResult` below mirrors that shape
deliberately, so the two are structurally the same concept even though nothing
currently imports one from the other -- see this wave's report for the
integration note. This module exists as the *dedicated, reusable* home for
that guarantee (T127 is a ``parity/`` deliverable in its own right, e.g. for
any future call site that needs "one result in, its attributed entries out"
without composing a full multi-result ``Observation``), and as the type
:mod:`civsim_harness.parity.forbidden` and
:mod:`civsim_harness.parity.screening` are documented against.

Why the guarantee is structural rather than conventional:

- :class:`CapabilityResult` is a frozen ``dataclass`` requiring a
  ``declaration_id`` up front. There is no constructor for it that accepts a
  bare string or an unattributed mapping -- a caller holding one has to
  *already* know which catalog declaration answers it before this type will
  accept it at all.
- :func:`filter_to_entries` immediately calls
  :meth:`~civsim_harness.capability.registry.CapabilityRegistry.resolve`,
  which raises :class:`~civsim_harness.errors.CatalogError` the moment
  ``declaration_id`` does not resolve in the loaded catalog. There is no
  code path here that skips this -- it is the first thing the function does.
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
this function (or its ``observe/assemble.py`` twin). There is no shortcut
that also works -- not because the codebase asks nicely, but because every
producer of ``ObservationEntry`` in the harness is typed against exactly
this shape, and this shape cannot be built from an unattributed value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import ObservationAssemblyError
from civsim_harness.models.catalog import DeclarationKind
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

    1. Resolves ``result.declaration_id`` against *registry*, refusing (via
       :class:`~civsim_harness.errors.CatalogError`) a ``declaration_id``
       that is not registered in the loaded catalog (FR-023).
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
    declaration = registry.resolve(result.declaration_id)

    if declaration.kind not in (DeclarationKind.OBSERVATION, DeclarationKind.VIEW):
        raise ObservationAssemblyError(
            "capability result names an action declaration, not an observation or view",
            detail={"declaration_id": str(result.declaration_id), "kind": str(declaration.kind)},
        )

    return [
        ObservationEntry(
            declaration_id=result.declaration_id,
            key=str(result.declaration_id),
            value=result.value,
            context=declaration.context,
        )
    ]
