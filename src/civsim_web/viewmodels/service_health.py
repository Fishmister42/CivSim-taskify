"""``GET /healthz``'s view models (T066).

These two models used to be declared in ``app.py``, beside the route that
serves them. That reads naturally and was wrong for one structural reason: the
FR-030 credential audit in ``tests/contract/test_web_read_api.py`` walks
``civsim_web.viewmodels``, so a view model declared anywhere else is outside the
audit while its own docstring claims to cover "every view model reachable from a
registered route's response". ``/healthz`` is also absent from the ``ROUTES``
parity matrix (it has no HTML rendering of its own), so the response scan missed
it too -- and it is the one route that renders a **real store's connection
state**, which is exactly where a DSN or bearer token surfaces.

Living in this package is therefore not tidiness. It is what puts these models
inside the audit by construction rather than by someone remembering to widen a
scan, the same reasoning the Panel Registry uses against store fields.

``/healthz`` stays operational rather than run-state: it reports this service's
liveness and the configured store's ``ping()``, and it is the one route that
still answers when the store is unreachable (contracts/web-read-api.md error
table).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import field_validator

from civsim_web.redact import redact_secrets
from civsim_web.viewmodels.base import PanelRegistryVersion, ViewModel

__all__ = ["ServiceHealthView", "StoreHealthView"]


class StoreHealthView(ViewModel):
    """The configured store's ``ping()`` result.

    ``detail`` is the one field in this feature that renders **free-form text
    produced by something else** -- the store's own message, or the string form
    of whatever its ``ping()`` raised. Every other rendered value is a declared
    field read through the Panel Registry gate, which is what normally makes
    FR-030 structural; there is no declaration behind an exception message, so
    the gate has nothing to check and this boundary needs its own pass.

    FR-028 makes that pass load-bearing rather than cautious: this page is
    readable, unauthenticated, by any device on the LAN, so a connection error
    naming ``postgresql://civsim:hunter2@host/db`` would be *published*. The
    validator below is FR-030's second gate, in the same spirit as
    ``CaptureView``'s second screening gate -- it does not assume the store
    redacted its own message.
    """

    ok: bool
    detail: str | None = None
    checked_at: datetime | None = None

    @field_validator("detail")
    @classmethod
    def _redact(cls, value: str | None) -> str | None:
        """Applied on the model, not at the call site, on purpose.

        A redaction that lives in the route handler is one a second call site
        can forget. Enforcing it in the validator means *every* construction of
        this model is redacted, including one a future route or test writes.
        """
        return redact_secrets(value)


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
