"""Fixtures for `001-unified-web-interface` (`src/civsim_web/`) -- T004.

Deliberately thin. Every import happens *inside* a fixture body, never at
module scope, so a problem in `civsim_web` can only fail the tests that ask
for one of these fixtures -- it can never break collection for the
`002-civ-playing-harness` tests that share this directory.

The fixtures themselves live in `tests/web_support/fixtures.py`, which is this
feature's own package (the counterpart to 002's `tests/fakes/`).
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def web_store() -> Any:
    """A `MatchStore` fake seeded with one three-turn run."""
    from web_support.fixtures import make_store

    return make_store()


@pytest.fixture
def web_store_factory() -> Any:
    """`make_store(...)` itself, for a test that needs a specific shape."""
    from web_support.fixtures import make_store

    return make_store


@pytest.fixture
def web_app(web_store: Any) -> Any:
    """The FastAPI app over `web_store`, bound to loopback."""
    from web_support.fixtures import make_app

    return make_app(web_store)


@pytest.fixture
def web_client(web_app: Any) -> Any:
    """A `TestClient` over `web_app`."""
    from fastapi.testclient import TestClient

    with TestClient(web_app) as client:
        yield client
