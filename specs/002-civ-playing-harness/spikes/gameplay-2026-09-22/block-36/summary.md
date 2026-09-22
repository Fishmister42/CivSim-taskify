### Goal run: build_a_builder

- provider: `scripted` (policy `uniform`) | started 2026-09-22T21:13:25.981272+00:00 | finished 2026-09-22T21:18:24.535217+00:00

**build_a_builder -- Build a Builder in your capital: REACHED**

- run: `run-8becab23703d40c1b1525df4be892b36` | final state: paused
- turns recorded: [1, 2, 3] (cap 12) | decision steps: 21
- reached at recorded turn 2
- success predicate (harness-side): `player.units.builder_count >= observed_start_builder_count + 1`
- actions by id:
  - `cities.select`: applied=2
  - `cities.set_production`: applied=1
  - `turn.end_turn`: applied=1, unavailable_to_human_now=16, verification_failed=1
- model calls: 21 | cost: $0 | images shown: 5

- frames: 257 (harness-landed-run.gif, 6448 KB); keyframes: keyframe_0_t000s.png, keyframe_1_t093s.png, keyframe_2_t186s.png, keyframe_3_t278s.png

_Principle I: the agent was given the objective text only. The success predicate and the prerequisites above were evaluated by the harness against the observations already in the store; neither was shown to the agent._
