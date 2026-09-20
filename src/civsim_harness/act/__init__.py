"""Action dispatch, execution verification, game-prompt/interrupt handling.

- :mod:`civsim_harness.act.predicates` -- the restricted predicate evaluator, bound to
  :mod:`civsim_harness.capability.predicates`'s fixed symbol table, plus the bindings builder that
  realises that table from a live ``Observation`` (T106, contracts/capability-catalog.md rule 6).
- :mod:`civsim_harness.act.dispatch` -- resolves and authorizes one requested action, rejecting an
  unregistered or unavailable one without ever performing it (T107, FR-017, FR-014).
- :mod:`civsim_harness.act.verify` -- derives ``applied``/``rejected`` and the step's ``progress``
  from re-read game state, never asserted by the executor (T108, FR-011, FR-014, research R14).
- :mod:`civsim_harness.act.prompts` -- routes game-initiated prompts and between-turn interrupts to
  their catalog action, or stalls visibly on an unrecognised screen (T109, FR-010, FR-049, SC-005).
"""
