# Gameplay ledger — 2026-09-21 (Linux node, live client)

Owner's directive (11:20 EDT): show the harness *visibly playing* with breadth of demonstrated
actions, observations, screens and prompts, documented honestly and posted to issue #3 every
30–45 minutes. Play quality is irrelevant; coverage and honesty are the point. Every sentence
below is something observed on this host; "not observed" is written where that is the truth.

Client: native Aspyr build 1.0.12.9 (564030), 22 mods incl. BBG/BBM/MPH, X11/Cinnamon, tier
SUPPORTED (text-only to the agent: no image has reached the model on Linux yet — see T260).
Model blocks: Claude Sonnet 5 via the production OpenRouter provider (fallback Opus 5).
Store: repo-root `civsim-match-store.db` (schema 1.1 after the first write-mode open today).

Resume a block: `uv run python -m tests.live.demo_landed_run specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/block-NN --provider openrouter --turns 5`
(client must be InGame; the driver writes the run configuration from the live V2 read-back).

| block | run_id | game turns | provider | actions (applied / refused) | stalls | frames | cost |
|---|---|---|---|---|---|---|---|
