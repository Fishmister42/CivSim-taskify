### Goal run: save_named_game

- provider: `openrouter` (policy `uniform`) | started 2026-09-21T23:28:14.205191+00:00 | finished 2026-09-21T23:31:45.317558+00:00

**save_named_game -- Take a named save of the game: not reached**

- run: `run-1f36ee88e89e4445b4ddf81d710aeb96` | final state: paused
- turns recorded: [1] (cap 3) | decision steps: 16
- success predicate (harness-side): `observed_applied_saves_save_game >= 1`
- actions by id:
  - `prompts.ai_diplomatic_approach`: verification_failed=16
- model calls: 16 | cost: $0.5293 | images shown: 16

- frames: 182 (harness-landed-run.gif, 112 KB); keyframes: keyframe_0_t000s.png, keyframe_1_t065s.png, keyframe_2_t131s.png, keyframe_3_t197s.png

_Principle I: the agent was given the objective text only. The success predicate and the prerequisites above were evaluated by the harness against the observations already in the store; neither was shown to the agent._
