### Goal run: send_delegation

- provider: `openrouter` (policy `uniform`) | started 2026-09-21T23:24:24.539546+00:00 | finished 2026-09-21T23:28:13.669767+00:00

**send_delegation -- Send a delegation to a civilization you have met: not reached**

- run: `run-7907cc6c36604c20842b47f151bc8563` | final state: paused
- turns recorded: [1] (cap 3) | decision steps: 16
- success predicate (harness-side): `observed_applied_diplomacy_send_delegation >= 1 and player.delegation_count >= observed_start_delegation_count + 1`
- actions by id:
  - `prompts.ai_diplomatic_approach`: verification_failed=16
- model calls: 16 | cost: $0.5345 | images shown: 16

- frames: 199 (harness-landed-run.gif, 112 KB); keyframes: keyframe_0_t000s.png, keyframe_1_t071s.png, keyframe_2_t143s.png, keyframe_3_t214s.png

_Principle I: the agent was given the objective text only. The success predicate and the prerequisites above were evaluated by the harness against the observations already in the store; neither was shown to the agent._
