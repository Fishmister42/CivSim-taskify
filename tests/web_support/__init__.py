"""Test support for `001-unified-web-interface` (`src/civsim_web/`).

A feature-owned package, deliberately separate from `tests/fakes/` (which
belongs to `002-civ-playing-harness`), so the two efforts can grow their
fixtures without editing each other's files. Importable as `web_support.*`
from any test under `tests/`, the same way `fakes.*` already is.
"""
