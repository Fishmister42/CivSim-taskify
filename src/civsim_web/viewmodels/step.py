"""``DecisionStepView`` and ``ObservationEntryView`` (T021, data-model.md SS6).

The step is the unit, not the turn. 002's own data model made record
completeness and reconstructability assertions at the step, so collapsing steps
back into a turn-level blob here would silently discard exactly the granularity
002 went to the trouble of recording (data-model.md, entity overview).

Two rules from data-model.md SS6 are enforced here rather than left to a
template's discretion:

1. **Verbatim, on dropping an unregistered observation entry** --

       an ``ObservationEntry.declaration_id`` that does not resolve to a Panel
       Registry entry is **dropped**, not passed through with a placeholder
       label

   The count of dropped entries *is* reported, in both readers. That is not a
   leak: it says how many entries the filter removed, never what they were, and
   a silent drop would be the "absence rendered as confirmed fact" UP-005
   forbids.

2. **``observation``, ``capture``, and ``decision`` are non-optional siblings.**
   There is no route that can return a decision without the state it was made
   from -- the structural form of UP-004's "a decision is never shown as a bare
   action".
"""

from __future__ import annotations

from typing import Any

from civsim_web.refs.reference import ViewReference
from civsim_web.registry.loader import PanelRegistry
from civsim_web.registry.lookup import panel_for_observation_entry
from civsim_web.viewmodels.base import UnavailableField, ViewModel
from civsim_web.viewmodels.capture import CaptureView, build_capture_view
from civsim_web.viewmodels.decision import DecisionView, build_decision_view
from civsim_web.viewmodels.gate import GatedReader, panel_basis_of

__all__ = [
    "DecisionStepView",
    "ObservationEntryView",
    "build_decision_step_view",
    "build_observation_entries",
]


class ObservationEntryView(ViewModel):
    """One entry the agent saw, filtered twice over (002's filter, then ours)."""

    panel_id: str
    label: str
    """From the Panel Registry entry's ``title`` -- never the raw store key."""
    value: Any = None
    panel_basis: str
    """The panel's restated ``parity_basis``, present on every entry
    unconditionally (SC-005's per-panel auditability), or the explicit
    out-of-game marker for a telemetry panel."""


class DecisionStepView(ViewModel):
    """One decision step: what was seen, what was decided, and what it cost."""

    run_id: str
    turn_number: int
    step_index: int
    reference: str
    """This step's canonical ``ViewReference`` path -- the same string the user
    copies out of the address bar and the directing session ``GET``s (FR-008)."""

    observation: tuple[ObservationEntryView, ...] = ()
    dropped_observation_count: int = 0
    capture: CaptureView = CaptureView()
    decision: DecisionView | None = None
    progress: str | None = None
    unavailable: tuple[UnavailableField, ...] = ()


def build_observation_entries(
    observation: Any | None, *, registry: PanelRegistry
) -> tuple[tuple[ObservationEntryView, ...], int]:
    """``(rendered entries, dropped count)`` for one step's observation.

    Every entry is looked up by its ``declaration_id``. No panel means the entry
    is dropped -- and because the lookup happens before the value is read, the
    registry is genuinely a closed list rather than a post-hoc filter.
    """
    if observation is None:
        return ((), 0)

    gate = GatedReader(registry, "Observation", observation)
    entries = gate.get("entries", default=()) or ()

    rendered: list[ObservationEntryView] = []
    dropped = 0
    for entry in entries:
        declaration_id = getattr(entry, "declaration_id", None)
        panel = (
            panel_for_observation_entry(registry, str(declaration_id))
            if declaration_id
            else None
        )
        if panel is None:
            dropped += 1
            continue
        rendered.append(
            ObservationEntryView(
                panel_id=panel.panel_id,
                label=panel.title,
                value=getattr(entry, "value", None),
                panel_basis=panel_basis_of(panel),
            )
        )
    return (tuple(rendered), dropped)


def build_decision_step_view(
    bundle: Any,
    *,
    registry: PanelRegistry,
    run_id: str,
    turn_number: int,
    capture: Any | None = None,
    capture_expected: bool | None = None,
) -> DecisionStepView:
    """Project one of 002's ``DecisionStepBundle``s.

    ``capture`` is the already-read ``ScreenCapture`` record for this step (the
    caller resolves it, because the store read belongs at the route layer, not
    in a view model). ``capture_expected`` defaults to "the observation named at
    least one capture", which is what separates ``never_captured`` from
    ``missing_record`` in ``CaptureView``.
    """
    step = getattr(bundle, "step", bundle)
    observation = getattr(bundle, "observation", None)

    gate = GatedReader(registry, "DecisionStep", step)
    step_index = int(gate.get("step_index", default=0) or 0)
    entries, dropped = build_observation_entries(observation, registry=registry)

    if capture_expected is None:
        declared = getattr(observation, "captures", ()) if observation is not None else ()
        capture_expected = bool(declared)

    reference = ViewReference(
        run_id=run_id, turn_number=turn_number, step_index=step_index
    ).path

    return DecisionStepView(
        run_id=run_id,
        turn_number=turn_number,
        step_index=step_index,
        reference=reference,
        observation=entries,
        dropped_observation_count=dropped,
        capture=build_capture_view(capture, registry=registry, expected=capture_expected),
        decision=build_decision_view(
            getattr(bundle, "decision", None),
            registry=registry,
            model_call=getattr(bundle, "model_call", None),
        ),
        progress=gate.text("progress"),
        unavailable=gate.missing,
    )
