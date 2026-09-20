"""Catalog loading, versioning/hashing, registry, firetuner vs bespoke binding.

See contracts/capability-catalog.md for the normative schema and load-time
validations.

- :mod:`civsim_harness.capability.predicates` -- the fixed predicate symbol
  table shared by the load-time gate and T106's restricted evaluator.
- :mod:`civsim_harness.capability.loader` -- the YAML loader enforcing all
  seven load-time validations (T035).
- :mod:`civsim_harness.capability.version` -- the catalog content hash
  (T036).
- :mod:`civsim_harness.capability.registry` -- lookup plus wrong-context
  execution refusal (T036, FR-022, research R3).
"""
