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
| 11 | run-fd9c08a1 | 41 → 42 (2 turns; paused at turn 3) | stochastic, seed 17 (prompt-first sampler, 0fa4407) | 4 / 10: `tech_civic_completed`… no — `saves.save_game`, `cities.select` ×2, `turn.end_turn` ×1 applied; `prompts.great_work_created` and `prompts.tech_civic_completed` refused (not on screen), 6 others refused correctly | **paused `CatalogError`**: a new first-meeting greeting arrived at game turn 42; the screens lane's mode-aware probe (working tree) reported it as `prompt.diplomatic_approach` — their mapping seen live — and the sampler derived the action id `prompts.diplomatic_approach`, which is not in the catalog (`prompts.ai_diplomatic_approach` is); the unregistered id paused the run instead of being refused `not_in_catalog`. Driver hung on the paused run, killed, lock cleared. | 1 (snap on exit) | $0 |

## Stage 1 hand-off — 12:48 EDT

**Client on exit:** Civ VI up, InGame, game turn 42, tuner free (no run, no lock). A first-meeting
greeting (the second civ met) is on screen — see `block-11/frame-turn42-on-exit.jpg` and the
read-back in the hypervisor log. Clearing it is either the screens lane's `prompts.ai_diplomatic_approach`
(now mapped in the working tree; the live test) or, if that has not landed,
`operator_answer_greeting.py <other_player_id> [run_id]` then `… <other_player_id> close`, labelled.

**Resume play:** the two commands at the top of this file, next block number 12 (model, 5 turns).

**Spend today:** $2.95 (blocks 1–3 $1.60, 5 $0.33, 7 $0.46, 8 $0.02, 10 $0.33, three repro
calls $0.07; stochastic blocks $0). Cap self-managed; nothing else to ask.

**Stage 2 first, in order:** (1) the owner's end-turn ruling — an end turn dispatched but not
confirmed within the bound is recorded `end_turn_unconfirmed` with `game_turn_advanced: false`,
harness turn still advances, store completeness excludes such runs from trending; R14 text and
the "stuck end turn" test change with it. (2) Verify the screens landing live: the greeting on
screen now is the test of `prompt.diplomatic_approach` + `prompts.ai_diplomatic_approach`;
`prompts.great_work_created` next time a relic pops. (3) The content gate: after T260 the
provenance gate passes (`target_revealed: true`, block 10) and every world/city frame is then
withheld `non_player_ui` with no detail on the capture record (block 9's leader-scene frames
passed the same gate) — make the gate record what it saw, then find out what. (4) A provider's
unregistered declaration id must be a `not_in_catalog` refusal, not a paused run (block 11).
(5) The driver hangs on a paused run (blocks 1, 7, 11): make `demo_landed_run` treat `paused` as
terminal and write its results. (6) `cities.set_production` has never been chosen — check its
availability under `city_screen`.

## Stage 2 — 12:51 EDT → (Linux live lane, second stage)

| block | run_id | game turns | provider | actions (applied / refused) | stalls | frames | cost |
|---|---|---|---|---|---|---|---|
| 13 | run-7f5446ef | 42 → 42 (1 harness turn under Georgia's greeting) | stochastic, seed 21 | 0 / 8: `prompts.ai_diplomatic_approach` ×4 (route fix c052c39 verified live: the probe reported `prompt.diplomatic_approach` with both real statement texts at every step; the sampler answered first), `diplomacy.declare_war` ×2, `move_to`, `units.select` refused correctly | every answer rejected on verification — the options never changed: `CallCallback` is a no-op (not a Firaxis API) | 4 keyframes | $0 |
| 14 | run-340250e4 | 42 → 42 (1 harness turn, same greeting) | Sonnet 5 | 0 / 8: `prompts.ai_diplomatic_approach` ×8, "visit our nearby city" every time | same no-op click; **image delivered at 8 of 8 steps to a real model** (`screened_clean`, `shown_to_agent`, blob, `image_count=1`; tier `validated`, `xcomposite`) — first frames a model ever received on Linux; whether it *used* them is not provable from its reasoning | 4 keyframes + the frame it saw (jpg) | $0.229 |
| 15 | run-4fdd0ca1 | 42 → 42 (1 harness turn, same greeting) | stochastic, seed 33 | 1 / 15: `saves.save_game` applied; `prompts.ai_diplomatic_approach` ×4 rejected (first host click, unscaled UI coordinates → empty scene); 6 `turn.end_turn` refused and **not** ending the turn (dc67529 confirmed) | the UI-space → window scaling (1024×768 → 1920×1200) was measured after this block; a manual click at the scaled spot answered the greeting (operator intervention, recorded on this run) | 1 (snap of the greeting) | $0 |
| 16 | run-51df5f43 | 42 (paused at turn 1 before any step) | Sonnet 5 | 0 / 0 | `save_failed`: `Network.SaveGame` returned false under the Gathering Storm eruption cinematic (`NaturalDisasterPopup`, unwatched → probe said `world`); the turn-start quicksave precedes any observation, so the prompt could not be routed | 1 (snap of the cinematic) | $0 |
| 17 | run-cf1b9b1e | 42 (paused at turn 1 before any step) | stochastic, seed 41 | 0 / 0 | same `save_failed`; the driver now ends a paused block in seconds and clears its lock; cinematic cleared by operator click at its `Close` button's scaled position (recorded on this run, not counted); game turn then read 43 | — | $0 |
| 18 | run-cf9d99e0 | 43 → 47 (5 turns, every one ended by the no-progress backstop) | Sonnet 5 | 1 / 40: `cities.select` applied; `prompts.era_transition` {"target": "continue"} ×40 refused `unavailable_to_human_now` | **the model was right and the harness was wrong**: Gathering Storm's "The World Has Entered the Classical Era" popup (`EraReviewPopup`, open per the live probe; the watchlist only knows the base game's `EraCompletePopup`) was up the whole block; the text observation said `world`, no prompt; Sonnet's reasoning at every step described the popup and asked to acknowledge it -- knowledge that could only have come from the picture (**41 of 41 images delivered**). Stage 3: map `EraReviewPopup` → `prompt.era_transition` with its `Continue`/close as the click | 4 keyframes + snap of the popup | $1.185 |
| 19 | run-08566ab0 | 48 → 48 (1 harness turn, ended by the agent, game-confirmed) | stochastic, seed 47 | 2 / 6: **`prompts.era_transition` applied** — the probe recognised `EraReviewPopup` (mapping landed after block 18), the sampler answered, the executor clicked the card's own `Continue` at its UI-space rectangle scaled 1.875 × 1.5625, verification saw `world`; `turn.end_turn` applied; 6 camera/diplomacy actions refused correctly | none — **the scaled click path is live-verified through the production executor** | 4 keyframes | $0 |

Landed this stage: prompt route table + `not_in_catalog` stall (c052c39); paused-run terminal + lock clear, `image_withheld` reason, views screened by platform (98c71bb); the real click path (Lua hands back the control's rect + the UI's own screen size, the executor scales by window/ui and clicks through XTest) for the diplomacy answer **and** every acknowledge-only popup, with `NaturalDisasterPopup` → `prompt.natural_disaster` / `prompts.natural_disaster` (catalog 2026.09.6). Unverified live until the next greeting / popup / cinematic: the scaled click through the production executor. Open for Stage 3: the turn-start quicksave must not deadlock on a prompt that blocks saving; `debug_overlay` corner heuristic; `cities.set_production` never chosen (not yet investigated).
