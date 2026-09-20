"""Structured state assembly, capture pipeline, screen-identity probe.

- :mod:`civsim_harness.observe.assemble` -- per-decision-step observation assembly and its
  assembly-failure handling (T095, T096, FR-012, FR-018, FR-046).
- :mod:`civsim_harness.observe.screen_identity` -- interprets the ``game.screen_state`` capability
  result as a recordable, never-raised screen-identity value (T097, research R13).
- :mod:`civsim_harness.observe.capture_paths` -- platform-neutral capture-path selection over the
  ``HostPlatform`` port (T098, research R6).
- :mod:`civsim_harness.observe.capture` -- the per-decision-step capture pipeline, withheld
  unconditionally until the US2 screening gates (T129) exist (T102, FR-015, FR-030, FR-050).
- :mod:`civsim_harness.observe.host_gate` -- the T101 host-capability preflight tier gate, as a
  self-contained function for ``run/preparation.py`` to call (FR-054, V13).
"""
