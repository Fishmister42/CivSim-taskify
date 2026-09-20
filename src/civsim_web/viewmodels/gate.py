"""The registry gate every view-model constructor reads store records through.

plan.md states the property this module exists to make true: *"there is no code
path from a raw ``MatchStore`` record to a response that does not pass through a
panel lookup. A store field with no registered panel is simply never read by any
constructor; it is not filtered *after* being read, it is never reached at all"*
(contracts/panel-registry.md, UP-001).

``GatedReader`` is that lookup, in the one shape every constructor needs. Ask it
for a field and one of three things happens:

- a panel declares it and the record has it -> the value, verbatim;
- **no panel declares it** -> the field is never touched on the record, and an
  ``UnavailableField`` records that the registry does not permit it (UP-001);
- a panel declares it but the record predates it -> the value is absent and an
  ``UnavailableField`` records *that*, which is FR-025's "unavailable for this
  run's recorded schema version" rather than a silent zero.

The three cases are deliberately one API. They are the same obligation --
*say unavailable, never blank* -- arriving from three different directions
(UP-001, FR-025, plan.md C1), and a constructor that had to remember which was
which would eventually forget one.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from civsim_web.registry.loader import PanelDeclaration, PanelRegistry
from civsim_web.viewmodels.base import UnavailableField

__all__ = [
    "GatedReader",
    "OUT_OF_GAME_BASIS",
    "Reason",
    "panel_basis_of",
]

#: What ``ObservationEntryView.panel_basis`` carries for a panel whose category
#: is ``out_of_game_telemetry``. data-model.md SS6 requires that field present on
#: every entry unconditionally, and panel-registry.md requires ``parity_basis``
#: be ``null`` for telemetry -- so the two are reconciled by naming the category
#: explicitly rather than by leaving the field blank, which would read as "nobody
#: recorded a basis" instead of "this is not in-game information at all".
OUT_OF_GAME_BASIS = "out_of_game_telemetry"


class Reason:
    """Why a field could not be shown, in the words each requirement uses."""

    UNREGISTERED = (
        "no Panel Registry declaration permits this field, so it is never read "
        "(UP-001, contracts/panel-registry.md)"
    )
    NOT_IN_RECORD = (
        "unavailable for this run's recorded schema version (FR-025)"
    )
    PORT_CANNOT_REACH = (
        "the published MatchStore port exposes no read that reaches this field "
        "(plan.md Complexity Tracking C1)"
    )


def panel_basis_of(panel: PanelDeclaration) -> str:
    """The basis string a view model carries for ``panel``.

    In-game panels carry their restated ``parity_basis`` (the registry refuses
    to load one without it, rule P1); telemetry panels carry the explicit
    out-of-game marker above.
    """
    if panel.category == "in_game":
        return panel.parity_basis or ""
    return OUT_OF_GAME_BASIS


@dataclass
class GatedReader:
    """Reads one store record's fields, gated by the Panel Registry.

    ``entity`` is the 002 data-model entity name the record represents --
    ``"TurnCycle"``, ``"Decision"``, ``"ModelCall"``, and so on -- because that
    is the name the registry indexes by. A reader is single-use and accumulates
    its own ``unavailable`` list, which the constructed view model carries so
    both readers see the same explanation for the same hole.
    """

    registry: PanelRegistry
    entity: str
    record: Any
    unavailable: list[UnavailableField] = dataclass_field(default_factory=list)

    # -- registry questions ---------------------------------------------------

    def panel(self, name: str) -> PanelDeclaration | None:
        """The first panel permitted to read ``entity.name``, if any."""
        panels = self.registry.panels_for_field(self.entity, name)
        return panels[0] if panels else None

    def allows(self, name: str) -> bool:
        return bool(self.registry.panels_for_field(self.entity, name))

    def basis(self, name: str) -> str | None:
        panel = self.panel(name)
        return panel_basis_of(panel) if panel is not None else None

    def title(self, name: str, fallback: str) -> str:
        panel = self.panel(name)
        return panel.title if panel is not None else fallback

    # -- reads ----------------------------------------------------------------

    def get(self, name: str, default: Any = None) -> Any:
        """``entity.name`` off the record, or ``default`` with a reason recorded.

        The unregistered branch returns *before* touching the record. That
        ordering is the whole mechanism: an unregistered field is not read and
        then discarded, it is never read.
        """
        if not self.allows(name):
            self._mark(name, Reason.UNREGISTERED)
            return default
        if not hasattr(self.record, name):
            self._mark(name, Reason.NOT_IN_RECORD)
            return default
        value = getattr(self.record, name)
        if value is None:
            return default
        return value

    def text(self, name: str, default: str | None = None) -> str | None:
        """``get`` for a value that may be an enum -- its ``value``, as a string.

        002's records type several fields as enums (``lifecycle_state``,
        ``outcome``, ``progress``, ``event_type``). Every one of them is
        rendered *verbatim* per data-model.md, so this unwraps the enum without
        relabelling it.
        """
        value = self.get(name)
        if value is None:
            return default
        return str(getattr(value, "value", value))

    def mapping(self, name: str) -> dict[str, Any]:
        """``get`` for a free-form JSON object field (``yields``, ``cost``)."""
        value = self.get(name)
        return dict(value) if isinstance(value, dict) else {}

    def unreachable(self, name: str, reason: str = Reason.PORT_CANNOT_REACH) -> None:
        """Record a field the *port*, not the registry, cannot reach (C1)."""
        self._mark(name, reason)

    def _mark(self, name: str, reason: str) -> None:
        entry = UnavailableField(field=f"{self.entity}.{name}", reason=reason)
        if entry not in self.unavailable:
            self.unavailable.append(entry)

    @property
    def missing(self) -> tuple[UnavailableField, ...]:
        return tuple(self.unavailable)
