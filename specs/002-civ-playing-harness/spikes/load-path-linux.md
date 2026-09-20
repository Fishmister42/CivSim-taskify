# Spike — `Network.LoadGame` and the load half of FR-034

**Feature**: `002-civ-playing-harness` · **Executed**: 2026-09-20 · **Host**: Linux (native Aspyr)
**Client**: `1.0.12.9 (564030)`, Gathering Storm, BBG active

The companion to [r5-save-path.md](./r5-save-path.md). That spike proved a save can be *written*; this
one answers whether it can be *read back*, which is what FR-034 branch identity actually rests on.

## Verdict — split, and the split matters

| Question | Answer |
|---|---|
| Does a save written by `Network.SaveGame` load back? | ✅ **Yes** |
| Is the restored position **identical**? | ✅ **Yes — verified field by field** |
| Does **`Network.LoadGame`** work from Lua? | ❌ **No.** Returns `false`, loads nothing |
| Are save files byte-stable across save → load → save? | ❌ **No — and this matters** |

**FR-034 is satisfiable. US4 branching, `resume-from`, replay, archival, and the reaper are not
invalidated** — the position round-trips exactly. But **the automated load path does not exist yet**,
so branching cannot currently be driven end-to-end without a UI-driven step.

## The position round-trips exactly

Method: fingerprint the position, save, advance the game, load, fingerprint again, compare.

**Baseline (turn 1), then advanced to turn 19, then loaded the save:**

```
=== after load ===                          === baseline ===
turn=1                                      turn=1
localplayer=0                               localplayer=0
gold=10.00                                  gold=10.00
science=0.00                                science=0.00
culture=0.00                                culture=0.00
faith=0.00                                  faith=0.00
civ=CIVILIZATION_MALI                       civ=CIVILIZATION_MALI
leader=LEADER_MANSA_MUSA                    leader=LEADER_MANSA_MUSA
unitcount=2                                 unitcount=2
  unit[1] 0@20,32 hp=0 moves=2                unit[1] 0@20,32 hp=0 moves=2
  unit[2] 21@19,32 hp=0 moves=2               unit[2] 21@19,32 hp=0 moves=2
citycount=0                                 citycount=0
```

Byte-identical across every compared field, from a state 18 turns downstream. **The save/load
round-trip preserves the game position.**

## `Network.LoadGame` does not work from Lua

It exists as a `function` in `InGame`, and it is callable — but it returns **`false`** and nothing
loads. **Six different `gameFile` shapes were tried, including the exact table that
`Network.SaveGame` accepts:**

```
A_name_only       -> ok=true ret=false      D_with_filetype  -> ok=true ret=false
B_with_filename   -> ok=true ret=false      E_with_ext       -> ok=true ret=false
C_saveloc_normal  -> ok=true ret=false      F_full_path      -> ok=true ret=false
```

`ok=true` means no Lua error was raised — the call is well-formed and the engine simply refuses it.
This is an important asymmetry worth stating plainly:

> **`Network.SaveGame` accepts a hand-constructed table. `Network.LoadGame` does not.**

### The likely reason, and the untested lead

`UI.QuerySaveGameList` **exists as a function** in `InGame`, `LoadGameMenu`, and `SaveGameMenu`, and
calling it returns `0` (a query handle) rather than erroring. The strong hypothesis is that
`Network.LoadGame` requires a **file record produced by that query** — carrying internal fields that
cannot be reproduced by hand — rather than a name-shaped table.

Completing that path needs the query's async results. **The events were eventually found — in a
different Lua state — but wiring a handler to them still does not produce results.**

### Round 1: looked in `InGame`, found nothing

- `Events.FileListQueryResults` → **`nil`**
- Scanning `Events` for any member matching `file` / `save` / `load` yields exactly one:
  `Events.LoadGameViewStateDone`
- `LuaEvents` yields none

### Round 2: the events live in `LoadGameMenu`, under `LuaEvents`

Acting on the hypothesis that the file-list machinery would be wired in the state that *displays* the
file list rather than in `InGame` — which follows from every UI screen being its own Lua state — a
rescan of state **112 (`LoadGameMenu`)** found them:

```
=== LoadGameMenu (112) LuaEvents: 5 ===
  LuaEvents.FileListQueryComplete
  LuaEvents.FileListQueryResults
  LuaEvents.HostGame_SetLoadGameServerType
  LuaEvents.InGameTopOptionsMenu_SetLoadGameServerType
  LuaEvents.MainMenu_SetLoadGameServerType

=== SaveGameMenu (113) LuaEvents: 1 ===
  LuaEvents.FileListQueryComplete
```

They are on **`LuaEvents`, not `Events`**, and **only in the menu states** — which is exactly why the
`InGame` scan came back empty. Worth generalising: *a symbol's absence in one state says nothing
about the others.*

### Round 3: the handler registers, the query runs, the event never fires

```
handler registered ok=true err=nil
query issued ok=true ret=0
...
fired=0
CIVSIM_SAVES type=nil
```

Tried twice: once with the Load Game screen closed, once with it **open and confirmed active**
(`ContextPtr:IsHidden() == false`). Identical result both times. The handler attaches without error
and `UI.QuerySaveGameList` returns without error, but nothing is ever delivered to it.

`ret=0` is ambiguous and may be a result *count* rather than a query handle — in which case the query
itself is matching nothing and the parameters are wrong, rather than the event being mis-wired.

**A further avenue is closed: `getfenv` returns `nil` in this sandbox**, so the menu state's globals
cannot be enumerated to find the file list the screen demonstrably populated (it renders all five
saves correctly).

**Status: unresolved.** The location hypothesis was correct and is real progress; the mechanism is
still not reachable from a one-shot tuner command. Remaining leads, cheapest first: vary the query
parameter names/values (`Directory` vs `Location`, other `SaveLocations`/`SaveFileTypes` members),
and `LuaEvents.FileListQueryComplete` as the completion signal with the results fetched some other
way.

**A useful enabling fact was established along the way: Lua globals persist across tuner commands.**
`CIVSIM_PERSIST_TEST = 4242` set in one command read back in the next. So the async pattern *is*
implementable — register a handler storing results in a global, read the global in a later command —
**once the correct event name is known.** That is the concrete next step, not a dead end.

## The UI load path works, and is the parity basis

Loading via the client's own UI succeeded and is what produced the identical-position result above.
The sequence, recorded because it is longer than expected and every step is a place automation can
stall:

1. `Escape` → in-game **Menu**
2. **Load Game**
3. Select the save from the list
4. **Load Game** button
5. ⚠️ **`CONFIRM LOAD FILE` modal** — *"This will lose any unsaved progress. Are you sure?"* → **Yes**
6. Civilization intro screen → **Continue Game** (note: the button reads `Continue Game` after a
   load, not `Begin Game` as on a new game)

Step 5 is the one that will catch an automation written from the obvious mental model, and step 6
changes its label depending on how the game was entered.

**All five Lua-written saves appeared correctly in the Load Game list**, by name, with working map
previews and full metadata — independent confirmation that `Network.SaveGame` produces genuine,
well-formed saves rather than files that merely exist.

## Save files are NOT byte-stable — do not verify branches by hash

Save → load → save, from a position proven identical by fingerprint:

```
preload  = 683,215 bytes
postload = 683,226 bytes      (+11 bytes)
differing bytes: 636,132
```

**The game position is identical; the files share almost no bytes.** Compression, serialisation
ordering, or embedded timestamps make the encoding non-deterministic.

> **Design consequence:** any implementation that verifies branch identity, replay correctness, or
> "the parent record is unchanged" by **comparing save-file bytes or checksums will always report a
> difference**, including when nothing is wrong. FR-034's "begin from an identical position" must be
> verified against **game state read through declared observations**, never against file bytes.
>
> Note this does *not* affect Scenario 5's `audit immutability` check on the parent **record** — that
> is store data, not save bytes. It affects any attempt to compare the `.Civ6Save` files themselves.

## Two findings outside this spike's remit

### The save file records that the tuner was active

The Load Game detail pane displays, for the harness-written saves:

```
Saved By Version: 1.0.12.9 (363760)
Tuner Active: Yes
```

**The save embeds provenance about how it was made.** This is not a parity violation — it is visible
in the standard UI, so a human sees it too, and it never reaches the agent's context. But it is worth
knowing that harness-made saves are self-identifying, and that `Saved By Version` is available as a
cross-check against the seed set's `game_build` pin, read from the save rather than from the client.

### Turns were advancing on their own — auto-end-turn is enabled on this host

During this spike the game advanced from turn 4 to 7 to 16 to 19 **with no end-turn issued by the
harness**. The quickstart lists *"Auto-end-turn disabled in the game's options"* as a prerequisite,
and on this host **it is not disabled**.

This would defeat the run loop directly: FR-011's turn-advance verification compares the turn number
before and after an end-turn decision, and a turn that advances on its own is indistinguishable from
one the agent ended. It also breaks the FR-015 no-progress backstop's accounting.

**Preparation must verify this setting, not assume it.** It was not located in `AppOptions.txt` or
`UserOptions.txt` by name; it is likely an in-game Options entry and needs its own short spike.

## Recommended next steps

1. **Find the file-list results event name** so `UI.QuerySaveGameList` → `Network.LoadGame` can be
   completed. Globals persisting across commands means the async pattern is already viable.
2. **If that fails, the load path is a documented Principle II `firetuner_gap`** — and unlike the
   save path, the gap is real. The bespoke driver would be the UI sequence above, whose parity basis
   is exactly the steps a human takes.
3. **Do not compare save bytes** anywhere in branch or replay verification.
4. **Settle auto-end-turn** before any unattended run.

## Scope limits

- One save, one load, one client session, turn 1 → 19 → 1.
- The fingerprint covers turn, yields, civ/leader, unit type+position+damage+moves, and city
  count/position/population. **It does not cover** fog of war, diplomatic state, AI internal state,
  RNG state, or great-people/religion progress — all of which are part of "identical position" in the
  full sense and none of which were compared.
- Linux only. The `Network.LoadGame` refusal is very unlikely to be platform-specific, but the UI
  sequence's coordinates certainly are.
