"""Route registration -- the one place a new router is added.

``app.py`` calls ``include_routers(app)`` and never learns which routers exist.
That is the seam that keeps later stories from all editing ``app.py``: a story
adding a route appends one line to ``ROUTER_MODULES`` below and touches nothing
else in the application wiring.

**Order matters once.** ``live`` is first because it owns ``/`` and
``/runs/{run_id}``; a router registering a conflicting path later would be
shadowed rather than rejected, and having the order stated in one list makes
that visible. Within the rest, order is irrelevant -- FastAPI matches the more
specific path regardless of registration order for these shapes.

Pending, by story:

- **US2** -- ``panels`` (T035, T038): ``/runs/{run_id}/turns/{turn}/panels/{panel_id}``
  and ``/runs/{run_id}/panels/{panel_id}``.
- **US3** -- ``metrics`` (T042): ``/runs/{run_id}/metrics``.
- **US4** -- ``catalog`` (T051): ``/runs``; ``compare`` (T053): ``/compare``.
"""

from __future__ import annotations

from importlib import import_module

from fastapi import FastAPI

__all__ = ["ROUTER_MODULES", "include_routers"]

#: Module names under ``civsim_web.routes``, each exporting ``router``.
ROUTER_MODULES: tuple[str, ...] = (
    "live",
    "turns",
    "events",
    "captures",
)


def include_routers(app: FastAPI) -> None:
    """Mount every registered router onto ``app``."""
    for name in ROUTER_MODULES:
        module = import_module(f"civsim_web.routes.{name}")
        app.include_router(module.router)
