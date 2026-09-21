### Goal run: build_a_builder

- provider: `openrouter` (policy `uniform`) | started 2026-09-21T23:13:04.276730+00:00 | finished 2026-09-21T23:13:13.160852+00:00

**build_a_builder -- Build a Builder in your capital: not reached**

- run: `run-1091122b10de4654a42d37e1e41de83c` | final state: paused
- turns recorded: [] (cap 12) | decision steps: 0
- success predicate (harness-side): `player.units.builder_count >= observed_start_builder_count + 1`
- model calls: 0 | cost: $0 | images shown: 1

- frames: 7 (harness-landed-run.gif, 1508 KB); keyframes: keyframe_0_t000s.png, keyframe_1_t002s.png, keyframe_2_t004s.png, keyframe_3_t006s.png

_Principle I: the agent was given the objective text only. The success predicate and the prerequisites above were evaluated by the harness against the observations already in the store; neither was shown to the agent._
