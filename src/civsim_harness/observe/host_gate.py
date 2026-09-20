"""Host-capability preflight tier gate (T101).

T101 is specified in tasks.md to live in ``src/civsim_harness/run/preparation.py``, which another
wave of this swarm owns concurrently. This module implements T101's logic as a self-contained,
side-effect-free function with a clean signature that ``preparation.py`` can call directly, rather
than editing a file outside this wave's ownership. See :func:`evaluate_host_gate`'s docstring for
the exact call-site contract; the implementer report for this wave restates the signature for
wiring.

Resolves the host's :class:`~civsim_harness.host.detect.SupportTier` (T049) and refuses a run
before turn 1 when the host is ``UNSUPPORTED`` -- FR-054 grounds this in FR-007 (no verified
quicksave path means a single turn can never complete on this host), not FR-002, which governs
configured elements rather than host capability. A merely *degraded* host (``SUPPORTED``: quicksave
verified, capture-hygiene spike not passed) is never refused -- it starts, and its runs are recorded
``visually_degraded`` under FR-050. That split is exactly what FR-054's last sentence draws, and is
the whole of what this function decides.
"""

from __future__ import annotations

from dataclasses import dataclass

from civsim_harness.errors import PreflightError
from civsim_harness.host.detect import SupportProbeResult, SupportTier, resolve_support_tier
from civsim_harness.models.run import ComparabilityStatus, HostSupportTier


@dataclass(frozen=True)
class HostGateResult:
    """The recordable outcome of the T101 gate for a host that may start a run.

    Never constructed for an ``UNSUPPORTED`` host -- :func:`evaluate_host_gate` raises instead of
    returning one, so a caller can never accidentally treat a refusal as a startable result.
    """

    tier: HostSupportTier
    comparability_status: ComparabilityStatus


def evaluate_host_gate(probe: SupportProbeResult) -> HostGateResult:
    """Resolve *probe* to a run-startable :class:`HostGateResult`, or refuse the run.

    Call this once during preparation, before ``Run.host_support_tier`` /
    ``Run.comparability_status`` are set and before turn 1 begins.

    Raises :class:`~civsim_harness.errors.PreflightError` when *probe* resolves to
    ``SupportTier.unsupported`` -- **recording which capability was missing** in
    ``exc.detail["missing_capability"]`` (always ``"quicksave_path_verified"``: it is the only
    capability that can drive this tier to ``unsupported``, per
    :func:`civsim_harness.host.detect.resolve_support_tier`) and the probe's own
    ``exc.detail["reason"]`` narrative, per FR-054's own wording ("refuse ... before turn 1,
    recording which capability was missing").

    Never raises for ``SupportTier.supported`` or ``SupportTier.validated`` -- a merely degraded
    host must start (FR-054's last sentence): ``supported`` maps to
    ``HostSupportTier.SUPPORTED`` / ``ComparabilityStatus.VISUALLY_DEGRADED``, and ``validated``
    maps to ``HostSupportTier.VALIDATED`` / ``ComparabilityStatus.COMPARABLE``.

    **Intended call site** (``src/civsim_harness/run/preparation.py``, owned by another wave):
    resolve a :class:`~civsim_harness.host.detect.SupportProbeResult` for the current host (T049's
    own probing over the adapter's capabilities -- quicksave-path verification and the R6 capture
    spike), call this function, and on success set both ``Run.host_support_tier = result.tier`` and
    ``Run.comparability_status = result.comparability_status`` on the ``Run`` under construction.
    On :class:`~civsim_harness.errors.PreflightError`, abort preparation before creating the run and
    surface ``exc.detail["missing_capability"]`` / ``exc.detail["reason"]`` to the operator (V13).
    This function has no store handle and writes nothing itself -- if the caller already has a
    partially-recorded run/event trail at that point, recording a ``preparation_mismatch``
    ``RunEvent`` carrying the same detail is the caller's responsibility, not this function's.
    """
    tier = resolve_support_tier(probe)

    if tier is SupportTier.unsupported:
        raise PreflightError(
            "host support tier is UNSUPPORTED: no verified quicksave path exists on this host, "
            "so FR-007 makes completing even a single turn impossible here (FR-054, V13)",
            detail={
                "missing_capability": "quicksave_path_verified",
                "reason": probe.reason,
            },
        )

    if tier is SupportTier.validated:
        return HostGateResult(
            tier=HostSupportTier.VALIDATED,
            comparability_status=ComparabilityStatus.COMPARABLE,
        )

    # SupportTier.supported: quicksave verified, capture-hygiene spike not passed.
    return HostGateResult(
        tier=HostSupportTier.SUPPORTED,
        comparability_status=ComparabilityStatus.VISUALLY_DEGRADED,
    )
