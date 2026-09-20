"""View references (T011, data-model.md SS12; FR-008, UP-006).

**The reference *is* the URL path.** There is no opaque token, no resolver
endpoint, no encoding that maps to a route. FR-008 requires the user and the
directing Claude Code session to resolve the same reference to identical
content, and the simplest way to guarantee that is for the reference to be the
very thing the server already knows how to serve
(contracts/web-read-api.md, "View-reference resolution").

So this module is deliberately small: it parses a path into its parts and
renders the parts back into a path, and the round trip is exact. Everything
that could drift -- a registry of short ids, an expiring token, a second
encoding -- is absent by design rather than unimplemented.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "ViewReference",
    "ViewReferenceError",
    "parse_view_reference",
]


class ViewReferenceError(ValueError):
    """A string that is not a valid view reference.

    Distinct from "a valid reference that resolves to nothing": an unparseable
    path is a malformed reference, while a well-formed reference to a run that
    never existed is a 404, and a well-formed reference to a superseded turn
    attempt is a 200 that explains itself (FR-009). Collapsing the three would
    lose exactly the distinction FR-009 asks for.
    """


_RUN = re.compile(r"^/runs/(?P<run_id>[^/]+)$")
_TURN = re.compile(r"^/runs/(?P<run_id>[^/]+)/turns/(?P<turn>\d+)$")
_STEP = re.compile(r"^/runs/(?P<run_id>[^/]+)/turns/(?P<turn>\d+)/steps/(?P<step>\d+)$")
_TURN_PANEL = re.compile(
    r"^/runs/(?P<run_id>[^/]+)/turns/(?P<turn>\d+)/panels/(?P<panel_id>[^/]+)$"
)
_STEP_PANEL = re.compile(
    r"^/runs/(?P<run_id>[^/]+)/turns/(?P<turn>\d+)/steps/(?P<step>\d+)"
    r"/panels/(?P<panel_id>[^/]+)$"
)
_RUN_PANEL = re.compile(r"^/runs/(?P<run_id>[^/]+)/panels/(?P<panel_id>[^/]+)$")

# Order matters: the longer shapes must be tried before the shorter ones they
# extend, or `/runs/r/turns/3/steps/2` would never be reached.
_PATTERNS = (_STEP_PANEL, _TURN_PANEL, _STEP, _RUN_PANEL, _TURN, _RUN)


class ViewReference(BaseModel):
    """A stable identifier resolving to a run, a turn, a step, and/or a panel.

    ``turn_number`` is absent for run-level references; ``panel_id`` absent
    resolves to the whole turn or run; ``step_index`` is present only for
    step-scoped references.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    turn_number: int | None = None
    step_index: int | None = None
    panel_id: str | None = None

    @model_validator(mode="after")
    def _shape_is_reachable(self) -> ViewReference:
        if self.step_index is not None and self.turn_number is None:
            raise ValueError(
                "a step-scoped reference must name its turn: there is no route "
                "shape for a step without one"
            )
        if self.turn_number is not None and self.turn_number < 1:
            raise ValueError("turn_number is 1-based (002 data-model.md SS5)")
        if self.step_index is not None and self.step_index < 1:
            raise ValueError("step_index is 1-based and contiguous from 1 (002 SS6)")
        if not self.run_id:
            raise ValueError("every reference names a run")
        return self

    @property
    def path(self) -> str:
        """The canonical path -- this *is* the reference (data-model.md SS12)."""
        parts = ["/runs", quote(self.run_id, safe="")]
        if self.turn_number is not None:
            parts += ["turns", str(self.turn_number)]
            if self.step_index is not None:
                parts += ["steps", str(self.step_index)]
        if self.panel_id is not None:
            parts += ["panels", quote(self.panel_id, safe="")]
        return "/".join(parts)

    def __str__(self) -> str:
        return self.path

    @classmethod
    def parse(cls, path: str) -> ViewReference:
        """Parse a canonical path back into a reference. Inverse of ``path``."""
        return parse_view_reference(path)


def parse_view_reference(path: str) -> ViewReference:
    """Parse one of the five canonical path shapes (plus the step-scoped panel).

    The five shapes in contracts/web-read-api.md's resolution table are run,
    turn, step, turn-scoped panel, and run-scoped panel. data-model.md SS12
    additionally describes ``/steps/{step_index}`` being *inserted* into the
    panel shape for a step-scoped panel; that sixth shape is accepted here, so
    a ``scope: step`` panel in the registry has a reference form at all. (The
    route table does not list it -- recorded as a contract gap rather than
    resolved silently.)
    """
    if not path:
        raise ViewReferenceError("empty view reference")
    candidate = path.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not candidate.startswith("/"):
        candidate = "/" + candidate
    if candidate in ("/runs", "/"):
        raise ViewReferenceError(
            f"{path!r} names a collection, not a view reference: every reference "
            f"resolves to a run, turn, step, or panel"
        )

    for pattern in _PATTERNS:
        matched = pattern.match(candidate)
        if matched is None:
            continue
        groups = matched.groupdict()
        turn = groups.get("turn")
        step = groups.get("step")
        panel_id = groups.get("panel_id")
        try:
            return ViewReference(
                run_id=unquote(groups["run_id"]),
                turn_number=int(turn) if turn is not None else None,
                step_index=int(step) if step is not None else None,
                panel_id=unquote(panel_id) if panel_id is not None else None,
            )
        except ValueError as exc:
            raise ViewReferenceError(f"{path!r} is not a valid view reference: {exc}") from exc

    raise ViewReferenceError(
        f"{path!r} does not match any canonical view-reference shape "
        f"(/runs/<run>, /runs/<run>/turns/<turn>, .../steps/<step>, "
        f".../panels/<panel>, /runs/<run>/panels/<panel>)"
    )
