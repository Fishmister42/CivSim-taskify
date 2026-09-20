"""Civilization-Playing Harness.

An unattended Civilization VI playing harness: brings a game to a fully
specified starting position, then plays turn after turn until a stop
condition, recovering from crashes and provider failures as the same
continuous run, and supporting branch-and-replay from any turn's save.

See specs/002-civ-playing-harness/plan.md for the full design.
"""

__version__ = "0.1.0"
