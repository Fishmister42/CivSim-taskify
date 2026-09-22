# Accessor-existence audit of every Lua body (2026-09-21)

**Status:** UNVERIFIED LIVE. Every finding and every replacement below was read out of Civilization
VI's own shipped Lua and out of the shipped engine binaries on this machine. Nothing here was
exercised against a running client; this audit was performed headless, with no client, no Steam and
no tuner port.

**Why this exists.** Three times on 2026-09-21 the same defect was found by accident: a body called
a method that does not exist in Civilization VI, behind a `if obj.Method then` guard or a `pcall`
that swallowed the absence, so the harness reported an honest-looking empty value for hundreds of
steps and the dependent action could never become available.

- `lua/gamecore/cities.lua` called `buildQueue:GetAvailableProduction()` -> `available_productions`
  was `[]` on 399 steps -> `cities.set_production` never available (fixed in `1d0b372`).
- `lua/gamecore/units.lua` called `unit:GetAvailablePromotions()` -> `available_promotions` was `[]`
  everywhere -> `units.promote` never available (fixed in `6606d4b`).
- `lua/ingame/city_orders.lua` used `CityOperationTypes.PARAM_PRODUCTION_ITEM`, no such key.

The T213 spike proved every observation body "produces a value" live. An empty list is a value, so
that proof could not catch any of these. This audit replaces "does it answer?" with "does the name
exist at all?", asked of every accessor in `lua/**`.

---

## 1. Method

### 1.1 The reference corpus

Four independent oracles, built on this machine from the installed game
(`~/.steam/debian-installation/steamapps/common/Sid Meier's Civilization VI/`):

| Oracle | What it is | Size |
|---|---|---|
| **UI/script Lua** | every `*.lua` under `steamassets/` — `base/assets/ui/**`, `base/assets/maps/**`, `dlc/*/ui/**`, `dlc/*/scripts/**`, `ctp/`, `debug/`, `launchpad/` | 645 files; 1 676 distinct `:Method(` names, 19 405 distinct dotted names |
| **Engine Lua-binding table** | `strings` over `Civ6`, `libGameCore_Base.so`, `libGameCore_XP1.so`, `libGameCore_XP2.so`, matching the engine's own registration symbol shape `GameCore::…::Lua::I<Interface>::l<Method>` | 1 861 bindings across 121 interfaces (`IUnit`, `ICityBuildQueue`, `IPlayerGovernors`, `IWorldCongress`, …) |
| **Engine UI-binding names** | bare `l<Name>` strings in the same binaries (the UI-side `UI.*` registrations) | 4 731 names |
| **Shipped linker maps** | `steamassets/dlc/expansion1/binaries/win64/gamecore_xp1_finalrelease.map` and `.../expansion2/binaries/win64/gamecore_xp2_finalrelease.map` — the Windows gamecore builds ship with their full MSVC map files, in which every Lua-callable method appears as a trampoline symbol `?l<Name>@I<Interface>@Lua@Cache@GameCore@@KAHPEAUlua_State@@@Z` in a `Cache_Lua_I<Interface>.obj` translation unit | 891 trampolines across 71 interfaces |

`steamassets/base/assets/gameplay` contains no Lua on this build; the gameplay-side scripts live
under `dlc/*/scripts/**` and are included in oracle 1, so the "a few engine methods are only used
from gameplay scripts" caveat is covered.

The two binary oracles are what make the negative results trustworthy rather than merely
suggestive. `GameCore::Lua::ICityBuildQueue::lGetAvailableProductionTypes` is in the binary;
`GetAvailableProduction` (the name `1d0b372` removed) is not, and neither is
`GetAvailablePromotions` (the name `6606d4b` removed). Both known phantoms are reproduced by the
method, which is the check that the method works.

The linker maps are the sharpest of the four, because a trampoline is not evidence *about* the Lua
API — it *is* the Lua API's registration, and it names the interface the method is bound on. That
is what distinguishes "this name does not exist" from "this name exists on a different object",
which four of the findings below turn on. For example
`?lRequestPolicyChanges@IPlayerCulture@Lua@Cache@GameCore@@…` (xp2 map `:49591`, `:90962-90963`)
settles both that `RequestPolicyChanges` is callable from Lua and that it lives on `IPlayerCulture`.

**Presence of a trampoline is conclusive; absence is strong but not airtight.** Three names that
Firaxis' own shipped Lua demonstrably calls have no trampoline symbol in either map or either
`.so`: `pPlayer:GetFavor()` (`dlc/expansion2/ui/replacements/toppanel_expansion2.lua:169`),
`culture:CanPolicyBeSlotted(hash)` and `culture:IsPolicyBanned(hash)`
(`dlc/expansion2/ui/replacements/governmentscreen_expansion2.lua:9-10`) — most likely
identical-code folding collapsing a trivial trampoline onto another. So **the usage citation is
the primary basis for every entry in `lua/ACCESSORS.txt`, and the maps corroborate it**, never the
other way round. Each of those three is called under its own `pcall` in the bodies below, with a
`<field>_reason` if it does not answer.

### 1.2 The extraction

Every `obj:Method(`, every `Namespace.Function(` and every `Table.CONSTANT` in `lua/**/*.lua`
(27 files, 5 274 lines), with `file:line`. Comment-only lines excluded. A name is a **phantom** when
it appears in none of the four oracles.

Inline Lua inside `src/civsim_harness/**/*.py` was extracted the same way and is **clean**: the only
absent name is `Modding.GetActiveGameVersion` (`src/civsim_harness/observe/game_build.py:158`),
which is already MEASURED-absent in that file's own comment at `:162-171` and already has a working
fallback (`UI.GetAppVersion()` at `:174`). No new finding there.

### 1.3 Result

**160 distinct accessor names are used across `lua/**`. 28 of them exist nowhere** — not in a single
one of 645 shipped Lua files, and not as a registered Lua binding in any of the four engine
binaries. The remaining 132 all have a shipped-corpus citation.

On top of those 28 names, the audit found **4 enum/table keys**, **9 result-field names** and **4
methods called on the wrong object or reachable only as a scenario-script setter**. The full list is
Section 2.

---

## 2. Findings

Legend for **How it was masked**: the mechanism that turned the absence into an honest-looking
value instead of an error.

### 2.1 Observation bodies — fields that were silently empty and are now real

| # | Site | Phantom | How it was masked | Replacement (Firaxis' own accessor, `steamassets/`) |
|---|---|---|---|---|
| 1 | `lua/gamecore/government.lua:77` | `culture:GetSlottablePolicies()` | `pcall` + `type(list)=="table"` -> `available_policies` `[]` forever | `GameInfo.Policies()` filtered by `culture:IsPolicyUnlocked(hash)` and `not culture:IsPolicyObsolete(hash)` — `base/assets/ui/screens/governmentscreen.lua:2224-2229`, built at `:2270-2273`; Gathering Storm replaces the predicate with `not IsPolicyBanned` + `CanPolicyBeSlotted` + `not IsPolicyObsolete` — `dlc/expansion2/ui/replacements/governmentscreen_expansion2.lua:8-14`. Cards already in a slot are excluded via `GetNumPolicySlots()`/`GetSlotPolicy(i)` — `governmentscreen.lua:2284-2289`. |
| 2 | `lua/gamecore/government.lua:86` | `culture:HasGovernment(index)` | `pcall` per row -> `available_governments` `[]` forever | `culture:IsGovernmentUnlocked(row.Hash)` — `governmentscreen.lua:2334` (takes a **hash**, while `GetCurrentGovernment()` returns an **index** — `:2250-2254`) |
| 3 | `lua/gamecore/government.lua:95` | `governorList.Members` (field probe) | `if governorList.Members then` -> loop never entered -> `governors` `[]` forever | `local bHasGovernors, tGovernorList = playerGovernors:GetGovernorList()`, then `ipairs` — `dlc/expansion1/ui/additions/governorpanel.lua:54,58,85`; `:Members()` is a **city-list** iterator, not a governor-list one (`dlc/expansion1/ui/additions/governorsupport.lua:99-100`) |
| 4 | `lua/gamecore/government.lua:98` | `governor:GetAssignedCityID()` | `governor.GetAssignedCityID and …  or nil` -> always `nil` | `governor:GetAssignedCity()` returns a **city object** or nil — `dlc/expansion1/ui/additions/governorsupport.lua:12-14` — then `:GetID()` |
| 5 | `lua/gamecore/great_people.lua:74` | `gpMgr:GetAvailableIndividuals(pid)` | `if gpMgr.GetAvailableIndividuals then` -> `recruitable_individuals` `[]` forever | `Game.GetGreatPeople():GetTimeline()` (global, one array) — `base/assets/ui/popups/greatpeoplepopup.lua:704,708` — filtered per player by `pGreatPeople:CanRecruitPerson(pid, entry.Individual)` — `:728`, which is exactly what hides/shows the Recruit button at `:265,269,276` |
| 6 | `lua/gamecore/great_people.lua:76-78` | `individual.Index`, `.ClassType`, `.Name` | fields off a `nil`-producing loop | `entry.Individual` (a `GameInfo.GreatPersonIndividuals` index) — `greatpeoplepopup.lua:726,741`; `entry.Class` — `:752`; `entry.Era` — `:753`; `entry.Claimant` — `:712`. Display name is `GameInfo.GreatPersonIndividuals[entry.Individual].Name` — `:741,746` |
| 7 | `lua/gamecore/great_people.lua:87` | `gpMgr:GetPlayerPoints(pid, classIndex)` | `if gpMgr.GetPlayerPoints then` + inner `pcall` -> `points_by_class` `[]` forever | `player:GetGreatPeoplePoints():GetPointsTotal(classID)` and `:GetPointsPerTurn(classID)` — a **Player** object, not the Game one — `greatpeoplepopup.lua:800-801`, corroborated `base/assets/ui/tutorialuiroot.lua:2095,2098` |
| 8 | `lua/gamecore/religion.lua:89` | `Game.GetReligion():GetAvailableBeliefs(pid)` | `pcall` + `type(list)=="table"` -> `available_beliefs` `[]` forever | No engine enumerator exists. `GameInfo.Beliefs()` filtered by `Game.GetReligion():IsInSomePantheon(row.Index)`, `:IsInSomeReligion(row.Index)` and `BeliefClassType` — `base/assets/ui/choosers/pantheonchooser.lua:29-40,69-77`; for a founded religion add `:IsTooManyForReligion(row.Index, religionType)` — `base/assets/ui/religionscreen.lua:451-469` |
| 9 | `lua/gamecore/congress.lua:98` | `Game.GetWorldCongress():GetActiveResolutions()` | `pcall` -> `active_resolutions` `[]` forever (on top of `Game.GetWorldCongress()` itself being nil in `GameCore_Tuner`, already documented in that file) | `pWorldCongress:GetResolutions(playerID)` — `dlc/expansion2/ui/additions/worldcongresspopup.lua:574`. The returned table mixes a `"Stage"` string key in with the numeric entries, so Firaxis iterates `pairs` + `type(i)=="number"` — `:580-581` |
| 10 | `lua/gamecore/congress.lua:102-104` | `res.ResolutionID`, `.ResolutionType`, `.ProposerID` | fields off a never-entered loop | The entry's key is `.Type`, used as `GameInfo.Resolutions[kResolutionData.Type]` — `worldcongresspopup.lua:590`. There is **no** resolution id and **no** proposer on the entry; the other real fields are `.TargetType`, `.PossibleTargets`, `.FavoredPlayerIDs`, `.ChosenOption`, `.PlayerSelections` (`:583,624,1584,627-651,1831,1838-1843`) |
| 11 | `lua/gamecore/congress.lua:113` | `Players[p]:GetStats():GetDiplomaticFavor()` | `pcall` -> `local_player_favor` `0` forever, so `congress.cast_vote`'s `player.diplomatic_favor > 0` could never hold | `Players[p]:GetFavor()` — directly on the player — `dlc/expansion2/ui/replacements/toppanel_expansion2.lua:169`; also `:GetFavorPerTurn()` `:170`, `:GetFavorEnteringCongress()` (`worldcongresspopup.lua:466`) |
| 12 | `lua/gamecore/espionage.lua:68,70` | `player:GetEspionage()`, `espionageMgr:GetSpies()` | `if … espionageMgr.GetSpies then` -> `spies` `[]` forever | There is no espionage object. Spies are units: iterate `Players[pid]:GetUnits():Members()` and keep `GameInfo.Units[unit:GetUnitType()].Spy` — `base/assets/ui/partialscreens/espionageoverview.lua:88-101` |
| 13 | `lua/gamecore/espionage.lua:72` | `spy:GetUnitID()` | `spy.GetUnitID and … or nil` | `unit:GetID()` (the spy **is** a unit) |
| 14 | `lua/gamecore/espionage.lua:73` | `spy:GetCurrentMission()` | same guard -> `mission` `null` forever | `unit:GetSpyOperation()` (`-1` when idle), resolved through `GameInfo.UnitOperations[…]` — `espionageoverview.lua:659,673`; remaining turns from `unit:GetSpyOperationEndTurn()` — `:677` |
| 15 | `lua/gamecore/espionage.lua:74` | `spy:GetTargetCityID()` | same guard -> `target_city_id` `null` forever | Derived from the spy's plot: `Cities.GetPlotPurchaseCity(Map.GetPlot(unit:GetX(), unit:GetY()))` then `:GetID()` — `espionageoverview.lua:643-644`, same derivation `base/assets/ui/panels/unitpanel.lua:2151-2155` |
| 16 | `lua/gamecore/espionage.lua:75` | `spy:IsAvailable()` | `spy.IsAvailable and … or false` -> `is_available` `false` forever, so `espionage.assign_mission` could never be available | `unit:GetSpyOperation() == -1` — `espionageoverview.lua:92-97,660`; the chooser additionally requires `unit:IsReadyToMove()` and `UnitManager.GetActivityType(unit) == ActivityTypes.ACTIVITY_AWAKE` — `base/assets/ui/choosers/espionagechooser.lua:741-755` |
| 17 | `lua/gamecore/units.lua:292` | `unit:GetActivityType()` | `if unit.GetActivityType and …` -> `queued_path` never emitted | `UnitManager.GetActivityType(unit)` — a **UnitManager** function, not a unit method — `base/assets/ui/panels/unitpanel.lua:2147` |
| 18 | `lua/gamecore/units.lua:292` | `UnitActivityType.ACTIVITY_OPERATION` | same guard short-circuits before the key is read | The enum table is `ActivityTypes` — `unitpanel.lua:2148`, `unitflagmanager.lua:856-869`. `UnitActivityType` does not exist |
| 19 | `lua/gamecore/units.lua:293` | `queued_path = { destination = nil }` | field emitted with a permanently-`nil` destination | **Principle I non-claim, removed.** No path-destination getter exists; `UnitManager.GetMoveToPathEx(unit, plotId)` (`base/assets/ui/worldinput.lua:961`) computes a *prospective* path for a plot the caller already names, which is not "where is this unit headed" |

### 2.2 Action bodies — orders that could never have been issued

| # | Site | Phantom | How it was masked | Replacement (`steamassets/`) |
|---|---|---|---|---|
| 20 | `lua/ingame/empire_orders.lua:117` | `culture:SetPolicyActive(slot, index)` | `pcall` -> `{ok=false}` every time (and `policies.slot_policy` was never *offered* anyway, because finding #1 kept `available_policies` empty) | **Not callable from Lua.** The native method exists (`GameCore::Player::Culture::SetPolicyActive`, xp2 map `:17226`) but has no `?l…@…@Lua@Cache@GameCore@@` trampoline in either map or either `.so`, and no shipped Lua calls it — it is engine-internal. The screen's Confirm button calls `culture:RequestPolicyChanges(clearList, addList)`, one call carrying the whole loadout: `clearList` an array of zero-based slot indices to empty, `addList` a sparse map slot -> policy **hash** — `base/assets/ui/screens/governmentscreen.lua:1549-1575` (the call at `:1570`; the "removals must ride along" reason at `:1555-1557`) |
| 21 | `lua/ingame/empire_orders.lua:132` | `culture:SetCurrentGovernment(index)` | `pcall` -> `{ok=false}` | **Callable, but a scenario-script god-setter.** It does have a trampoline (`IPlayerCulture::lSetCurrentGovernment`), so this one is a parity objection, not an existence one: its only appearance in shipped Lua is `dlc/blackdeathscenario/scripts/blackdeathscenario.lua:187`, forcing a government on a player, and it bypasses the anarchy/legality path the human's click goes through. The Confirm button calls `culture:RequestChangeGovernment(row.Hash)` — `governmentscreen.lua:912,929` |
| 22 | `lua/ingame/empire_orders.lua:146` | `governors:AssignGovernor(type, cityId)` | `pcall` -> `{ok=false}` | `UI.RequestPlayerOperation(pid, PlayerOperations.ASSIGN_GOVERNOR, {PARAM_GOVERNOR_TYPE = <Governors row index>, PARAM_CITY_DEST = cityID})` — `dlc/expansion1/ui/additions/governorpanel.lua:556-564`; the cross-player form adds `PARAM_PLAYER_ONE` — `dlc/expansion1/ui/additions/governorassignmentchooser.lua:377-380` |
| 23 | `lua/ingame/diplomacy.lua:66` | `GetDiplomaticAI():DeclareWar(id)` | `pcall` -> `{ok=false}` | `UI.RequestPlayerOperation(attacker, PlayerOperations.DIPLOMACY_DECLARE_WAR, {PARAM_PLAYER_ONE=attacker, PARAM_PLAYER_TWO=defender})` — `base/assets/ui/popups/declarewarpopup.lua:77-81`. (`GetDiplomaticAI()` is read-only opinion data — the only seven methods ever called on it are `GetDiplomaticStateIndex`, `GetDiplomaticScore`, `GetDiplomaticModifiers`, `GetThreatFrom/String`, `GetTrustFrom/String`.) |
| 24 | `lua/ingame/diplomacy.lua:74` | `GetDiplomaticAI():MakePeace(id)` | `pcall` -> `{ok=false}` | `UI.RequestPlayerOperation(pid, PlayerOperations.DIPLOMACY_MAKE_PEACE, {PARAM_PLAYER_ONE, PARAM_PLAYER_TWO})` — `base/assets/ui/partialscreens/citystates.lua:815-818` |
| 25 | `lua/ingame/diplomacy.lua:82` | `GetDiplomacy():SendDelegation(id)` | `pcall` -> `{ok=false}`; this is one of the five actions the coverage report lists as attempted-but-never-applied | `DiplomacyManager.RequestSession(localPlayerID, otherPlayerID, "DIPLOMATIC_DELEGATION")` — `base/assets/ui/diplomacyactionview.lua:466-467`. There is no delegation `PlayerOperation` and no delegation `DealAgreementType` |
| 26 | `lua/ingame/congress.lua:66` | `Game.GetWorldCongress():CastVote(...)` | `pcall` -> `{ok=false}` | `UI.RequestPlayerOperation(pid, PlayerOperations.WORLD_CONGRESS_RESOLUTION_VOTE, {PARAM_RESOLUTION_TYPE = <Resolutions hash>, PARAM_WORLD_CONGRESS_VOTES = n, PARAM_RESOLUTION_OPTION = 1|2, PARAM_RESOLUTION_SELECTION = target-1})` — `dlc/expansion2/ui/additions/worldcongresspopup.lua:2239-2253`; the turn is then submitted with `PlayerOperations.WORLD_CONGRESS_SUBMIT_TURN` — `:2270` |
| 27 | `lua/ingame/great_people.lua:66` | `Game.GetGreatPeople():Recruit(pid, id)` | `pcall` -> `{ok=false}` | `UI.RequestPlayerOperation(pid, PlayerOperations.RECRUIT_GREAT_PERSON, {PARAM_GREAT_PERSON_INDIVIDUAL_TYPE = individualIndex})` — `base/assets/ui/popups/greatpeoplepopup.lua:886-894` (an **index**, not a hash) |
| 28 | `lua/ingame/religion.lua:70` | `religionMgr:ChoosePantheon(index)` | `pcall` -> `{ok=false}` | `UI.RequestPlayerOperation(pid, PlayerOperations.FOUND_PANTHEON, {PARAM_BELIEF_TYPE = GameInfo.Beliefs[i].Hash, PARAM_INSERT_MODE = VALUE_EXCLUSIVE})` — `base/assets/ui/choosers/pantheonchooser.lua:125-139` (a **hash** here, unlike great people) |
| 29 | `lua/ingame/religion.lua:83` | `Players[p]:GetReligion():FoundReligion(relIdx, beliefIdx)` | `pcall` -> `{ok=false}` | **Wrong object and wrong arity**: `FoundReligion(playerID, religionIndex)` exists only on `Game.GetReligion()` and only in scenario scripts (`dlc/polandscenario/scripts/polandscenario.lua:567`). The human's path is `PlayerOperations.FOUND_RELIGION` with `PARAM_RELIGION_TYPE` (hash) + `PARAM_INSERT_MODE`, then a **separate** `PlayerOperations.ADD_BELIEF` request per belief — `base/assets/ui/religionscreen.lua:903-919` |
| 30 | `lua/ingame/religion.lua:95` | `Players[p]:GetReligion():AddBelief(index)` | `pcall` -> `{ok=false}` | Same: `AddBelief(playerID, beliefIndex)` is a `Game.GetReligion()` scenario setter (`polandscenario.lua:568`). The human's path is `PlayerOperations.ADD_BELIEF` with `PARAM_BELIEF_TYPE` (hash) + `PARAM_INSERT_MODE` — `religionscreen.lua:914-919` |
| 31 | `lua/ingame/espionage.lua:83` | `UnitOperationTypes.PARAM_SPY_MISSION` | key reads `nil`, so `tParameters[nil] = …` raises inside the call | No `PARAM_SPY*` key exists. Spy operations take only `PARAM_X`/`PARAM_Y` — `base/assets/ui/choosers/espionagechooser.lua:369-375,612-616`; the offensive-mission request passes **no** parameter table at all (`UnitManager.RequestOperation(spy, operation.Hash)`) — `base/assets/ui/popups/espionagepopup.lua:472-477` |
| 32 | `lua/ingame/espionage.lua:86` | `UnitOperationTypes.SPY_MISSION` | `nil` operation type -> `RequestOperation` cannot match anything | The real constants are `SPY_COUNTERSPY`, `SPY_GAIN_SOURCES`, `SPY_GREAT_WORK_HEIST`, `SPY_LISTENING_POST`, `SPY_SIPHON_FUNDS`, `SPY_STEAL_TECH_BOOST`, `SPY_TRAVEL_NEW_CITY`; the legal set for a given city comes from `GameInfo.UnitOperations()` where `CategoryInUI == "OFFENSIVESPY"`, each tested with `UnitManager.CanStartOperation(spy, operation.Hash, cityPlot, false, true)` — `espionagechooser.lua:196-236` |
| 33 | `lua/ingame/camera.lua:74` | `UI.SetCameraZoom(z)` | `pcall` -> `{ok=false}`; `camera.zoom` is one of the three camera actions never applied | `UI.SetMapZoom(zoom, 0.0, 0.0)` — `base/assets/ui/worldinput.lua:1204`. No `UI.*` name in the whole shipped corpus contains "Camera"; the API is spelled "Map" |
| 34 | `lua/ingame/camera.lua:91,93` | `ActionTypes["ToggleStrategicView"]` | `ActionTypes and ActionTypes[…]` -> `UI.RequestAction(nil)` | `UI.SetWorldRenderView(WorldRenderView.VIEW_2D)` / `VIEW_3D` — `base/assets/ui/minimappanel.lua:369-381` (`Toggle2DView`). VIEW_2D **is** the strategic view |
| 35 | `lua/ingame/camera.lua:125` | `UI.GetCameraTargetPlot()` | already MEASURED-absent; kept as a legacy first-try | Removed. `UI.GetMapLookAtWorldTarget()` + `UI.GetPlotCoordFromWorld(wx, wy)` — `base/assets/ui/automation/automation_observercamera.lua:381-382` — is already this file's primary path (T260) and is the only look-at read in the corpus |
| 36 | `lua/ingame/camera.lua:198` | `UI.GetCameraZoom()` | already MEASURED-absent; `pcall` then falls through to `UI.GetMapZoom()` | Removed; `UI.GetMapZoom()` — `base/assets/ui/worldview/cameramanager.lua:41` — becomes the only read |
| 37 | `lua/ingame/camera.lua:203` | `UI.IsStrategicView()` | already MEASURED-absent; `pcall` then falls through | `UI.GetWorldRenderView() == WorldRenderView.VIEW_2D` — `base/assets/ui/worldview/citybannermanager.lua:1466`, `minimappanel.lua:369`. This also resolves the "the strategic value is UNVERIFIED and assumed 1" note in that file: the comparison is against the named enum, never a literal |
| 38 | `lua/ingame/screens.lua:1207` | `UI.RespondToPrompt(type, option)` | `pcall` -> `{ok=false}` with no reason, for any watchlist prompt that has no mapped control | There is no such primitive. A popup answers by releasing the engine hold it took (`UI.ReferenceCurrentEvent()` / `UI.ReleaseEventID()` — `base/assets/ui/popupmanager.lua:52-61,95-98`) after its own button callback runs (`base/assets/ui/popups/popupdialog.lua:215-217`). Nothing generic can stand in for a specific button, so this fallback is replaced by an explicit refusal naming the gap |

### 2.3 Not a defect, recorded for completeness

- `Modding.GetActiveGameVersion` (`src/civsim_harness/observe/game_build.py:158`) — absent, already
  MEASURED and documented in place, with a working `UI.GetAppVersion()` fallback. Left alone.
- `Game.GetWorldCongress()` is nil in `GameCore_Tuner` and a function in `InGame`
  (`spikes/sweep-raw/*_P3_*.txt`). `congress.state` is declared `GameCore_Tuner`, so its session
  read fails in its own context before any of the above matters. That is a **context** defect, not an
  accessor defect; it is recorded in `lua/gamecore/congress.lua`'s header and is not fixed here.
- `unit:GetBuildCharges()`, `plot:HasBeenPlaced()`, `buildQueue:GetAt()`, `city:CanProduce()`,
  `diplomacy:HasDelegationAt()` and every other name in `lua/**` not listed above have a shipped-Lua
  citation; they are recorded in `lua/ACCESSORS.txt`.

---

## 3. What this changes about the board

Observation fields that read as an honest empty list on every step, and are now read from the
accessor the panel uses:

`government.state.available_policies`, `.available_governments`, `.governors`,
`.available_governors`; `great_people.state.recruitable_individuals`, `.points_by_class`;
`religion.state.available_beliefs`; `congress.state.active_resolutions`, `.local_player_favor`;
`espionage.state.spies` (and every field on a spy).

Actions whose availability predicate reads one of those fields, and which therefore could never
become available no matter what the game state was:

`policies.slot_policy`, `policies.change_government`, `policies.assign_governor`,
`great_people.recruit`, `religion.select_pantheon`, `religion.found_religion`,
`religion.select_belief`, `congress.cast_vote`, `espionage.assign_mission`.

Actions that could be *offered* but whose body could never succeed:

`diplomacy.declare_war`, `diplomacy.make_peace`, `diplomacy.send_delegation`, `camera.zoom`,
`camera.set_view_mode`.

That is 14 of the 38 declared actions. It is also the explanation for the coverage line
"applied 9 of 38": the five attempted-but-never-applied actions the report names (camera x3,
`send_delegation`, `ai_diplomatic_approach`) are in this list, and most of the nine never-drawn ones
were unavailable because the observation behind them was permanently empty.

`tests/live/goals/promote_unit.yaml`'s note — "`available_promotions` is `[]` for every unit on every
record in the store today, which is a game-state fact, not a body gap" — was wrong in exactly this
way, and is corrected as part of this work.

---

## 4. Guarding against recurrence

`lua/ACCESSORS.txt` is a checked-in allowlist: one line per accessor name used anywhere in
`lua/**/*.lua` -- 216 of them after the fixes, 99 object methods and 117 `Namespace.Name` globals --
each carrying the `steamassets/`-relative `file:line` where Firaxis' own code uses that name. The
globals are included because three of the findings above were *constants*
(`UnitActivityType.ACTIVITY_OPERATION`, `UnitOperationTypes.SPY_MISSION`,
`CityOperationTypes.PARAM_PRODUCTION_ITEM`), which is the same defect wearing different clothes: a
nil key poisons the parameter table it is written into. It is generated from the shipped corpus, it contains only names the harness actually
uses, and there is no wildcard.

`tests/unit/test_lua_accessors.py` re-extracts every `obj:Method(` and every `Namespace.Function(`
from `lua/**/*.lua` and asserts the set matches the allowlist **in both directions**:

- a name in the Lua that is not in the allowlist fails — this is the phantom guard;
- a name in the allowlist that no longer appears in the Lua fails — so the list cannot rot into a
  blanket permission slip.

Adding a genuinely new accessor is therefore a two-line change: the call, and its citation. Writing
a name Civilization VI does not have is a CI failure instead of an empty list that nobody notices
for 399 steps.

The test does not itself read the game install (CI has no Civ VI), which is why the citations are
checked in rather than re-derived. The extractor used to build them is described in Section 1 and is
reproducible from any machine with the game installed.

---

## 5. Caveats

- **Everything here is UNVERIFIED LIVE.** The replacements are what Firaxis' own panels call. That
  they answer in this harness's declared context (`GameCore_Tuner` vs `InGame`) is a separate
  question, and for `congress.state` the answer is already known to be "no" (Section 2.3).
- Index-vs-hash is inconsistent across Civilization VI's own systems and the inconsistency is
  load-bearing: great-people operations take an **index**
  (`PARAM_GREAT_PERSON_INDIVIDUAL_TYPE`, `greatpeoplepopup.lua:890`), religion and pantheon
  operations take a **hash** (`pantheonchooser.lua:130`, `religionscreen.lua:906,916`),
  `IsGovernmentUnlocked`/`RequestChangeGovernment` take a **hash** while `GetCurrentGovernment`
  returns an **index** (`governmentscreen.lua:2250,2334,912`). Each body follows its own panel.
- The engine-binary oracles can only prove **presence**; a name present there may still be bound on
  a different object than the one the harness calls it on. Findings 17, 21, 29 and 30 are exactly
  that case, and were caught by reading the caller rather than by the name check.
- The converse also holds, in both directions, and neither is a licence to guess:
  - **Zero usage is not nonexistence.** `culture:CanChangeGovernment(...)` has a real trampoline
    (`IPlayerCulture::lCanChangeGovernment`) but no shipped panel calls it — the government screen
    uses `CanChangeGovernmentAtAll()` at `governmentscreen.lua:866`. A name absent from the Lua
    corpus but present in a binding table is *unused*, not *invented*.
  - **No trampoline is not nonexistence either**, per §1.1's three ICF cases. The bodies call all
    three under their own `pcall` and report a `<field>_reason` if they do not answer, which is the
    only honest posture toward a name the oracles disagree about.
- Neither map covers base-game gamecore; they are the XP1 and XP2 builds only. The `.so` binding
  table covers all three, so the two together span the API, but a name found only in one should be
  read as "registered in that build", not "registered everywhere".

## 2026-09-22: `nm -DC` is sound as a POSITIVE discriminator and unsound as a negative

The symbol table settled `research.set_civic` on 2026-09-22 (`0989e3b`) and deserves the credit: it
was the only instrument that could separate two byte-for-byte parallel Lua bodies, by showing
`GameCore::Cache::Lua::IPlayerCulture::lCanProgress` exported in all three libraries while
`GameCore::Lua::IPlayerCulture::lCanProgress` is exported in none, and `lCanResearch` exported in
both. **That is the positive direction: name present in one namespace and absent in the other, with
a known-positive control confirming the pattern can match.**

**Its silence is NOT evidence, and this was found the same day.** No `l*Favor*` symbol exists on any
player interface in any of the three shipped libraries -- yet `strings -a` finds the registration
names `GetFavor` (Base, XP1) and `GetDiplomaticFavor` (XP2), and Firaxis' own shipped XP2 UI calls
`Players[id]:GetFavor()` (`worldcongresspopup.lua:181`, `toppanel_expansion2.lua:169`,
`diplomacyribbon_expansion2.lua:77`). **Some Lua bindings exist only as name strings with no exported
`l<Name>` trampoline**, so an absent symbol is consistent with both "not bound" and "bound by a
mechanism this tool cannot see".

**Rule, and it is the project's negative-search rule applied to a binary:** use `nm -DC` to answer
*"is this bound HERE and not THERE"*, never *"does this exist at all"*. Before trusting any absence,
corroborate with `strings -a` over the same libraries and with a grep for Firaxis' own callers in the
shipped `.lua`. **Two negatives agreeing is not a measurement** -- they share the failure mode of
looking for an exported symbol that need not exist.

This sits alongside the standing caution on `ACCESSORS.txt`, which answers *"does this method exist"*
and must never be read as *"is it callable from here"*. The two instruments fail in opposite
directions and neither answers the other's question.
