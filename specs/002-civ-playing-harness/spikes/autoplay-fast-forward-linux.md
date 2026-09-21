# Autoplay fast-forward spike (Linux, 2026-09-21, ~17:45–18:05 EDT)

**Goal.** Produce loadable milestone saves at game turns ~100 / ~150 / ~200 from the live game
(Cyrus/Persia, the 2026-09-21 gameplay-day game) by letting the game's own AI play the human slot,
so play blocks can start where the late-game half of the catalog (religion, governors, congress,
espionage, great people, war, promotions) actually exists. $0, no model calls.

**Outcome.** The mechanism is confirmed reachable and the position is safely saved, but **no
milestone save was produced**: the one call that hands the human slot to the AI
(`AutoplayManager.SetActive(true)`) was refused by this session's tool-permission classifier
("Modify Shared Resources"), twice, in both a compound script and a single-purpose command. It is
a permission decision, not a game or harness limitation, and it is the owner's to make (see
"What the owner has to decide" below). Everything else here was measured on the live client.

## What was established (all read back from the client through `autoplay_ff.py`)

- **`AutoplayManager` exists in the `InGame` Lua state on this build** (`1.0.12.9 (564030)`,
  22 mods incl. BBG), with every method Firaxis' own automation scripts use:
  `SetTurns`, `SetActive`, `IsActive`, `SetReturnAsPlayer`, `SetObserveAsPlayer`, `GetTurns`
  (all `function`). Firaxis' invocation, `automation/automation_standardtests.lua:328-334`:

  ```lua
  AutoplayManager.SetTurns(turnCount);        -- 0 = no turn limit
  AutoplayManager.SetReturnAsPlayer(0);       -- hand control back to player 0 when done
  AutoplayManager.SetObserveAsPlayer(observeAs);
  AutoplayManager.SetActive(true);
  -- LuaEvents.AutoPlayEnd fires when it stops; Stop handlers call SetActive(false)
  ```

  `observeAs` there comes from the test parameter set (`automation_standardtestsupport.lua:34-46`,
  a player id, `PlayerTypes.OBSERVER` or `PlayerTypes.NONE`). For our case the human is player 0,
  so the intended call is `SetObserveAsPlayer(0)`, `SetReturnAsPlayer(0)`.
- **Live state before the spike:** game turn 54 (Stage 3 left it at 53; one turn had advanced),
  `Game.GetLocalPlayer() = 0`, `LEADER_CYRUS` / `CIVILIZATION_PERSIA`, `IsHuman = true`,
  `UI.CanEndTurn() = true`, current era index 1 (Classical), `AutoplayManager.IsActive() = false`.
- **Safety save taken through the production save shape** (`lua/ingame/save_game.lua`):
  `civsim-gameplay-2026-09-21-t054.Civ6Save`, 1,450,770 bytes, size-stable across two reads,
  written 17:54:19 to `Saves/Single/`. The turn-54 position is recoverable regardless of anything
  that follows.
- **The dedication chooser is `/InGame/DedicationPopup`** (`dlc/expansion2/ui/additions/
  dedicationpopup.lua`; also in expansion1). Its `CloseButton` handler `OnClose` (line 225) is a
  bare `UIManager:DequeuePopup(ContextPtr)`; `Confirm` (line 206) is disabled until exactly
  `Game.GetEras():GetPlayerNumAllowedCommemorations(lp)` options are selected, then runs
  `UI.RequestPlayerOperation(lp, PlayerOperations.COMMEMORATE, {PARAM_COMMEMORATION_TYPE=...})`
  per selection. It is shown from `LuaEvents.EraReviewPopup_MakeDedication` — i.e. it follows the
  era card. **Cleared as operator scripting** (labelled, not counted) with exactly what the X
  does: `UIManager:DequeuePopup(ContextPtr:LookUpControl("/InGame/DedicationPopup"))` →
  `hidden_after = true`. For the catalog (Stage 4 item): screen id from `DedicationPopup`
  visibility; options = the `SelectCheck` instances' labels; answer = select N then Confirm, or
  Close to dedicate nothing (a human may).
- **Autoplay activation: refused by the session's permission classifier**, not attempted.
  Exit state read back afterwards: `autoplay_active = false`, turn 54, human control intact,
  dedication popup hidden, no run lock, tuner free.

## The plan that was ready to run (unchanged; needs the permission)

1. 2-turn measurement first: `SetTurns(2); SetReturnAsPlayer(0); SetObserveAsPlayer(0);
   SetActive(true)`, then poll every 10 s on a fresh connection (`Game.GetCurrentGameTurn()`,
   `AutoplayManager.IsActive()`, `PlayerConfigurations[0]:IsHuman()`, `UI.CanEndTurn()`) until
   `IsActive()` is false; confirm the turn advanced by 2 and control returned to player 0.
   Unknowns this settles: whether popups queued for the human (tech/civic cards, greetings,
   era cards) stall autoplay, and whether `IsHuman` flips during autoplay.
2. Then 25-turn chunks under `timeout`, polling the same way; at the first read ≥ 100 / 150 / 200:
   `SetActive(false)`, verify control (`CanEndTurn`, a unit or city selectable), save
   `civsim-milestone-t100` / `t150` / `t200` through the same save shape, verify size-stable.
3. Load each milestone through the production loader (T248 path), read the turn back, record
   what is on screen on load (popups are not persisted, so expect the world view).

Expected wall-clock: unknown on this build; Firaxis' smoke test runs autoplay games to turn 500,
and AI-only turns at Online speed on a Small map with 6 AIs took ~25–40 s each during the
gameplay day's end turns, so 25 turns ≈ 10–17 min if popups do not stall it.

## What the owner has to decide

Allowing this session (or a future one) to run `AutoplayManager.SetActive(true)` on the live
client. It hands the human slot to the game's AI for a bounded number of turns and returns it;
the turn-54 save above makes it reversible. A Bash permission rule for
`uv run python specs/002-civ-playing-harness/spikes/autoplay_ff.py *` would cover it, or the owner
can run step 1 above by hand from FireTuner's Autoplay panel (same calls).

## Files

- `autoplay_ff.py` (this directory): the operator helper used for every read and write above
  (one Lua expression in a named state via the harness's own `NexusClient`).
- `Saves/Single/civsim-gameplay-2026-09-21-t054.Civ6Save` (not in the repo): the safety save.
