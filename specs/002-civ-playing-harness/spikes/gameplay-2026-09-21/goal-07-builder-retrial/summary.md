### Goal run: build_a_builder

- provider: `openrouter` (policy `uniform`) | started 2026-09-22T00:46:26.719965+00:00 | finished 2026-09-22T00:46:50.094432+00:00

**build_a_builder -- Build a Builder in your capital: not reached**

- run: `run-024bc6135d9a4493bef4311fb472df74` | final state: paused
- turns recorded: [] (cap 12) | decision steps: 0
- success predicate (harness-side): `player.units.builder_count >= observed_start_builder_count + 1`
- model calls: 0 | cost: $0 | images shown: 0

- frames: 17 (harness-landed-run.gif, 1766 KB); keyframes: keyframe_0_t000s.png, keyframe_1_t005s.png, keyframe_2_t012s.png, keyframe_3_t017s.png

_Principle I: the agent was given the objective text only. The success predicate and the prerequisites above were evaluated by the harness against the observations already in the store; neither was shown to the agent._
