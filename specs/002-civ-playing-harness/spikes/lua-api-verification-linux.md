# Lua API Verification Sweep — Linux live client

**Feature**: `002-civ-playing-harness` · **Executed**: 2026-09-20 · **Host**: Linux (native Aspyr)
**Client**: `1.0.12.9 (564030)`, Gathering Storm, BBG active · **Game state**: single player, turn 1–2

Settles the `-- UNVERIFIED:` markers the catalog authors left behind. Method is direct name probing
under `pcall` in both tuner contexts — see [r5-save-path.md](./r5-save-path.md) for why iteration
does not work in this sandbox (`_G` is nil; `UI`/`Network` are opaque userdata).

Raw unedited output: [`sweep-raw/`](./sweep-raw/).

## Headline results

| Priority | Question | Answer |
|---|---|---|
| **P1** | What ends a turn, and how is it verified? | **Solved.** `UI.RequestAction(ActionTypes.ACTION_ENDTURN)`, verified by `Game.GetCurrentGameTurn()`. Live-proven 1 → 2. |
| **P2** | Can the tuner see which UI screen is open? | **Solved — positive, and not via `UIManager`.** Per-screen `ContextPtr:IsHidden()`. Live-proven. |
| **P3** | Do the congress / great-people / religion surfaces exist? | **All 8 exist** in `InGame`. |
| **P4** | Spot-check the higher-confidence reads | **18 / 20 exist.** Two named wrong. |
| **P5** | Is a JSON library present? | **No.** The hand-rolled encoder is correct and must stay. |

---

## P1 — Turn control (`lua/ingame/turn_control.lua`)

**The file's primary path is wrong and its fallback is right.**

| Symbol | `InGame` | `GameCore_Tuner` |
|---|---|---|
| `Game.EndTurn` | **`nil`** — does not exist | `nil` |
| **`UI.RequestAction`** | **`function`** | `ERROR` (no `UI`) |
| **`ActionTypes.ACTION_ENDTURN`** | **`number`** (751412917) | `ERROR` |
| **`UI.CanEndTurn`** | **`function`** | `ERROR` |
| **`Game.GetCurrentGameTurn`** | **`function`** | `function` |
| `EndTurnBlockingTypes.NO_ENDTURN_BLOCKING` | `number` | `number` |
| `Game.SetCurrentGameTurn` | `nil` | `nil` |
| `Network.SendPlayerOperation` / `SendAction` | `nil` | `ERROR` |
| `PlayerOperations.ENDTURN` | `nil` | `nil` |
| `PlayerOperationTypes.ENDTURN` | `ERROR` (root missing) | `ERROR` |
| `ActionTypes.ACTION_NEXTREADYUNIT` | `nil` | `ERROR` |
| `NotificationManager.GetCount` | `nil` | `ERROR` |
| `UI.GetHeadSelectedUnit` / `UI.SelectNextReadyUnit` | `function` | `ERROR` |

### Live proof — both halves of FR-011

```
--- before ---
turn=1
canEndTurn=true
ACTION_ENDTURN=751412917

--- issuing UI.RequestAction(ActionTypes.ACTION_ENDTURN) ---
requestaction ok=true ret=nil

--- after ---
turn=2
canEndTurn=true
```

**Recommended shape for the capability:**

- **Precondition**: `UI.CanEndTurn()` — a real boolean, available before acting. This is the cheap
  guard against issuing an end-turn the game will swallow.
- **Action**: `UI.RequestAction(ActionTypes.ACTION_ENDTURN)`.
- **Verification**: `Game.GetCurrentGameTurn()` before and after, compared. **This is mandatory, not
  optional** — `UI.RequestAction` returns `nil`, so its return value carries no information about
  whether anything happened. An end-turn that was swallowed and one that succeeded are
  indistinguishable at the call site and distinguishable only by the turn counter.

That satisfies the "issue it *and* verify it" requirement in the ask. Note the verification is also
what distinguishes a genuine turn advance from the no-progress backstop case in FR-015.

### One caution about `ACTION_ENDTURN`

It is a **hash** (751412917), not a stable small enum. Read it from `ActionTypes` at runtime; do not
hard-code the integer. See [the hash note below](#config-values-are-hashes-not-strings) — the same
issue shows up across the configuration surface.

---

## P2 — Screen identity (`lua/ingame/screens.lua`)

The file was flagged as the highest-uncertainty in the catalog, with the fear that **no way exists**
to ask which screen is topmost. The fear is half-right and the outcome is good.

**No `UIManager` screen-identity API exists:**

| Symbol | `InGame` |
|---|---|
| `UIManager.GetScreen` | `nil` |
| `UIManager.GetTopmostScreen` | `nil` |
| `UIManager.GetCurrentPopup` | `nil` |
| `UIManager.ClosePopup` | `nil` |
| `UI.IsScreenOpen` / `UI.GetScreenName` | `nil` |
| `UIManager.IsInPopupQueue` | `function` |
| `UIManager.IsPopupQueueEmpty` | `function` |
| `UIManager.QueuePopup` / `DequeuePopup` | `function` |
| `UI.GetInterfaceMode` / `SetInterfaceMode` | `function` |
| `ContextPtr` / `Controls` / `LuaEvents` / `Events` | `table` |

### But screen identity *is* reachable — via the state table, not via `UIManager`

**Every Civ VI UI screen is its own Lua state** (137 of them in game; see
[`r5-raw/00_states.txt`](./r5-raw/00_states.txt)). Each state has its own `ContextPtr`, and
`ContextPtr:IsHidden()` reports whether that screen is currently showing.

**Proven by flipping a known screen.** Baseline, all 26 watched screens reported `hidden=true`. Then
a single Escape was sent to the client and every state re-read:

```
--- CHANGED after Escape ---
  InGameTopOptionsMenu       true  ->  false
```

Exactly one state flipped, and it was the correct one. Full before/after table:
[`sweep-raw/screen_identity.md`](./sweep-raw/screen_identity.md).

**This means `screens.lua` does not need a bespoke path and FR-010 / FR-049 are reachable through
FireTuner.** There is no `firetuner_gap` to document here.

### Three honest limitations

1. **It answers "is screen X open", not "what is topmost."** Z-order is not exposed. Deriving the
   open *set* is straightforward; ranking that set is not. For FR-049's purpose — *did we hit a
   screen we do not recognise* — the open set is sufficient, since the unknown case is "the game is
   blocked and nothing in our known list is open."
2. **Cost is one round-trip per screen state.** There is no single call returning all of them,
   because the states are genuinely separate Lua environments. Scanning 26 screens is 26 commands.
   That is fine per *prompt-check* but would be expensive if run every step; worth a targeted list
   ordered by likelihood rather than a full scan.
3. Screens whose state is **not instantiated until first use** would report `STATE_ABSENT` rather
   than `hidden=true`. All 26 watched states existed at turn 1, but that should not be assumed for
   rarely-opened screens across a whole game.

---

## P3 — Congress, great people, religion

Written by analogy and unvalidated. **All 8 exist in `InGame`**, so the analogy held:

```
Game.GetWorldCongress     -> function      Game.GetReligion        -> function
Game.GetGreatPeople       -> function      Game.GetEmergencyManager-> function
Game.GetHistoryManager    -> function      Game.GetEras            -> function
Game.GetQuestsManager     -> function      Game.GetGossipManager   -> function
```

In `GameCore_Tuner` only 4 of the 8 are present — the manager accessors are a `InGame`-side surface.
Per-context detail in [`sweep-raw/`](./sweep-raw/). **Existence is all that was checked**; the
methods *on* the returned manager objects are not verified here.

---

## P4 — Spot checks

**18 / 20 confirmed.** Everything believed plausible was correct except two names:

```
Map.GetGridSize / GetPlot / GetPlotByIndex / GetPlotDistance      -> function
UnitManager.RequestOperation / RequestCommand                      -> function
UnitManager.GetOperationTargets / CanStartOperation                -> function
UnitOperationTypes.PARAM_X / PARAM_Y / FOUND_CITY / MOVE_TO        -> number
UnitCommandTypes.PARAM_X                                           -> number
CityManager.RequestOperation / RequestCommand                      -> function
CityCommandTypes.PARAM_X                                           -> number
Players                                                            -> table
PlayerManager.GetAlive                                             -> function
```

**Wrong:**

| Symbol | Result |
|---|---|
| `Cities.GetCity` | `nil` — no `Cities` global with that shape |
| `Units.GetUnit` | `nil` — same |

Both are reachable through the `Players` table instead, which does exist. Any catalog entry naming
`Cities.GetCity` or `Units.GetUnit` will fail at runtime.

---

## P5 — No JSON library, and what else is missing

**There is no JSON library in either context.** `json`, `JSON`, `cjson`, `dkjson`, `Serialize` are
all `nil`. **The hand-rolled encoder in `lua/` is correct and must stay.**

The sandbox is also narrower than stock Lua in ways worth knowing before writing more Lua:

| Symbol | `InGame` | Note |
|---|---|---|
| `require` | `nil` | No module loading — every Lua body must be self-contained |
| `io` | `nil` | No file I/O from Lua; this is also a useful Principle I property |
| `debug` | `nil` | No introspection; signatures cannot be recovered from the client |
| `os` | `table` | Present |
| `loadstring` | `function` | Present |
| `DB`, `GameInfo`, `Locale`, `Modding`, `Options`, `UserConfiguration` | `table` | Present |

`debug` being absent is why this document reports `EXISTS` / `DOES NOT EXIST` and not real
signatures — the client cannot be asked for them, so arity is only discoverable by calling.

---

## Bonus: the FR-002 / V2 setup read-back path works

Not asked for, but it came free while the tuner was open and it unblocks the config-pinning
question. `GameConfiguration` exists with **82 members**, including everything preparation needs to
read the actual setup back:

`GetRuleSet`, `GetHandicapType`, `GetGameSpeedType`, `GetStartEra`, `GetMaxTurns`, `GetStartTurn`,
`GetStartYear`, `GetAIPlayerCount`, `GetHumanPlayerCount`, `GetParticipatingPlayerCount`,
`GetEnabledMods`, `GetEnableModsMetaString`, `GetTurnTimerType`, `IsAnyMultiplayer`, `GetValue`.

Civ and leader come from `PlayerConfigurations[Game.GetLocalPlayer()]`:

```
LocalPlayerID   = 0
LocalCiv        = CIVILIZATION_ENGLAND
LocalLeader     = LEADER_ELEANOR_ENGLAND
RuleSet         = RULESET_EXPANSION_2
MapScript       = Continents.lua
AIPlayerCount   = 16
HumanPlayerCount= 1
StartTurn       = 1
StartYear       = -4000
IsAnyMultiplayer= false
```

### Config values are hashes, not strings

**This is the trap.** Some getters return readable strings (`RULESET_EXPANSION_2`,
`CIVILIZATION_ENGLAND`) but most return **Civ VI type hashes**:

```
Handicap(difficulty) = -179952465
GameSpeed            = 327976177
StartEra             = -1851407529
GameMode             = -379035929
TurnTimer            = 2133509568
MapValue_SIZE        = -1837222328
```

Two consequences for V2's field-by-field comparison:

1. **Do not log these as if they were names** — a report saying `difficulty: -179952465` is not
   human-auditable, and Principle III wants records that reconstruct decisions.
2. **Comparing hashes to hashes is actually fine, and more robust than comparing display names**
   (no localisation dependency). The recommendation is to *store* the hash as authoritative and
   resolve a display name through `GameInfo`/`DB` for the record. What must not happen is pinning a
   seed set to a *localised* string.

### The mod set is not empty — it is 22 mods

`configs/seedsets/shuffle-classic-2026q3.yaml` carrying a placeholder `mod_set` will fail V3 against
this host. The live list:

```
Expansion: Rise and Fall · Expansion: Gathering Storm
Better Balanced Game 7.5.0 · Better Balanced Map 1.39.5 · Multiplayer Helper 1.7.9
Extended Policy Cards · TopPanel Extension Pro
Aztec · Poland · Nubia · Khmer and Indonesia · Persia and Macedon · Australia · Vikings
Byzantium and Gaul · Babylon · Maya and Gran Colombia · Ethiopia · Portugal · Vietnam and Kublai Khan
Teddy Roosevelt Persona · Catherine de Medici Persona
```

`GetEnableModsMetaString()` returns this as a JSON document including `modid`, `version`, `title`,
and `subscriptionid` — which is a better basis for pinning than the display titles, since titles are
localised and some carry colour markup (one is literally
`[ENDCOLOR]TopPanel Extension [COLOR:ResGoldLabelCS]Pro[ENDCOLOR]`).

Full output: [`sweep-raw/gameconfig_values.txt`](./sweep-raw/gameconfig_values.txt).

---

## Scope limits on everything above

- **Existence is not arity.** Every result is `type()` of the symbol. No function was called except
  `UI.RequestAction(ACTION_ENDTURN)`, `UI.CanEndTurn`, `Game.GetCurrentGameTurn`,
  `ContextPtr:IsHidden`, and the `GameConfiguration` getters listed. With `debug` absent, signatures
  cannot be recovered without calling.
- **Turn 1–2 of one game.** Screens that only exist later (World Congress, era transitions, great
  people) were probed for *state* existence, not exercised.
- **BBG is active.** These results describe a BBG-loaded client. Mods can add and shadow Lua states.
- **Linux only**, though the Lua surface is unlikely to differ by platform.
- `lua/` files were **not edited** — reporting only, per the ownership rule.
