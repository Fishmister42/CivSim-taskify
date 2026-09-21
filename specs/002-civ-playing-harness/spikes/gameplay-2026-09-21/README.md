# Gameplay ledger — 2026-09-21 (Linux node, live client)

Owner's directive (11:20 EDT): show the harness *visibly playing* with breadth of demonstrated
actions, observations, screens and prompts, documented honestly and posted to issue #3 every
30–45 minutes. Play quality is irrelevant; coverage and honesty are the point. Every sentence
below is something observed on this host; "not observed" is written where that is the truth.

Client: native Aspyr build 1.0.12.9 (564030), 22 mods incl. BBG/BBM/MPH, X11/Cinnamon, tier
SUPPORTED (text-only to the agent: no image has reached the model on Linux yet — see T260).
Model blocks: Claude Sonnet 5 via the production OpenRouter provider (fallback Opus 5).
Store: repo-root `civsim-match-store.db` (schema 1.1 after the first write-mode open today).

Resume a block (client must be InGame; the driver writes the run configuration from the live V2
read-back; blocks alternate model / stochastic, a fresh `--provider-seed` per stochastic block):

    uv run python -m tests.live.demo_landed_run specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/block-NN --provider openrouter --turns 5
    uv run python -m tests.live.demo_landed_run specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/block-NN --provider stochastic --provider-seed <N> --turns 3

After each block: `uv run python <this dir>/summarize_run.py <run_id>` (read-only, from the store),
`uv run civsim store coverage --since <run_id> --format md` (delta) and `--format md` (whole store),
convert `block-NN/keyframe_*.png` to JPEG (never commit the 60 MB GIF), add a row below, post to
issue #3. A stalled run leaves `/tmp/civsim_harness/run_locks/<run_id>.lock.json` behind: kill the
driver's *python* child (not just the shell wrapper) and remove the lock before the next block.

| block | run_id | game turns | provider | actions (applied / refused) | stalls | frames | cost |
|---|---|---|---|---|---|---|---|
| 01 | run-809988bb | 17 → 20 (4 harness turns, all ended by the no-progress backstop) | Sonnet 5 | 0 / 32 (set_tech ×8, move_to ×16, tech_civic_completed ×8 — every one `parameters: {}`) | paused at turn 4: popup blocked the backstop end turn | 1 (popup, via snap.py; the driver was killed before it wrote its GIF) | $0.684 |
| 02 | run-d2184c44 | 20 → 24 (5 turns, 4 ended by the agent) | Sonnet 5 | 9 / 12: tech_civic_completed ×2 applied, move_to 3 applied / 4 refused, end_turn ×4 applied, set_tech ×8 refused | turn 2 no-progress on set_tech (the setter never took) | 4 keyframes | $0.454 |
| 03 | run-de13afc6 | 25 → 29 (5 turns, 4 ended by the agent) | Sonnet 5 | 5 / 13: tech_civic_completed ×1, end_turn ×4; set_tech ×8 and move_to ×5 refused (same two causes, fixed in 47dcfba after this block) | turn 1 no-progress on set_tech | 4 keyframes | $0.458 |
| 04 | run-fd5fa128 | 30 → 30 (3 harness turns; the game never advanced) | stochastic, seed 7 | 2 / 21: `cities.select` ×2 applied (first city-level action ever); 12 distinct actions attempted at $0 | the whole block sat under a civic popup the sampler never acknowledged; every `turn.end_turn` refused, yet each turn record reads `ended_by_agent` | 4 keyframes | $0 |
| 05 | run-41ef0b94 | 30 → 34 (5 turns, all ended by the agent) | Sonnet 5 | 13 / 1: tech_civic_completed ×2 (the replayed Code of Laws card, then Craftsmanship), `research.set_tech` Writing **applied and verified** (PlayerOperations.RESEARCH), `cities.select` ×4 (probe then reports `city_screen`), move_to 1 applied / 1 refused, end_turn ×5 | none | 4 keyframes | $0.330 |
| 06 | run-26bf638a | 35 → 35 (3 harness turns under an unacknowledged popup; ran before dc67529) | stochastic, seed 11 | 2 / 19: `saves.save_game` applied (first named save by an agent), `units.select` applied; 11 distinct prompt/camera/diplomacy actions refused correctly | popup never drawn by the sampler; end_turn refused ×3 | 4 keyframes | $0 |
| 07 | run-480aa573 | 35 → 35 (5 harness turns under an unmapped first-meeting leader scene) | Sonnet 5 | 5 / 15: `research.set_tech` Currency applied+verified, `cities.select` ×2, `units.select` ×1; `diplomacy.send_delegation` ×9 and `units.move_to` ×1 refused; 4 end turns dispatched, unconfirmed | Australia's greeting (`prompt.diplomatic_approach`, unmapped → probe says `diplomacy`); backstop paused; driver hung, killed (SIGTERM: no recorder output, one snap frame) | 1 (snap) | $0.464 |
| 08 | run-4c0b8fb4 | 35 (died at step 1) | Sonnet 5 | 0 / 0 | the model's `prompt_type` under a proactive trigger crashed the run (validator raised out of the loop) — fixed 2139479 | — | $0.02 |
| 09 | run-221d541d | 35 → 35 (1 harness turn, under the same greeting) | stochastic, seed 13 | 1 / 6: `saves.save_game` applied; 5 refused correctly | **T260 live: 7 of 7 steps had an image delivered** (`screened_clean`, `shown_to_agent`, blob, `image_count=1`; tier `validated`, `xcomposite`) — no model saw it (stochastic) | 4 keyframes | $0 |
| 10 | run-22799875 | 36 → 40 (5 turns, all ended by the agent; screen clear after the operator intervention) | Sonnet 5 | 13 / 1: `cities.select` ×6, `units.select` ×1, `tech_civic_completed` ×1, end_turn ×5; `units.move_to` ×1 refused | none — but **0 of 14 images delivered**: camera provenance passed for the first time (`target_plot` {46,36}, `target_revealed: true`), then the content gate withheld every frame `non_player_ui` with no detail recorded; the probe reported `world` (the new naming) and `city_screen` | 4 keyframes | $0.329 |

## Blocks 2 and 3 — the model plays; two orders never take

**Block 2 (run-d2184c44, 11:35–11:41, game 20 → 24)** opened on the Code of Laws popup:
`prompts.tech_civic_completed {"target": "continue"}` → **applied**, the next step's screen was
`world_view` — `UIManager:DequeuePopup` from the InGame tuner state works (T253 live, twice: a
second popup at turn 21 was acknowledged the same way). `units.move_to` to a plot from
`reachable_plots` was applied 3 times and refused 4 times with `verification_failed`; probed
live at turn 25, a MOVE_TO order lands at +1 s while the verifier read at +0 s — timing, not the
order. `research.set_tech {"target": "TECH_MINING"}` (and Writing) was dispatched 8 times in one
turn and never took: the body called `Player:GetTechs():SetResearchingTech`, an unconfirmed
name; the Research chooser's click is `UI.RequestPlayerOperation(PlayerOperations.RESEARCH, …)`.
The model ended 4 of 5 turns itself. Yields grew 3.0 → 3.5 science.
**Block 3 (run-de13afc6, 11:44–11:50, game 25 → 29)**: one more popup acknowledged; the same
two orders refused 13 times between them; faith 4.0 per turn appeared at turn 26 (a pantheon
or holy-site source; not investigated). Both causes fixed in `47dcfba`, together with
`units.select` / `cities.select` — nothing in the catalog could select a city, so every city
order was structurally unavailable (the model said so in its own reasoning at block 2 turn 1).

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

## Block 4 — stochastic seed 7 — 11:51–11:53 — coverage at $0, and two integrity findings

21 decisions in 89 s, 12 distinct actions, none chosen by a model. The client had raised a
"Civic Completed" popup at game turn 30 (Craftsmanship completed at the end of block 3) and the
sampler never drew `prompts.tech_civic_completed`, so the popup stayed up for all three turns:
`turn.end_turn` refused ×3 and game turn 30 → 30. Findings: (1) the turn-cycle outcome reads
`ended_by_agent` for a turn whose end-turn decision was refused and whose game turn did not
advance — the record should say the turn did not end; (2) the popup's card shows **Code of
Laws** again (frame 4) while the world tracker says Craftsmanship "just completed": T253's
`UIManager:DequeuePopup` closes the popup without running its own Continue handler, so the
popup's internal queue still held the turn-20 civic and replays it. Demonstrated: `cities.select
{"target": 65536}` → applied twice (the `UI.SelectCity` click, verified through `cities.selection`),
even under the popup. Refused correctly: eight prompt responses for prompts that were not on
screen (`unavailable_to_human_now`), `diplomacy.make_peace` (no war). `camera.zoom` ×3 and
`camera.set_view_mode` ×2 refused `verification_failed` — the camera lane's agent owns that.
Block 3's frame 3 shows a third popup kind, "Your civilization has produced a Great Work"
(relic from a tribal village, turn 27), which the probe reported as `world_view`: unmapped, and
the likely reason every `units.move_to` in block 3 turns 3–5 failed verification.

## Operator intervention — 12:34–12:37 EDT — NOT a demonstrated capability

Owner's ruling: clear the first-meeting greeting as operator scripting so play can continue while
`prompt.diplomatic_approach` is unmapped. `operator_answer_greeting.py 1 run-221d541d…` issued the
decline button's own call, `DiplomacyManager.AddResponse(session 1, player 0, "NEGATIVE")` — the
call returned ok, the leader answered, and the session stayed open with the scene up (a human's
second click, Exit, was still owed). `operator_answer_greeting.py 1 close` then issued that click,
`DiplomacyManager.CloseSession(1)`: at +1 s `DiplomacyActionView` hidden, `LeaderScene` hidden,
session closed, `UI.CanEndTurn()` true, game turn 35. One `operator_intervention` run event is
written against run-221d541d (the last run before the intervention) with the before/after
read-backs; the exit click is recorded here. The next first-meeting greeting is the live test of
the screens lane's mapping; if it arrives first, clear it the same way and say so.
