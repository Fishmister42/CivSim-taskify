# `LuaEvents` auto-creates on access — every existence check against it is worthless

**Date:** 2026-09-20 · **Host:** Linux live node · State: `InGame` (132).

```
  LuaEvents.ZZZ_TotallyFakeEventName_12345   exists=true  type=table
  LuaEvents.AnotherObviousNonsense_XYZ       exists=true  type=table
  LuaEvents.FileListQueryResults             exists=true  type=table
  LuaEvents.AutomationMainMenuStarted        exists=true  type=table
```

**A name I invented on the spot returns a table.** `LuaEvents` has a metatable that creates an event
object for any key read, so:

> **`LuaEvents[name] ~= nil` is `true` for every possible string.** It tells you nothing. It can
> never tell you anything.

And the consequence that actually costs time:

> **`LuaEvents.<misspelled>.Add(handler)` succeeds silently and the handler never fires** — because
> the event it registered on is a fresh object nobody raises.

## This retroactively explains three failed rounds of T217

`load-path-linux.md` recorded the symptom as *"the events were eventually found — in a different Lua
state — but wiring a handler to them still does not produce results,"* and treated "the handler
registers" as evidence the event name was right.

**It was not evidence of anything.** Registration succeeds against any name. A probe that checks
`LuaEvents[name] ~= nil`, sees `true`, registers a handler and then waits is guaranteed to look
exactly like this whether the name is real or invented — which is precisely what those rounds looked
like.

`spikes/t217_savegame_query.lua` section [2] is built on this check and its output should be read as
meaningless, not as a negative result. (T217 was ultimately solved another way — the call is
front-end only — so nothing downstream depends on it.)

## What to do instead

- **`Events` does not auto-vivify.** `Events.FileListQueryResults` → `nil` was a *real* negative in
  Round 1. Prefer `Events` for existence questions where the symbol lives there.
- **Get names from shipped code, not from probing.** The real names were sitting in
  `steamassets/base/assets/ui/automation/*.lua` the whole time: `LuaEvents.AutomationMainMenuStarted`,
  `AutomationGameStarted`, `AutomationPostGameInitialization`, `AutoPlayEnd`. Grepping the game's own
  assets answers in seconds what probing cannot answer at all.
- **Assert on the handler *firing*, never on the event *existing*.** The only real test of an event
  name is that a handler attached to it receives something. That is the project's boundary rule
  again: assert on what arrived, not on what our side returned.

## Recorded alongside: the victory-progress parity check is still inconclusive

The queue item asks whether banning `Game.GetVictoryProgressForTeam` as out-of-parity is
over-restrictive, on the suspicion that Civ VI's Victory Progress screen shows a human the same
rival information.

Measured so far:

```
  type(Game.GetVictoryProgressForTeam)        = function
  Game.GetVictoryProgressForTeam(0)           -> ERR: bad argument #2 (integer expected)
  Game.GetVictoryProgressForTeam(0, <hash>)   -> nil
  Game.GetVictoryProgressForTeam(1, <hash>)   -> nil
```

It takes **(team, victoryTypeHash)**, and the victory types enumerate cleanly from `GameInfo.Victories()`:

```
  VICTORY_SCORE 1428793787 · VICTORY_DEFAULT -1085549345 · VICTORY_CONQUEST 407109516
  VICTORY_CULTURE -356759317 · VICTORY_RELIGIOUS 415516560 · VICTORY_TECHNOLOGY 353119609
  VICTORY_CONCEDE -12499977 · VICTORY_DIPLOMATIC -494301917
```

**`nil` here is uninformative**, because the game under test is at turn 2 — no civilisation has any
victory progress to report. Settling the parity question needs a game far enough along to have
non-null progress, *and* a capture of what the Victory Progress screen shows a human at that same
moment. **The ban stands until then**; nothing here weakens it.
