"""Structured state assembly and assembly-failure handling (T095, T096).

The :class:`~civsim_harness.models.turn.Observation` is the parity-filtered board the agent sees
for exactly one decision step (FR-012, FR-024). This module's only input type is
:class:`CapabilityResult` -- an already-executed capability's raw, JSON-decoded value plus the
``declaration_id`` that produced it (contracts/capability-catalog.md "Context assembly": "the
assembler accepts capability results only; there is no raw-Lua input type").
:func:`assemble_observation` never calls Nexus, never re-derives a value from a previous step, and
caches nothing between calls -- it is a pure function of its arguments, meant to be invoked fresh
once per decision step by the run loop (T110, a later wave). There is structurally no
incremental-update path here to skip the schema-validation gate or return a stale board (FR-018,
invariant I14): the only way to get a stale ``Observation`` out of this function is to call it
with stale ``results``, which is a caller discipline this module cannot and does not try to
enforce on its own -- it is enforced by there being no other entry point into an ``Observation``
at all.

**T231 -- one structural filter, not two.** The resolve-and-kind gate and the attributed-entry
construction below are ``parity/filter.py``'s (``resolve_observable``/``filter_to_entries``), called
rather than re-implemented. This module used to carry an inline twin of both, which left the copy in
``parity/`` -- the package documented as "Principle I's enforcement point" -- with no caller at all,
so hardening it would have hardened a path the agent's data never travels. What remains this
module's own is ``output_schema`` validation (FR-018) and composing many results into one
``Observation``.

**T137 -- declaration attribution on this record path.** Every ``ObservationEntry`` this module
builds carries ``declaration_id=declaration.declaration_id``, where ``declaration`` is what
``registry.resolve(result.declaration_id)`` (via ``parity.filter.resolve_observable``) actually
returned --
never a caller-supplied string threaded through unchecked. Structurally, this could not be
otherwise: ``ObservationEntry.declaration_id`` (``models/turn.py``) has no default, so an entry
without one cannot be constructed at all, by this module or any other caller of that model. There
is no separate code path for an unattributed value to take.

**T096 -- assembly failure.** A failure at decision step *n* > 1, after earlier steps have already
executed and verified, is not recoverable in place: the turn cannot be finished from a stale
board, because the whole reason the observe-decide-execute-verify loop exists is to show the
agent the effect of what it just did (research R14). :func:`assemble_observation` raises
:class:`~civsim_harness.errors.ObservationAssemblyError` on any failure;
:func:`handle_assembly_failure` turns that raise into its ``observation_assembly_failed``
:class:`~civsim_harness.models.records.RunEvent` (FR-046) -- and per **T245** it is the *only*
definition of that event: ``resilience/recovery.py`` routes through it rather than carrying an
inline twin (the same one-definition rule, and the same resolution, as T231's structural-parity
filter). Neither function decides to abandon the
turn attempt itself -- consistent with every other pure builder in this wave
(``run.lifecycle.transition`` is the precedent this follows: return a record, let the caller
persist and act on it) -- that decision, and the actual replay from the turn's start quicksave,
belongs to the run loop (T110/T114, a later wave), which has the store handle and the
turn-attempt bookkeeping this module deliberately does not.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from civsim_harness.capability.registry import CapabilityRegistry
from civsim_harness.errors import ObservationAssemblyError
from civsim_harness.models.catalog import ParityDeclaration
from civsim_harness.models.common import (
    CaptureId,
    CatalogVersionRef,
    DecisionStepId,
    DeclarationId,
    EventId,
    ObservationId,
    RunId,
    Timestamp,
)
from civsim_harness.models.records import RunEvent, RunEventType
from civsim_harness.models.turn import Observation, ObservationEntry
from civsim_harness.parity.filter import CapabilityResult as FilterCapabilityResult
from civsim_harness.parity.filter import filter_to_entries, resolve_observable

# --------------------------------------------------------------------------
# T095 -- assembly
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityResult:
    """One already-executed capability's raw output -- the assembler's only accepted input shape.

    *value* is whatever the capability's own JSON-encoded Lua return decoded to (a ``dict``, a
    ``list``, a scalar) -- exactly the shape
    :meth:`~civsim_harness.nexus.client.NexusClient.execute_command` hands back, or an equivalent
    value from a fixture/fake in tests. This type carries no raw Lua,
    no camera control, and no execution side effects of its own; it is pure data.
    """

    declaration_id: DeclarationId
    value: Any


def assemble_observation(
    *,
    observation_id: ObservationId,
    decision_step_id: DecisionStepId,
    catalog_version: CatalogVersionRef,
    registry: CapabilityRegistry,
    results: Sequence[CapabilityResult],
    screen_identity: str,
    assembled_at: Timestamp,
    captures: Sequence[CaptureId] = (),
) -> Observation:
    """Assemble exactly one decision step's :class:`~civsim_harness.models.turn.Observation`.

    Every result is validated against its own declaration's ``output_schema`` (FR-018) and
    attributed to it via ``ObservationEntry.declaration_id`` (FR-016) before it can appear in the
    returned ``Observation`` -- there is no unattributed-value path. Raises
    :class:`~civsim_harness.errors.ObservationAssemblyError` when a result's ``declaration_id``
    does not resolve in *registry*, names an action rather than an observation/view, or fails
    schema validation. Callers must treat any raise here per T096 (see
    :func:`handle_assembly_failure`): the turn attempt is abandoned and replayed, never continued
    from a stale view.
    """
    entries: list[ObservationEntry] = []
    for result in results:
        # T231: the resolution-and-kind gate has exactly one definition, and it lives in
        # `parity/filter.py` -- the package whose own docstring calls itself "Principle I's
        # enforcement point". This module used to carry an inline twin of it, which made the
        # `parity/` copy the dead one: a reviewer hardening the parity boundary would have edited
        # a module the agent's data never passed through. What stays here is only what is
        # genuinely this module's own -- `output_schema` validation, and composing many results
        # into one `Observation`.
        declaration = resolve_observable(registry, result.declaration_id)
        _validate_output(declaration, result.value)
        entries.extend(
            filter_to_entries(
                FilterCapabilityResult(
                    declaration_id=declaration.declaration_id, value=result.value
                ),
                registry=registry,
            )
        )

    return Observation(
        observation_id=observation_id,
        decision_step_id=decision_step_id,
        assembled_at=assembled_at,
        catalog_version=catalog_version,
        entries=entries,
        captures=list(captures),
        screen_identity=screen_identity,
    )


def _validate_output(declaration: ParityDeclaration, value: Any) -> None:
    schema = declaration.output_schema
    assert schema is not None  # guaranteed by ParityDeclaration for observation/view kinds
    errors = _schema_errors(schema, value, path=str(declaration.declaration_id))
    if errors:
        raise ObservationAssemblyError(
            "capability result failed its declared output_schema",
            detail={"declaration_id": str(declaration.declaration_id), "errors": errors},
        )


# --------------------------------------------------------------------------
# Minimal JSON-Schema-subset value validator.
#
# This package carries no `jsonschema` dependency (out of scope for this deliverable -- see
# civsim_harness.capability.loader's own structural, dependency-free schema-shape checker, which
# this mirrors but for *values* rather than for the schema's own shape). Supports exactly the
# vocabulary the catalog's own output_schema entries use: `type` (single or list, including
# "null"), `properties` + `required`, and `items` (single schema or positional list).
# --------------------------------------------------------------------------

_JSON_SCHEMA_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


def _schema_errors(schema: Mapping[str, Any], value: Any, *, path: str) -> list[str]:
    errors: list[str] = []

    schema_type = schema.get("type")
    if schema_type is not None:
        allowed_types = schema_type if isinstance(schema_type, list) else [schema_type]
        if not _matches_any_type(value, allowed_types):
            errors.append(f"{path}: expected type in {allowed_types}, got {type(value).__name__}")
            return errors  # further structural checks are meaningless against the wrong type

    if isinstance(value, Mapping):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: required property missing")
        properties = schema.get("properties") or {}
        for key, sub_schema in properties.items():
            if key in value:
                errors.extend(_schema_errors(sub_schema, value[key], path=f"{path}.{key}"))

    if isinstance(value, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, list):
            for index, (item, sub_schema) in enumerate(zip(value, items_schema, strict=False)):
                errors.extend(_schema_errors(sub_schema, item, path=f"{path}[{index}]"))
        elif isinstance(items_schema, Mapping):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(items_schema, item, path=f"{path}[{index}]"))

    return errors


def _matches_any_type(value: Any, allowed_types: Sequence[str]) -> bool:
    for type_name in allowed_types:
        python_types = _JSON_SCHEMA_TYPE_CHECKS.get(type_name)
        if python_types is None:
            continue  # an unrecognised type name is a catalog-load-time concern, not this gate's
        # bool is a subclass of int in Python; JSON Schema treats them as distinct.
        if isinstance(value, bool) and type_name in ("integer", "number"):
            continue
        if isinstance(value, python_types):
            return True
    return False


# --------------------------------------------------------------------------
# T096 -- assembly-failure handling
# --------------------------------------------------------------------------


def handle_assembly_failure(
    exc: ObservationAssemblyError,
    *,
    run_id: RunId,
    turn_number: int,
    step_index: int | None = None,
    occurred_at: Timestamp,
    event_id: EventId | None = None,
) -> RunEvent:
    """Turn an :class:`~civsim_harness.errors.ObservationAssemblyError` into its
    ``observation_assembly_failed`` :class:`~civsim_harness.models.records.RunEvent` (FR-046,
    spec edge case).

    **T245 -- this is the one definition of that event.** The production caller is
    ``resilience/recovery.py``'s
    :meth:`~civsim_harness.resilience.recovery.RecoveryEngine.recover_from_observation_assembly_error`,
    which used to build the event inline -- leaving this, the named home, the dead twin (T231's
    exact shape, resolved the same way: the caller routes through the named home). *step_index*
    is recorded when the caller knows which step's assembly failed; the recovery path passes
    nothing here because by the time recovery runs the step attribution lives in the error's own
    ``detail`` (which this function merges into the event's ``detail`` verbatim), not as a
    separate value threaded alongside it.

    This function does not decide what happens to the turn attempt -- that is the run loop's job
    (T110/T114): mark the attempt ``abandoned``, record this event, and replay the turn from its own
    start quicksave (research R14, "Mid-turn observation failure is an interruption, not a
    recovery-in-place"). The turn may never be finished from the last good observation; continuing
    from it would silently violate the re-observe rule the whole decision loop exists to enforce
    (invariant I14) -- which is exactly why "abandon and replay from the turn's start quicksave"
    rather than "retry this one step in place" is the only response that keeps that invariant intact
    once *earlier* steps in the same attempt have already executed and verified real game-state
    changes that cannot be silently un-happened.
    """
    return RunEvent(
        event_id=event_id if event_id is not None else EventId(uuid.uuid4().hex),
        run_id=run_id,
        turn_number=turn_number,
        step_index=step_index,
        event_type=RunEventType.OBSERVATION_ASSEMBLY_FAILED,
        occurred_at=occurred_at,
        detail={"message": exc.message, **exc.detail},
    )
