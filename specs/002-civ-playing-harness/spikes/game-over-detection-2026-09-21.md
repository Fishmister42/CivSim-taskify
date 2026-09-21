# Game-over detection at the turn boundary — 2026-09-21

**Observed.** At 18:50 EDT on the live Linux client (Aspyr 1.0.12.9), Persia was defeated at game
turn 59 — Georgia took the only city. The harness had no game-over detection of any kind, so the
next turn began exactly as any other: `run/turn_cycle.py` asked for the FR-007 start-of-turn
quicksave, the already-finished game never wrote one, and the run paused with
`SaveVerificationError("expected .Civ6Save not found ... waited 10 s")`. A fresh `InGame` read
taken afterwards showed `Players[0]:IsAlive() == false` and `EndGameMenu` as the only shown
screen. The run's honest terminal state was a **defeat**, recorded as a stop resolution — not a
save error, and not a `paused` run an operator has to go and interpret. The existing
`game.outcome_state` declaration (T216, `lua/gamecore/game_outcome.lua`) could never have caught
it: it is read inside a decision step's observation sweep and consumed by `evaluate_stop_facts`
*after* a turn finishes, and the turn that discovers this ending is precisely the turn that cannot
start.

**Built.** A new `lua/ingame/game_over.lua` (no catalog declaration — this is harness bookkeeping
at a turn boundary, never part of the agent's observation surface) returns
`{ game_over, local_player_alive, outcome: victory|defeat, victory_type, winner, basis,
end_game_screen_shown }` with every accessor separately `pcall`'d, mirroring Firaxis's own
`base/assets/ui/endgame/endgamemenu.lua`: `:1057` `player:IsAlive()` as the shipped defeat gate,
`:1058` `local victor, victoryType = Game.GetWinningTeam()` (two return values), `:875`
`winningTeamID == localPlayerTeamID` as the victory test, `:926` `GameInfo.Victories[victoryType].Name`
for the victory's human name, and `:948-951` `Teams[winningTeamID][1]` →
`PlayerConfigurations[…]:GetCivilizationDescription()` for the winner's. `Game.GetWinningPlayer()`
does not exist anywhere in the shipped UI and is not used; `worldinput.lua:3444` (`return
Game.GetWinningTeam() ~= nil`) is the authority for treating a nil winning team as "nobody has
won". Principle I is kept by a structural gate: `victory_type` and `winner` are computed only
*after* the ending has been established, because only then does the end-game screen state them to
the human — `lua/gamecore/game_outcome.lua`'s parity note (which forbids naming the winner while
the game is still being played) is unchanged and still binding for that file.
`src/civsim_harness/run/game_over.py` interprets the read, `run/turn_cycle.py` calls it in the
pre-save position (ahead of the blocking-prompt probe, because a finished game has no prompt worth
spending a model call on), writes a new `game_over_detected` `RunEvent`, and raises
`GameOverDetected` without attempting the save; `run/runner.py`'s `_finish_on_game_over` is the one
place an exception out of a turn becomes `finished` rather than `paused`, recording
`stop_resolution` `defeat`/`victory` (both already existed in the enum) with any coinciding
condition kept as a timeline event (invariant I10). No gap results: a turn that took no quicksave
was never an attempted turn, so `store/completeness.py` owes it no record. A game-over read that
errors — transport failure, a missing accessor, a malformed body, an outcome string nothing can
name — is **not** a game over: the turn proceeds exactly as it did before the check existed.
`RunEvent.schema.json` was regenerated; the only change is the additive `game_over_detected` enum
member. Tested unit-and-integration only (13 Lua-executing tests under `lupa`, 16 integration
tests through the real `Runner` and a real `SqliteMatchStore`); **unverified live**.

**Left for other lanes.** `EndGameMenu` → a `game_over` screen id belongs to the screens lane
(`lua/ingame/screens.lua`, `catalogs/observations/game.yaml`), and was deliberately not added
here: that file's `CIVSIM_KNOWN_SCREENS` has no entry for it today, so a live game over also
surfaces as an unrecognised screen. The literal ids that lane needs are all confirmed:
`<LuaContext ID="EndGameMenu" FileName="EndGameMenu" Hidden="1"/>` at `base/assets/ui/ingame.xml:120`,
reachable as `ContextPtr:LookUpControl("/InGame/EndGameMenu")` (exactly as shipped
`base/assets/ui/menus/hotseatbackground.lua:12` does), queued at `endgamemenu.lua:755` with
`PopupPriority.EndGameMenu` (`loaders/popuppriorityloader_base.lua:14`, above everything else).
This file already reads that context's `IsHidden()` as `end_game_screen_shown`, but only as
corroboration in the record — never as a gate, since a defeat whose popup failed to queue (the
tutorial ruleset never subscribes at all, `endgamemenu.lua:1327-1330`) is still a defeat.

**Follow-up for the save-loader owner (observed live, not fixed here).** After today's defeat at
game turn 59 the production loader exited the finished game to the MainMenu, and from there
`Network.LoadGame` returned **false** for a known-good quicksave (issued `true`, accepted `false`,
empty error) — while the same loader loads fine from a freshly launched client's main menu. So a
defeat-to-reload recovery under Principle VII currently needs a client relaunch. That is
`saves/load_game.py`'s (`LuaSaveLoader`) call, not this lane's; recorded here so the next branch
or `resume-from` taken off a finished game is not debugged from scratch.
