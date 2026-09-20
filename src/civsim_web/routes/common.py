"""What every route in this feature shares (US1, extended by US2-US4).

Four things live here because having them in one place is what keeps the routes
themselves thin enough to read:

- **The store and registry handles.** Routes take them off ``app.state`` rather
  than importing the port, so ``store_client/`` stays the only module that names
  the ``MatchStore`` type (T005, enforced by
  ``tests/contract/test_read_only_boundary.py``).
- **``WebError``**, the one error type. Every failure a route can produce --
  no such run, no such turn, a gap, an unreachable store -- is raised as this
  and rendered through the *same* content-negotiation seam as a success, so an
  error page and an error JSON body are the same object twice, exactly like
  every other response (contracts/web-read-api.md).
- **``require_store``**, which enforces the contract's blanket rule: an
  unreachable store is ``503`` on every route except ``/healthz``, with a body
  that distinguishes "store unreachable" from "run not found" so a client does
  not confuse the two.
- **``load_run_context``**, the read every run-scoped route starts with.

**For a later story adding a route**: raise ``WebError`` rather than FastAPI's
``HTTPException`` (the latter bypasses the negotiation seam and would answer a
browser with raw JSON), start with ``load_run_context``, and register the new
router in ``routes/__init__.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request

from civsim_web.registry.loader import PanelRegistry
from civsim_web.store_client import reads
from civsim_web.viewmodels.base import PanelRegistryVersion, ViewModel
from civsim_web.viewmodels.provenance import Provenance, build_provenance

__all__ = [
    "ERROR_TEMPLATE",
    "ErrorView",
    "RunContext",
    "WebError",
    "load_run_context",
    "registry_of",
    "registry_version",
    "require_store",
    "store_of",
]

ERROR_TEMPLATE = "error.html"


class ErrorView(ViewModel):
    """An error, as a view model -- so both readers get the same explanation.

    ``kind`` is the machine-readable discriminator the contract's error table
    asks for: a client must be able to tell "store unreachable" from "run not
    found", and a superseded turn from a turn that never existed, without
    parsing prose.
    """

    error: str = "error"
    kind: str
    message: str
    run_id: str | None = None
    turn_number: int | None = None
    detail: dict[str, Any] = {}


class WebError(Exception):
    """A route failure, carrying the view model that will be rendered for it."""

    def __init__(self, status_code: int, view: ErrorView) -> None:
        super().__init__(f"{status_code} {view.kind}: {view.message}")
        self.status_code = status_code
        self.view = view

    @classmethod
    def no_such_run(cls, run_id: str) -> WebError:
        return cls(
            404,
            ErrorView(
                kind="run_not_found",
                message="No such run.",
                run_id=run_id,
            ),
        )

    @classmethod
    def store_unreachable(cls, detail: str | None) -> WebError:
        return cls(
            503,
            ErrorView(
                kind="store_unreachable",
                message=(
                    "The match-tracking store is not reachable. This is not the same "
                    "as a run that does not exist -- nothing can be read right now."
                ),
                detail={"store_detail": detail} if detail else {},
            ),
        )


def store_of(request: Request) -> Any:
    """The configured store. Reads only -- see ``store_client/port.py``."""
    return request.app.state.store


def registry_of(request: Request) -> PanelRegistry:
    """The validated Panel Registry in force for this process."""
    registry: PanelRegistry = request.app.state.registry
    return registry


def registry_version(registry: PanelRegistry) -> PanelRegistryVersion:
    return PanelRegistryVersion(
        version=registry.version,
        content_hash=registry.content_hash,
        panel_ids=registry.panel_ids,
    )


def require_store(request: Request) -> Any:
    """The store, or ``503`` -- the contract's rule for every non-health route."""
    store = store_of(request)
    ok, detail = reads.store_is_reachable(store)
    if not ok:
        raise WebError.store_unreachable(detail)
    return store


@dataclass(frozen=True)
class RunContext:
    """The reads every run-scoped route needs before it can build anything."""

    store: Any
    registry: PanelRegistry
    run: Any
    run_id: str
    events: tuple[Any, ...]
    provenance: Provenance

    @property
    def registry_version(self) -> PanelRegistryVersion:
        return registry_version(self.registry)


def load_run_context(request: Request, run_id: str, *, with_events: bool = True) -> RunContext:
    """Resolve a ``run_id`` to its record, timeline, and provenance stamp.

    Raises ``WebError`` for an unreachable store (``503``) or a run that never
    existed (``404``) -- the two cases the contract insists a client be able to
    tell apart.
    """
    store = require_store(request)
    registry = registry_of(request)
    run = store.get_run(run_id)
    if run is None:
        raise WebError.no_such_run(run_id)
    events = reads.list_run_events(store, run_id) if with_events else ()
    return RunContext(
        store=store,
        registry=registry,
        run=run,
        run_id=run_id,
        events=events,
        provenance=build_provenance(registry, run),
    )
