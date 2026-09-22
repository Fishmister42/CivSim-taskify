# The screen-identity probe answered `world` with a full-screen modal up (MEASURED 2026-09-22, 14:53Z)

Live Linux client 1.0.12.9, game turn 67, local player (Persia) eliminated by Georgia.
Evidence in this directory: `observations-at-defeat.json` (production chain),
`endgamemenu-probe.json` (raw watchlist sweep), `endgamemenu-defeat-screen.jpg` (the frame),
`endgamemenu-probe.timestamp`.

## What the production probe said, and what was true

Dispatched through the production `load_catalog` -> `CapabilityRegistry` -> `CapabilityExecutor`
chain (`tests/live/probe_observation_bodies.py`), both in the same sweep:

    game.screen_state    {"screen": "world", "raw_screen_id": "InGame", "recognized": true,
                          "has_blocking_prompt": false, "prompt_options": []}
    game.outcome_state   {"is_game_over": true, "outcome": "defeat"}

What was actually on screen: Firaxis's `EndGameMenu`, full-screen, ribbon label `DEFEAT`,
tabs Results/Ranking/Graphs, bottom bar Replay Movie / Main Menu / Just One More Turn.
`/InGame/EndGameMenu`:`IsHidden()` == `false`, read from InGame.

## This is the dangerous shape, not the safe one

`recognized = false` would have been safe: the harness would have known it did not know, and
FR-049 would have stalled the board. `recognized = true` while wrong is the shape every
downstream consumer treats as settled. A harness in this state dispatches actions into a modal,
watches every one fail, and records them as **action failures rather than as a blocked board** --
so this is a candidate contributor to today's `verification_failed` column, alongside the
phantom-predicate-field cause and the 4 s confirm bound.

## The structural defect: the watchlist is an allowlist

`CIVSIM_SCREEN_WATCHLIST` (lua/ingame/screens.lua) is the entire universe the probe can see.
`CivSim_Screens_State()` loops it, and when nothing in it reports `hidden == false` it returns
`screen = "world", raw_screen_id = "InGame", recognized = true` -- the `#open == 0` branch.

So a screen that is not on the list reads as **no screen at all**. The probe cannot distinguish
"nothing is blocking" from "nothing I know about is blocking", and it reports the first when it
means the second. Adding `EndGameMenu` fixes this board. It does not fix that: the next unwatched
modal produces the identical false negative. This is the third unmapped blocking screen today
(`WorldCongressIntro`, the full-screen `DiplomacyActionView`, now `EndGameMenu`) and the first
where the probe said it was *not* blocked.

## The hidden-check itself is NOT broken

Every watchlist entry reading `hidden` at once, with a modal demonstrably up, looked like the
check might be failing wholesale from that context. It is not. The same sweep, in the same
command, over the same `ContextPtr:LookUpControl("/InGame/" .. name)` + `:IsHidden()` mechanism,
returned `EndGameMenu = SHOWN` and every other entry `hidden`. One entry flipping by the identical
code path proves the mechanism works; the watchlist entries are genuinely all hidden, because the
end-game screen is up instead of them. The defect is coverage, not the read.

## A cheap cross-check already exists and is not wired

`game.outcome_state` reported `is_game_over: true` in the same sweep in which `game.screen_state`
reported `world`. That contradiction is machine-detectable with no new client reach at all:
`is_game_over == true` while `screen == "world"` is impossible, and either observation alone
would have stopped the board. Nothing currently compares them.

## Follow-ups this opens (for the live lane to file)

1. `EndGameMenu` on the watchlist, mapped to a non-prompt `game_over` screen id -- landed in this
   block. It is a terminal-state screen, not a blocking prompt: `has_blocking_prompt` stays false
   and `prompt_options` stays empty, so the game-over path claims it, not the prompt watchlist.
2. The probe must be able to answer `unknown` for a modal it does not know, instead of `world`.
   The allowlist cannot do this; it needs a positive signal that *something* is up (a shown
   context that is not the world view) independent of whether that context is named.
3. Cross-check `game.outcome_state.is_game_over` against `game.screen_state.screen` at assembly
   time and refuse the pair when they contradict.
4. `UI.CanEndTurn()` returned **true** at turn 67 with the local player dead and the game over.
   Any liveness check resting on it is unsound at terminal state.
