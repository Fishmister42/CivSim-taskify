"""``civsim-web doctor`` and ``civsim-web serve`` (T015).

Two commands, matching quickstart.md:

``doctor``
    Preflight. Reports store reachability, Panel Registry load status, and the
    resolved bind addresses -- the three things that can be wrong before a
    single page is served. Exits non-zero when any of them is, so it is usable
    as a gate rather than only as a report.

``serve``
    Starts the application, binding every resolved private address (LAN *and*
    loopback, one server each) and logging all of them.

Built on ``argparse`` rather than a CLI framework: this feature's dependency
group is deliberately small, and two commands with four options between them do
not justify pulling another library into a process whose whole job is to read
and render.

**Store selection is configuration, not code** (quickstart Scenario 6). The
``--store`` option (or ``CIVSIM_WEB_STORE``) takes either ``fake`` or a
``module:factory`` reference that returns something satisfying the read-only
``MatchStore`` Protocol. Swapping the bundled fake for 002's reference adapter,
or for deliverable 3's store, is expected to be this value changing and nothing
else -- if it ever requires touching ``viewmodels/`` or ``routes/``, that is
itself a finding: a dependency leaked past ``store_client/``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from civsim_web.net.bind import DEFAULT_PORT, WildcardBindRefused, resolve_bind_addresses
from civsim_web.registry.coverage import assess_registry_coverage
from civsim_web.registry.harness_schema import load_harness_schema
from civsim_web.registry.loader import (
    PanelRegistryLoadError,
    default_harness_data_model_path,
    default_panels_dir,
    load_panel_registry,
)

__all__ = ["CONTRACT_ROUTES", "build_parser", "main", "resolve_store"]

logger = logging.getLogger("civsim_web.cli")

_STORE_ENV = "CIVSIM_WEB_STORE"

#: Every path ``contracts/web-read-api.md`` promises, transcribed from its route
#: and view-reference tables rather than derived from ``ROUTER_MODULES``.
#:
#: Deriving it from the code would make this check vacuous -- it would compare
#: the application to itself and pass forever, which is the exact defect shape
#: this project keeps finding. Transcribing it means ``doctor`` fails when a
#: router stops registering, or when someone renames a path the contract names.
#:
#: The last two shapes are served by this feature and appear in the contract's
#: **view-reference** table rather than its route table: the run-scoped panel
#: shape, and the step-scoped one (see ``routes/panels.py``).
#:
#: *Corrected 2026-09-21 (T074).* This comment used to say the step-scoped shape
#: was "in ``data-model.md`` SS12 and in neither table", which was true when it
#: was written and stopped being true the same day: the 2026-09-20 amendment
#: added that row to the view-reference table, precisely because twenty-two of
#: the thirty-seven shipped panels are ``scope: step`` and would otherwise have
#: had no documented URL.
CONTRACT_ROUTES: tuple[str, ...] = (
    "/",
    "/runs",
    "/runs/{run_id}",
    "/runs/{run_id}/turns/{turn_number}",
    "/runs/{run_id}/turns/{turn_number}/steps/{step_index}",
    "/runs/{run_id}/turns/{turn_number}/panels/{panel_id}",
    "/runs/{run_id}/events",
    "/runs/{run_id}/metrics",
    "/captures/{capture_id}/image",
    "/compare",
    "/healthz",
    "/runs/{run_id}/panels/{panel_id}",
    "/runs/{run_id}/turns/{turn_number}/steps/{step_index}/panels/{panel_id}",
)


def resolve_store(reference: str) -> Any:
    """Build the configured ``MatchStore``.

    ``fake`` yields an empty bundled fake -- useful for ``doctor`` and for a
    first ``serve`` before any run exists. Anything else is read as
    ``module:attribute``; the attribute is called with no arguments if it is
    callable, and used as-is otherwise.
    """
    if reference in ("", "fake"):
        from civsim_web.store_client.fake import FakeMatchStore

        return FakeMatchStore()
    if ":" not in reference:
        raise ValueError(
            f"store reference {reference!r} must be 'fake' or 'module:attribute'"
        )
    module_name, _, attribute = reference.partition(":")
    module = importlib.import_module(module_name)
    target = getattr(module, attribute)
    return target() if callable(target) else target


def _doctor(args: argparse.Namespace) -> int:
    """Preflight, in quickstart.md's output shape."""
    lines: list[str] = []
    ok = True
    store: Any = None

    # -- store ---------------------------------------------------------------
    try:
        store = resolve_store(args.store)
        health = store.ping()
        if getattr(health, "ok", False):
            lines.append("store             : ok (ping succeeded)")
        else:
            ok = False
            lines.append(f"store             : FAILED ({getattr(health, 'detail', 'not ok')})")
    except Exception as exc:
        ok = False
        lines.append(f"store             : FAILED ({exc})")

    # -- panel registry ------------------------------------------------------
    #
    # A registry that fails to load aborts startup; `doctor` reports the same
    # failure rather than letting `serve` be the first place it is noticed.
    registry = None
    try:
        panels_dir = args.panels or default_panels_dir()
        registry = load_panel_registry(panels_dir)
        frozen = "frozen" if (panels_dir / "VERSION.lock").is_file() else "NOT FROZEN"
        lines.append(
            f"panel registry    : ok (version {registry.version}, "
            f"{len(registry.panel_ids)} panels, {frozen})"
        )
        if frozen != "frozen":
            # Not a failure -- a version under authorship legitimately has no
            # lock yet -- but it must be said out loud, because while it is
            # absent rule P6's immutability check is simply not running.
            lines.append(
                "                    WARNING: no panels/VERSION.lock, so rule P6's "
                "immutability check is not in force"
            )
    except PanelRegistryLoadError as exc:
        ok = False
        lines.append(f"panel registry    : FAILED {exc}")

    # -- registry coverage ---------------------------------------------------
    #
    # The number below is computed (registry/coverage.py), not asserted. It was
    # a literal `0` until T062: a preflight that prints its own conclusion
    # regardless of the facts is worse than no preflight, because it reads as
    # evidence.
    if registry is not None:
        try:
            schema = load_harness_schema(default_harness_data_model_path())
            report = assess_registry_coverage(registry, schema)
            status = "ok" if report.ok else "FAILED"
            ok = ok and report.ok
            lines.append(f"registry coverage : {status} ({report.summary()})")
            for leaked in report.reachable_but_unregistered:
                lines.append(
                    f"                    {leaked} renders as {leaked.rendered_as} "
                    f"on the {'/'.join(leaked.views)} view with no panel permitting it"
                )
        except Exception as exc:
            ok = False
            lines.append(f"registry coverage : FAILED ({exc})")

    # -- routes --------------------------------------------------------------
    #
    # T062: the preflight reaches the routes Phases 3-6 built, not only the
    # foundation. Building the app here is the check -- a router that fails to
    # import, or a route the contract names and nothing registers, is a
    # `doctor` failure rather than a 404 someone meets later.
    try:
        from civsim_web.app import create_app

        # The store built above is reused rather than resolved a second time: a
        # real `module:factory` may open a connection, and a preflight that
        # opened two would be reporting on a process shape that never exists.
        app = create_app(
            store=store if store is not None else resolve_store(args.store),
            registry=registry,
            bind_addresses=args.bind or None,
            panels_dir=args.panels,
        )
        served = set(app.openapi()["paths"])
        missing = [path for path in CONTRACT_ROUTES if path not in served]
        if missing:
            ok = False
            lines.append(
                f"routes            : FAILED ({len(missing)} contract route(s) not "
                f"registered: {', '.join(missing)})"
            )
        else:
            lines.append(
                f"routes            : ok ({len(served)} registered, "
                f"all {len(CONTRACT_ROUTES)} contract routes present)"
            )
    except Exception as exc:
        ok = False
        lines.append(f"routes            : FAILED ({exc})")

    # -- bind addresses ------------------------------------------------------
    try:
        addresses = resolve_bind_addresses(args.bind or None)
        rendered = ", ".join(f"{a}:{args.port}" for a in addresses)
        shape = "LAN + loopback" if len(addresses) > 1 else "loopback only"
        lines.append(f"bind address(es)  : {rendered}   ({shape} -- no wildcard)")
    except WildcardBindRefused as exc:
        ok = False
        lines.append(f"bind address(es)  : FAILED ({exc})")

    for line in lines:
        print(line)
    if not ok:
        print("\npreflight FAILED -- civsim-web will not serve until these are resolved")
    return 0 if ok else 1


async def _serve_all(app: Any, addresses: Sequence[str], port: int) -> None:
    """One ``uvicorn`` server per resolved address, concurrently.

    ``uvicorn`` binds a single host per server, and this feature must be
    reachable on the LAN address *and* on loopback (FR-028). Running one server
    per address is the explicit way to get both without a wildcard bind, which
    is the thing ``net/bind.py`` refuses outright.
    """
    import uvicorn

    servers = [
        uvicorn.Server(uvicorn.Config(app, host=address, port=port, log_level="info"))
        for address in addresses
    ]
    await asyncio.gather(*(server.serve() for server in servers))


def _serve(args: argparse.Namespace) -> int:
    from civsim_web.app import create_app

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    store = resolve_store(args.store)
    addresses = resolve_bind_addresses(args.bind or None)
    app = create_app(store=store, bind_addresses=addresses, panels_dir=args.panels)
    for address in addresses:
        logger.info("serving on http://%s:%d", address, args.port)
    asyncio.run(_serve_all(app, addresses, args.port))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="civsim-web",
        description="Read-only web interface over CivSim match-tracking data.",
    )
    parser.add_argument(
        "--store",
        default=os.environ.get(_STORE_ENV, "fake"),
        help="'fake' or 'module:attribute' returning a read-only MatchStore "
        f"(env: {_STORE_ENV})",
    )
    parser.add_argument(
        "--panels",
        type=Path,
        default=None,
        help="Panel Registry directory (default: panels/ at the repository root)",
    )
    parser.add_argument(
        "--bind",
        action="append",
        default=[],
        help="explicit private address to bind; repeatable. A wildcard is refused",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="preflight: store, panel registry, bind addresses")
    subparsers.add_parser("serve", help="start the service")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "serve":
        return _serve(args)
    raise AssertionError(f"unreachable command {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
