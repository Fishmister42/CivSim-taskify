### Goal run: build_a_builder

- provider: `openrouter` (policy `uniform`) | started 2026-09-22T13:34:52.920549+00:00 | finished 2026-09-22T13:40:15.799268+00:00

**build_a_builder -- Build a Builder in your capital: REACHED**

- run: `run-d3d0368b0cf34ff7ad782a41e539d8c0` | final state: finished
- turns recorded: [1, 2, 3, 4] (cap 8) | decision steps: 8
- reached at recorded turn 3
- success predicate (harness-side): `player.units.builder_count >= observed_start_builder_count + 1`
- actions by id:
  - `cities.select`: applied=2
  - `cities.set_production`: applied=1
  - `prompts.tech_civic_completed`: applied=1
  - `turn.end_turn`: applied=4
- model calls: 8 | cost: $0.297 | images shown: 4

- frames: 239 (harness-landed-run.gif, 49356 KB); keyframes: keyframe_0_t000s.png, keyframe_1_t088s.png, keyframe_2_t179s.png, keyframe_3_t267s.png

_Principle I: the agent was given the objective text only. The success predicate and the prerequisites above were evaluated by the harness against the observations already in the store; neither was shown to the agent._
