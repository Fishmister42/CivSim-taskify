"""The harness exception hierarchy (T013).

Every exception below accepts an optional structured ``detail`` mapping and
redacts it (via :mod:`civsim_harness.telemetry.redaction`) *at construction
time* -- not later, when something happens to log or serialize it. That
ordering is deliberate: it means the raw value is never retained anywhere
reachable from the exception object at all, so a plain ``str(exc)``, a
standard ``traceback.format_exception`` (which prints the exception's own
message, not local variable values), or a downstream store/log write can
never surface it even if the code that raises the exception forgets that
redaction exists (FR-043, SC-018, invariant I8).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from civsim_harness.telemetry.redaction import redact_value


class HarnessError(Exception):
    """Base class for every harness-raised error."""

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        safe_message = redact_value(message)
        safe_detail = redact_value(dict(detail)) if detail is not None else {}
        if not isinstance(safe_detail, dict):  # pragma: no cover - defensive
            safe_detail = {}
        self.message: str = safe_message
        self.detail: dict[str, Any] = safe_detail
        rendered = f"{safe_message} | detail={safe_detail}" if safe_detail else safe_message
        super().__init__(rendered)


class PreflightError(HarnessError):
    """A run configuration or client state failed a preflight check (FR-001, FR-002)."""


class CatalogError(HarnessError):
    """The capability catalog failed to load or validate (contracts/capability-catalog.md)."""


class NexusError(HarnessError):
    """The Nexus wire transport to the game client failed (contracts/nexus-protocol.md)."""


class StoreWriteError(HarnessError):
    """A write to the match-tracking store failed and must halt the run (FR-013, D2)."""


class StoreReadError(HarnessError):
    """A match-tracking store read could not be answered -- an unknown run asked for by an
    operation that has no honest empty answer (003 contracts/match-tracking-store.md)."""


class StoreSchemaError(HarnessError):
    """The store file's schema version cannot be handled by this reader: a newer major version
    (003 FR-026) or a migration that a read-only opener may not perform (003 FR-024)."""


class BundleError(HarnessError):
    """A run bundle could not be written, read or imported: a run-id collision, a hash mismatch,
    a missing image, or a newer bundle format (003 FR-027)."""


class ParityViolation(HarnessError):
    """Something reached, or would have reached, the agent outside the parity boundary (FR-016)."""


class ProviderChainExhausted(HarnessError):
    """Every model in the configured primary + fallback chain failed (FR-042, SC-012)."""


class RecoveryLimitReached(HarnessError):
    """The consecutive-failure recovery bound was exceeded (FR-048)."""


class ObservationAssemblyError(HarnessError):
    """A decision step's observation could not be assembled (FR-046, spec edge case)."""


class BuildMismatchError(HarnessError):
    """The client's reported game build does not match the seed set's pinned build (FR-002, R20)."""


class DiskHeadroomError(HarnessError):
    """Free disk space fell below the configured floor (min_free_disk_gb, R17)."""
