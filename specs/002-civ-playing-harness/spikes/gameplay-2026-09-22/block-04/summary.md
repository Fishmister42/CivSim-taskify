### Goal run: build_a_builder

- provider: `openrouter` (policy `uniform`) | started 2026-09-22T13:42:39.452267+00:00 | finished 2026-09-22T13:47:53.394032+00:00

**build_a_builder -- Build a Builder in your capital: REACHED**

- run: `run-e90d69673dd4458aa60e8cd8bc1daa5d` | final state: finished
- turns recorded: [1, 2, 3, 4] (cap 8) | decision steps: 9
- reached at recorded turn 3
- success predicate (harness-side): `player.units.builder_count >= observed_start_builder_count + 1`
- actions by id:
  - `cities.select`: applied=3
  - `cities.set_production`: applied=2
  - `turn.end_turn`: applied=3, verification_failed=1
- model calls: 9 | cost: $0.3226 | images shown: 4

- frames: 261 (harness-landed-run.gif, 29311 KB); keyframes: keyframe_0_t000s.png, keyframe_1_t097s.png, keyframe_2_t193s.png, keyframe_3_t286s.png

_Principle I: the agent was given the objective text only. The success predicate and the prerequisites above were evaluated by the harness against the observations already in the store; neither was shown to the agent._
