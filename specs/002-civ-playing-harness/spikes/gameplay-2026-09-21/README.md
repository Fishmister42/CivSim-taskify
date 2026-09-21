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
| 01 | run-809988bb | 17 → 20 (4 harness turns, all ended by the no-progress backstop) | Sonnet 5 | 0 / 32 (set_tech ×8, move_to ×16, tech_civic_completed ×8 — every one `parameters: {}`) | paused at turn 4: popup blocked the backstop end turn | 1 (popup, via snap.py; the driver was killed before it wrote its GIF) | $0.684 |

## Block 1 — run-809988bb — 11:21–11:29 EDT — the schema was the stall

Loaded the attempt-5 quicksave (`…8bc17e7e…__t0003`, game turn 17) through the production
`LuaSaveLoader` in 31.5 s with one intro press; no popup persisted in the save (expected), no
research set. Every one of the 32 decisions Sonnet 5 returned carried `parameters: {}` and was
refused `unavailable_to_human_now` by its availability predicate, while the reasoning named the
target ("Writing is a strong early pick"). Reproduced twice out of band from the stored step
(`repro_decision_call.py`): strict structured output with a bare `{"type": "object"}` for
`parameters` yields `{}`; with `target` declared the same request yields `{"target": "TECH_MINING"}`;
without a schema the model answers in prose. Fixed in `decisions.py` (commit 86a92db).
What the block still demonstrated: `player.yields` observed every step (science 3.0, culture 1.6,
gold 5.0 at turn 17 — T258 live); `cities.selection` and 16 observation bodies assembled each
step; the no-progress backstop ended turns 1–3 (game 17 → 20); at turn 4 the screen probe reported
`prompt.tech_civic_completed` with `has_blocking_prompt` (T253's mapping, live) and the model
chose `prompts.tech_civic_completed` 8/8 times — refused only for the empty target; the backstop's
end turn was then blocked by the popup and the run paused honestly (`BackstopEndTurnNotConfirmed`).
Not observed: any applied action; any image reaching the model (36 captures withheld,
`provenance_failure`: no revealed target plot — T260). Research shows Animal Husbandry with one
turn left at turn 20; no harness action set it (all refused); who set it was not determined.
