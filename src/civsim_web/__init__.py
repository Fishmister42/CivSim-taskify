"""CivSim deliverable 1 -- the unified web interface.

A **read-only** presentation layer over the match-tracking data deliverable 2
(`civsim_harness`) writes. This package has no domain logic of its own: every
fact it shows is already in the store behind 002's ``MatchStore`` port, already
parity-filtered before it arrives.

Three structural rules hold across every module here and are stated once:

1. **Read-only, structurally.** ``store_client/port.py`` is the only module
   permitted to name the ``MatchStore`` type, and it re-declares *only* the
   port's read operations. None of the port's nine write operations has a name
   anywhere in this package (FR-023, FR-024, FR-026).
2. **Parity is a second, independent gate.** ``registry/`` loads the Panel
   Registry (``panels/*.yaml``); a store field with no registered panel is never
   read by a view-model constructor at all (UP-001).
3. **One view model, two readers.** ``negotiate/respond.py`` is the single
   branch point between the HTML and JSON renderings of one constructed view
   model, so the user's browser and the directing Claude Code session can never
   drift apart (Principle VI, FR-007).

This package never imports ``civsim_harness``: it is a separate deployable
process, and collapsing the two would blur exactly the write/read boundary
Principle VI depends on being structural (plan.md Structure Decision).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
