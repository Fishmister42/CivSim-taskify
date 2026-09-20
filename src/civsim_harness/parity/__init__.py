"""Principle I's enforcement point: the structural filter, the forbidden-field
guard, and the four image-screening gates.

- :mod:`civsim_harness.parity.filter` -- the structural parity filter
  (T127): ``CapabilityResult`` is the only input shape accepted, and
  ``filter_to_entries`` the producer of attributed observation entries from
  it (research R9 rule 1, FR-018).
- :mod:`civsim_harness.parity.forbidden` -- the forbidden-field guard
  (T128): a red-team list of game-state-leakage (FR-019) and harness-
  telemetry (FR-020) fields/values, asserted at runtime against every
  assembled context once per decision step.
- :mod:`civsim_harness.parity.screening` -- the four image-screening gates
  (T129, research R7): source, geometry, provenance, and content, each
  independently capable of withholding a capture before it ever reaches the
  agent or the store (FR-025, FR-030, SC-019).

Camera validation and execution (T132) lives in ``act/camera.py``, a
different task and a different owner; it is not part of this package.
"""
