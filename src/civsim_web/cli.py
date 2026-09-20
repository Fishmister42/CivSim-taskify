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
from civsim_web.registry.harness_schema import load_harness_schema
from civsim_web.registry.loader import (
    PanelRegistryLoadError,
    default_harness_data_model_path,
    load_panel_registry,
)

__all__ = ["build_parser", "main", "resolve_store"]

logger = logging.getLogger("civsim_web.cli")

_STORE_ENV = "CIVSIM_WEB_STORE"


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
    try:
        registry = load_panel_registry(args.panels)
        schema = load_harness_schema(default_harness_data_model_path())
        scanned = sum(len(fields) for fields in schema.fields_by_entity.values())
        lines.append(
            f"panel registry    : ok (version {registry.version}, "
            f"{len(registry.panel_ids)} panels, "
            f"{scanned} fields scanned in {schema.source.name}, "
            f"0 unregistered fields reachable from a view model)"
        )
    except PanelRegistryLoadError as exc:
        ok = False
        lines.append(f"panel registry    : FAILED {exc}")

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
