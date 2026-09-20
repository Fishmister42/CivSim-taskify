"""The FastAPI application (T014).

Assembles the four foundational pieces into one process: the read-only store
seam, the Panel Registry, the Jinja2 environment the content-negotiation seam
renders through, and the resolved bind addresses. Nothing here has a route that
writes, and nothing here calls 002's operator surface -- this feature presents;
the operator surface commands, and the two never meet (plan.md Constraints).

``/healthz`` is the only route this phase registers. It is deliberately an
*operational* route rather than a run-state one: it reports this service's own
liveness and the configured store's ``ping()``, and it is the one route that
still answers when the store is unreachable (contracts/web-read-api.md error
table).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from civsim_web.negotiate.respond import respond
from civsim_web.net.bind import resolve_bind_addresses
from civsim_web.registry.loader import PanelRegistry, load_panel_registry
from civsim_web.routes import include_routers
from civsim_web.routes.common import ERROR_TEMPLATE, WebError
from civsim_web.store_client.port import MatchStore
from civsim_web.viewmodels.base import PanelRegistryVersion, ViewModel

__all__ = [
    "ServiceHealthView",
    "StoreHealthView",
    "create_app",
    "get_registry",
    "get_store",
    "static_dir",
    "templates_dir",
]

logger = logging.getLogger("civsim_web")


def templates_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


class StoreHealthView(ViewModel):
    """The configured store's ``ping()`` result, verbatim."""

    ok: bool
    detail: str | None = None
    checked_at: datetime | None = None


class ServiceHealthView(ViewModel):
    """``GET /healthz`` -- operational, not a run-state route.

    Carries the Panel Registry version for the same reason every run-state
    response does (invariant V10): an operator diagnosing a parity question
    needs to know which registry version the running process actually loaded,
    and asking the process is more reliable than reading the file on disk next
    to it.
    """

    service: str = "civsim_web"
    ok: bool
    store: StoreHealthView
    panel_registry: PanelRegistryVersion
    bind_addresses: tuple[str, ...] = ()
    checked_at: datetime


def get_store(request: Request) -> MatchStore:
    """The configured ``MatchStore``. Reads only -- see ``store_client/port.py``."""
    store: MatchStore = request.app.state.store
    return store


def get_registry(request: Request) -> PanelRegistry:
    """The validated Panel Registry in force for this process."""
    registry: PanelRegistry = request.app.state.registry
    return registry


def _registry_version(registry: PanelRegistry) -> PanelRegistryVersion:
    return PanelRegistryVersion(
        version=registry.version,
        content_hash=registry.content_hash,
        panel_ids=registry.panel_ids,
    )


def create_app(
    *,
    store: MatchStore,
    registry: PanelRegistry | None = None,
    bind_addresses: Sequence[str] | None = None,
    panels_dir: Path | None = None,
) -> FastAPI:
    """Build the application.

    ``registry`` is loaded here when not supplied, and a
    ``PanelRegistryLoadError`` is deliberately **not** caught: a registry that
    fails validation aborts startup rather than serving a partially-validated
    parity boundary (contracts/panel-registry.md, quickstart Setup).

    ``bind_addresses`` is resolved and logged but not bound here -- binding is
    ``uvicorn``'s job, driven by ``cli.py serve``. Resolving at app-build time
    means a wildcard request is refused before a socket exists.
    """
    loaded_registry = registry if registry is not None else load_panel_registry(panels_dir)
    resolved = tuple(resolve_bind_addresses(bind_addresses))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # FR-028/FR-029: log every bound address so the operator verifies
        # reachability by inspection rather than by assumption (research R8).
        for address in app.state.bind_addresses:
            logger.info("civsim_web bound address: %s", address)
        logger.info(
            "civsim_web panel registry: version=%s content_hash=%s panels=%d",
            app.state.registry.version,
            app.state.registry.content_hash[:12],
            len(app.state.registry.panel_ids),
        )
        yield

    app = FastAPI(
        title="CivSim Unified Web Interface",
        description=(
            "Read-only, LAN-bound, unauthenticated view of CivSim run data. "
            "Every route serves one view model to both the user's browser and "
            "the directing Claude Code session (Principle VI)."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.store = store
    app.state.registry = loaded_registry
    app.state.bind_addresses = resolved
    app.state.jinja_env = Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=select_autoescape(("html", "xml")),
        # StrictUndefined: a template referencing a field the view model does
        # not have fails loudly instead of rendering an empty string, which
        # would look exactly like a legitimately absent value (UP-005) and
        # would silently break the JSON/HTML field-parity contract.
        undefined=StrictUndefined,
    )

    directory = static_dir()
    directory.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(directory)), name="static")

    @app.get("/healthz")
    def healthz(request: Request) -> Response:
        """This service's liveness plus the configured store's ``ping()``.

        Never raises on an unreachable store: ``ping()`` is a check, and its
        result is the answer this route exists to report.
        """
        registry_in_force = get_registry(request)
        try:
            health: Any = get_store(request).ping()
            store_view = StoreHealthView(
                ok=bool(health.ok),
                detail=getattr(health, "detail", None),
                checked_at=getattr(health, "checked_at", None),
            )
        except Exception as exc:  # a store that raises is still "not ok"
            store_view = StoreHealthView(ok=False, detail=f"ping raised: {exc}")

        view = ServiceHealthView(
            ok=store_view.ok,
            store=store_view,
            panel_registry=_registry_version(registry_in_force),
            bind_addresses=tuple(request.app.state.bind_addresses),
            checked_at=datetime.now(UTC),
        )
        # 200 either way: this route reports health, it does not fail on it.
        # A caller distinguishes reachable-store from unreachable-store by
        # reading `store.ok`, not by a status code that would make an
        # unreachable store look like a broken endpoint.
        return respond(request, view)

    @app.exception_handler(WebError)
    def web_error(request: Request, exc: Exception) -> Response:
        """Errors go through the *same* seam as successes.

        A 404 rendered as a page for the browser and as JSON for the directing
        session is still one view model serialized two ways, so Principle VI
        holds on the unhappy path too -- the two parties never get different
        accounts of why something is not there.
        """
        error = exc if isinstance(exc, WebError) else WebError(500, _unexpected(exc))
        return respond(request, error.view, ERROR_TEMPLATE, status_code=error.status_code)

    include_routers(app)
    return app


def _unexpected(exc: Exception) -> Any:
    from civsim_web.routes.common import ErrorView

    return ErrorView(kind="internal_error", message=str(exc))
