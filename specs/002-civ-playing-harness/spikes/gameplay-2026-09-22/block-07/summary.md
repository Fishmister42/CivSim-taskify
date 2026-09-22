### Goal run: move_unit_to_plot

- provider: `stochastic` (policy `coverage`) | started 2026-09-22T14:30:51.334545+00:00 | finished 2026-09-22T14:34:37.864254+00:00

**move_unit_to_plot -- Move a unit to a plot it can reach: not reached**

- run: `run-5cf209a76e73420d80efae6295cbc83d` | final state: finished
- turns recorded: [1] (cap 2) | decision steps: 7
- success predicate (harness-side): `observed_applied_units_move_to >= 1`
- actions by id:
  - `camera.move`: out_of_parity_camera=1
  - `diplomacy.declare_war`: verification_failed=1
  - `diplomacy.send_delegation`: unavailable_to_human_now=1
  - `policies.slot_policy`: verification_failed=1
  - `saves.save_game`: applied=1
  - `turn.end_turn`: verification_failed=1
  - `units.select`: applied=1
- model calls: 7 | cost: $0 | images shown: 0

_Principle I: the agent was given the objective text only. The success predicate and the prerequisites above were evaluated by the harness against the observations already in the store; neither was shown to the agent._
