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

``metrics`` (``/runs/{run_id}/metrics``) landed with US3 (T042). It is
registered after ``turns`` only for readability -- ``/runs/{id}/metrics`` and
``/runs/{id}/turns/{n}`` are distinct literal segments and neither shadows the
other.

``panels`` landed with US2 (T035, T038) and carries three paths, not two: the
run- and turn-scoped shapes the contract's table names, plus the step-scoped
one ``data-model.md`` SS12 describes and that table omits (see the module's own
docstring -- 22 of the 37 shipped panels are ``scope: step`` and would
otherwise have no resolvable URL).

``catalog`` (``/runs``) and ``compare`` (``/compare``) landed with US4. Note
that ``catalog`` is registered *after* ``live``: ``live`` owns ``/`` and
``/runs/{run_id}``, and ``/runs`` is a distinct literal path, so the two do not
shadow one another -- but the ordering is the one stated above and should stay
that way.
"""

from __future__ import annotations

from importlib import import_module

from fastapi import FastAPI

__all__ = ["ROUTER_MODULES", "include_routers"]

#: Module names under ``civsim_web.routes``, each exporting ``router``.
ROUTER_MODULES: tuple[str, ...] = (
    "live",
    "turns",
    # US3 (T042) -- the single-run metric trajectory FR-015 navigates from.
    "metrics",
    "events",
    "captures",
    # US2 (T035, T038) -- the ViewReference resolution targets.
    "panels",
    # US4 (T051, T053).
    "catalog",
    "compare",
)


def include_routers(app: FastAPI) -> None:
    """Mount every registered router onto ``app``."""
    for name in ROUTER_MODULES:
        module = import_module(f"civsim_web.routes.{name}")
        app.include_router(module.router)
