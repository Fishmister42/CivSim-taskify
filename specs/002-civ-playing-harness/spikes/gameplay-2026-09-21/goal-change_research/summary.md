### Goal run: change_research

- provider: `openrouter` (policy `uniform`) | started 2026-09-21T23:20:59.090826+00:00 | finished 2026-09-21T23:24:24.030234+00:00

**change_research -- Change the current research to a named technology: not reached**

- run: `run-d0933ca884b84be9a6d2d859bc2f94d9` | final state: paused
- turns recorded: [1] (cap 3) | decision steps: 16
- success predicate (harness-side): `observed_applied_research_set_tech >= 1 and player.current_research != observed_start_current_research`
- actions by id:
  - `prompts.ai_diplomatic_approach`: verification_failed=16
- model calls: 16 | cost: $0.5299 | images shown: 16

- frames: 178 (harness-landed-run.gif, 112 KB); keyframes: keyframe_0_t000s.png, keyframe_1_t064s.png, keyframe_2_t128s.png, keyframe_3_t192s.png

_Principle I: the agent was given the objective text only. The success predicate and the prerequisites above were evaluated by the harness against the observations already in the store; neither was shown to the agent._
