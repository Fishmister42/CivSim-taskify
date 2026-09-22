# Capability Catalog — Authoring Conventions and Predicate Symbol Vocabulary

This file documents the conventions used across every YAML file in `catalogs/` and the Lua files
under `lua/`, so a later implementer of the restricted predicate evaluator (T106) and the catalog
loader (T035) can build against a fixed, written-down vocabulary instead of reverse-engineering it
from scattered predicate strings. It is data/reference for the swarm, not a project README.

`catalogs/VERSION` is authoritative for the catalog version; this file never restates it.

## 1. Declaration and capability ID conventions

- `declaration_id` is dotted, domain-first, stable, and globally unique across every file in
  `catalogs/observations/`, `catalogs/observations/views.yaml`, and `catalogs/actions/` (e.g.
  `units.state`, `units.move_to`, `turn.end_turn`, `views.city_screen`).
- `capability_id` is dotted and domain-first, one per Lua file (or closely related pair of files),
  e.g. `units.read` → `lua/gamecore/units.lua`, `units.orders` → `lua/ingame/unit_orders.lua`.
  Several `declaration_id`s may share one `capability_id` when they are served by different
  functions in the same Lua library.
- Every `capability_id` referenced by a declaration resolves to exactly one entry in
  `catalogs/capabilities.yaml`.

## 2. Context split

- `lua/gamecore/*.lua` is **read-only** — it observes, it never issues an order.
- `lua/ingame/*.lua` is **write/act** — orders, production changes, and anything that touches a
  UI-bound surface (diplomacy, World Congress, screens).
- The directory says read-vs-write, **not** which tuner context the declaration runs in. The
  declaration's own `context` field is authoritative, and several read-only files are declared
  `InGame` because the accessors they need do not exist in `GameCore_Tuner`: `units.state`
  (T213 — `GetUnitType`, `GetReachableMovement`, `CanStartOperation` are InGame-only),
  `cities.state`, `yields.state`, and — since the 2026-09-21 accessor audit — `congress.state`
  (`Game.GetWorldCongress()` is MEASURED nil in `GameCore_Tuner`) and `espionage.state` (a spy is
  a unit, so it needs `unit:GetUnitType()`).
- `GameCore_Tuner` is read-only and faster; prefer it when every accessor a body needs answers
  there, and record the measurement when one does not.

## 3. Lua file conventions

Every `lua/**/*.lua` file in this catalog:

1. Opens with a header comment naming the context it runs in and the `declaration_id`s it backs.
2. Defines a local, hand-rolled JSON encoder (`CivSim_JsonEncode`). Civ VI's stock Lua runtime is
   not known to expose a global JSON library to the tuner contexts, so each file carries its own
   rather than assuming one exists (flagged `UNVERIFIED` at first use per file).
3. Defines one function per served declaration, returning a plain Lua table — never printing
   directly. A single invocation of one such function, JSON-encoded and printed once, is what
   satisfies contracts/nexus-protocol.md's "the Lua body emits a single JSON document between the
   sentinels": the dispatcher (`src/civsim_harness/capability/registry.py` and the Nexus client,
   T032/T036 — outside this directory's scope) is responsible for selecting the function for the
   requested `declaration_id`, calling it, and `print()`-ing its encoded result inside the
   `---BEGIN:<nonce>---` / `---END:<nonce>---` wrapper. Each file ends with an example-invocation
   comment showing that call so the contract is visible next to the library it dispatches into.
4. Marks any Civ VI Gameplay/Lua API call whose existence, name, or exact field shape is uncertain
   with an inline `-- UNVERIFIED:` comment rather than presenting a guess as fact.
5. Never reads unrevealed fog-of-war contents, opponent internal state, hidden AI intent, other
   civilizations' undisclosed research/civics, RNG state, or debug/provenance data — the source
   files are themselves part of the parity boundary, not just the catalog entries pointing at them.

## 4. The predicate language (T106's fixed symbol table)

Predicates (`availability_predicate`, `verification_predicate`) are restricted boolean expressions,
not arbitrary code. The grammar T106 must evaluate:

- Literals: numbers, strings, `true`/`false`, `null`, list literals `[a, b, c]`.
- Attribute access with `.` (e.g. `unit.movement_remaining`).
- Operators: `and`, `or`, `not`, `==`, `!=`, `<`, `<=`, `>`, `>=`, `in` (membership on a list or
  equality against a scalar).
- Arithmetic: binary `+` and `-` between two numeric operands only (e.g. `observed_turn_number +
  1`, as `turn.end_turn`'s own `verification_predicate` below uses). Permitted deliberately, and
  narrowly: the grammar's restriction exists to rule out arbitrary evaluation and function calls,
  not arithmetic as such, and integer/float addition or subtraction inside the AST-restricted
  evaluator (`src/civsim_harness/act/predicates.py`) introduces no such risk — both operands are
  already-evaluated plain data before the operator applies. Forbidding it would instead force the
  harness to precompute derived values like "next turn number" in Python and hand them in as
  bespoke bindings, pushing game semantics out of the declarative catalog. Do not "simplify" this
  back to disallowing arithmetic entirely; that regression previously made `turn.end_turn` — the
  single most important action in the harness — unable to ever verify as `applied`.
- No function calls, no assignment, no multiplication, no division, no arithmetic beyond binary
  `+`/`-` on numeric operands (a non-numeric operand, e.g. a string or an absent/`null` field, is a
  clean evaluation-time error, never silently coerced).

### Namespaces exposed to every predicate

| Namespace | Meaning | Bound when |
|---|---|---|
| `game.*` | Whole-game state a human sees regardless of selection | always |
| `player.*` | The local human player's own revealed resources/state | always |
| `unit.*` | The unit named by the action's own parameters (its subject) | unit actions |
| `city.*` | The city named by the action's own parameters (its subject) | city actions |
| `target` / `target.*` | The action's requested parameter payload (plot, id, choice, …) | actions with a target |
| `other_player.*` | The civilization named by a diplomacy action's `target` player id | diplomacy actions |
| `congress.*` | Whole-Congress state, plus the resolution named by a congress action's `target` | congress actions |
| `great_person.*` | The individual named by a great-people action's `target` | great-people actions |
| `spy.*` | The local player's own spy named by an espionage action's `target` | espionage actions |
| `prompt.*` | The currently open blocking prompt, if any | prompt-response actions |
| `camera.*` | Current camera state | camera actions/views |
| `observed_*` | Flat, harness-bound pre-execution snapshot of a same-named field | verification predicates only |

### Binding subject namespaces to `target`

`unit.*`, `city.*`, `other_player.*`, `congress.*`, `great_person.*`, and `spy.*` are all
**subject namespaces**: the harness resolves the action's own `target` (or, for a unit/city
action, the unit/city the human selected before issuing it) against the matching observation's
list — `units.state`'s `units[].unit_id`, `cities.state`'s `cities[].city_id`,
`diplomacy.state`'s `relations[].player_id`, `congress.state`'s
`active_resolutions[].resolution_id`, `great_people.state`'s
`recruitable_individuals[].individual_id`, `espionage.state`'s `spies[].unit_id` — and binds that
one matching entry's fields under the namespace. A `target` that matches nothing binds every
field in that namespace to its "absent" value (`exists: false`, or `false`/`null` for booleans
and scalars), which is what lets an availability predicate simply check e.g. `spy.exists and
spy.is_available` rather than needing a separate existence check bolted on.

### Field vocabulary

`game.*`
- `game.turn_number` (integer)
- `game.is_local_player_turn` (boolean)
- `game.has_blocking_prompt` (boolean)
- `game.active_prompt_type` (string or null) — a `prompts.yaml` declaration family, or null
- `game.current_screen` (string) — screen-identity probe result (`world`, `city_screen`,
  `diplomacy`, `congress`, `strategic`, `game_over`, `unknown`, …). The claimed vocabulary is
  `CIVSIM_KNOWN_SCREENS` in `lua/ingame/screens.lua`; §6 below lists the ids retired from it and
  the ones kept as documented gaps.
  - `strategic` — **documented gap, 2026-09-21**: the probe cannot currently report it.
    `<LuaContext ID="StrategicView" FileName="StrategicView"/>` (`base/assets/ui/ingame.xml:13`)
    carries no `Hidden` attribute and `strategicview.lua` is six lines of comment with no
    show/hide logic, so its `IsHidden()` is false at the ordinary world view too — watching it
    would make the probe answer `strategic` permanently. Strategic view is a world *render mode*,
    not a screen: `UI.GetWorldRenderView()`, which `lua/ingame/camera.lua` reads and
    `catalogs/observations/views.yaml` (`views.strategic`) already declares, and which this file's
    own `camera.mode` enum below covers. The id stays claimed here rather than being mapped to a
    state, because a mapping would be a fabrication. See
    `specs/002-civ-playing-harness/spikes/screens-unmapped-2026-09-21.md` ("`strategic` —
    unmappable").
  - `prompt.congress_vote` — **gap closed 2026-09-21** (`2026.09.8`). It was a documented gap:
    `WorldCongressPopup` is one context for every stage of a congress and the stage
    (`m_CurrentStage`/`m_CurrentPhase`) is private to that context's own Lua state, so
    `IsHidden()` cannot distinguish the vote from browsing the session, and `congress` already
    maps to that state — so the id is still **not** in the Lua's state map. It is answered
    directly instead, the way `prompt.diplomatic_approach` is: the stage is also expressed as the
    visibility and disabled state of the popup's own `NextButton` / `AcceptButton` / `PassButton`
    (`worldcongresspopup.lua:380-425`, `worldcongresspopup.xml:117-124`), which InGame can read.
    Reported only when one of those is visible **and** enabled; an open session with none of them
    live is the ordinary `congress` screen and does not block the turn (measured: an end turn
    advanced 56 → 57 with the session on screen). Casting an actual vote stays out of scope — see
    the comment above `prompts.congress_vote` in `catalogs/actions/prompts.yaml`.
  - `prompt.congress_intro` — the World Congress "Begin Voting" welcome card
    (`WorldCongressIntro`), added `2026.09.8` after it stalled a run as
    `UnknownScreenEncountered` at game turn 57. One option, `accept`.
  - `prompt.era_dedication` — Gathering Storm's era dedication chooser (`DedicationPopup`), added
    `2026.09.8` after it blocked play for 41 steps in gameplay block 20 while the probe answered
    `world`. Its options are the visible commemoration labels; the probe also reports
    `prompt_selections_allowed`, `prompt_selections_made` and `prompt_selected_options`.
- `game.is_waiting_for_other_players` (boolean)

`player.*` (the local human player only — never an opponent's)
- `player.gold` (number) — the top-bar gold balance (`player.yields.gold_balance`, T258)
- `player.faith` (number) — the top-bar faith balance (`player.yields.faith_balance`, T258)
- `player.science_per_turn`, `player.culture_per_turn`, `player.gold_per_turn` (net of
  maintenance, as the bar shows it), `player.faith_per_turn`, `player.tourism_per_turn`
  (numbers) — the top bar's per-turn rates (`player.yields`, T258)
- `player.current_research` (string or null)
- `player.current_civic` (string or null)
- `player.researchable_techs` (list<string>)
- `player.researchable_civics` (list<string>)
- `player.available_policies` (list<string>)
- `player.current_government` (string or null)
- `player.available_governments` (list<string>)
- `player.available_governors` (list<string>) — appointed governors not yet assigned to a city
- `player.pantheon_selected` (boolean)
- `player.religion_founded` (boolean)
- `player.available_beliefs` (list<string>) — beliefs currently choosable by the local player
- `player.diplomatic_favor` (number)
- `player.city_count` (number or null) — how many `cities.state` entries are the local player's
  own (`owner_is_local_player == true`); `null` when `cities.state` was not observed or its
  `cities` field is missing/not a list, never coerced to `0` (T313, `units.found_city`)

`unit.*` (subject unit of a unit action)
- `unit.exists` (boolean)
- `unit.is_selected` (boolean)
- `unit.owner_is_local_player` (boolean)
- `unit.plot` (plot-ref: `{x, y}`)
- `unit.movement_remaining` (number)
- `unit.reachable_plots` (list<plot-ref>)
- `unit.has_queued_orders` (boolean) — the unit is mid-operation, the badge a human sees on its
  flag. Replaced `unit.queued_path` on 2026-09-21: that field's guard called a method the unit
  does not have and its `destination` was hard-coded nil, so it could never answer anything.
- `unit.can_found_city` (boolean)
- `unit.available_promotions` (list<string>)
- `unit.charges_remaining` (number or null)
- `unit.available_builds` (list<string>) — the improvements the unit panel is offering on the
  tile this unit is standing on, with a live (not greyed-out) button; reported for the selected
  unit only, because that is the only unit whose panel a human is looking at
  (`units.state`, 2026-09-21). The panel's full build row, greyed entries included, is
  `units.state`'s `build_options` (improvement_type, name, disabled, is_recommended), which the
  agent reads but which has no predicate symbol — the same split `city.available_productions` and
  `cities.state`'s `production_options` already use

`city.*` (subject city of a city action)
- `city.exists` (boolean)
- `city.is_selected` (boolean) — produced by overlaying `cities.selection` (InGame,
  `UI.GetHeadSelectedCity()`) onto the `cities.state` entry (T255, 2026-09-21);
  `unit.is_selected` needs no overlay because `units.state` reports it itself
- `city.owner_is_local_player` (boolean)
- `city.production_queue` (list<string>) — the item in production, then the queued ones
- `city.available_productions` (list<string>) — the production panel's rows whose button is live
  and needs no further plot click; the panel's full list, greyed rows included, is
  `cities.state`'s `production_options` (name, kind, production_required, turns, disabled), which is
  read by the agent but has no predicate symbol
- `city.can_buy_with_gold` (boolean)
- `city.can_buy_with_faith` (boolean)
- `city.purchasable_with_gold` (list<string>)
- `city.purchasable_with_faith` (list<string>)

`target` / `target.*` (the action's own requested parameter — shape depends on the action)
- `target` — raw value, compared with `==`/`in` against the fields above (e.g.
  `target in unit.reachable_plots`)
- `target.is_revealed` (boolean) — plot targets only
- `target.is_valid` (boolean) — generic catch-all an action may declare against its own domain

`other_player.*` (the civilization named by a diplomacy action's `target` player id)
- `other_player.exists` (boolean)
- `other_player.has_met` (boolean)
- `other_player.diplomatic_state` (string or null)
- `other_player.has_delegation` (boolean)

`congress.*` (bound for congress actions; `congress.exists` reflects the resolution named by `target`)
- `congress.is_in_session` (boolean)
- `congress.exists` (boolean) — whether `target` names a currently active resolution

`great_person.*` (bound for great-people actions; reflects the individual named by `target`)
- `great_person.is_recruitable` (boolean)

`spy.*` (bound for espionage actions; reflects the local player's own spy named by `target`)
- `spy.exists` (boolean)
- `spy.is_available` (boolean)
- `spy.mission` (string or null)

`prompt.*`
- `prompt.type` (string)
- `prompt.is_active` (boolean)
- `prompt.options` (list<string>)

`camera.*`
- `camera.mode` (string: `world`|`strategic`|`city_screen`|`diplomacy`|`congress`)
- `camera.zoom` (number)
- `camera.target_plot` (plot-ref or null)
- `camera.target_is_revealed` (boolean)

`observed_*`
- A flat, harness-bound snapshot of a same-named field taken immediately before the action
  executed, available only to `verification_predicate`s that need a before/after comparison. Named
  `observed_<flattened field>`, e.g. `observed_turn_number` for `game.turn_number`. Only the
  snapshots an actual predicate in this catalog uses need exist; this catalog uses
  `observed_turn_number` (`catalogs/actions/turn.yaml`), `observed_diplomatic_favor` for
  `player.diplomatic_favor` (`catalogs/actions/congress.yaml`), `observed_charges_remaining`
  for `unit.charges_remaining` (`units.build_improvement`, `catalogs/actions/units.yaml`), and
  `observed_city_count` for `player.city_count` (`units.found_city`, `catalogs/actions/units.yaml`,
  T313). The snapshot's `unit` namespace is bound with no `target`, so it is the selected unit's
  counter — the same unit the order acts on.
  - **`observed_city_count` KNOWN GAP (2026-09-22, T313):** the predicate that reads it is landed,
    but the name is not yet registered in `act/verify.py`'s `_OBSERVED_FIELD_SOURCES` table — the
    one place a pre-execution `observed_*` snapshot is actually flattened from bindings into this
    namespace — because that file was out of scope for the task that landed the predicate. Until a
    one-line addition there registers it, `units.found_city`'s verification raises
    `PredicateEvaluationError` on this unbound name whenever `player.city_count` itself resolves
    (the ordinary case), which `act/verify.py` maps identically to `False`/`rejected` — never a
    fabricated `applied`, but also never a confirmed one, until the follow-up lands.

This vocabulary is deliberately small. A predicate that needs a symbol not listed here is a sign
the declaration belongs to a different observation, not a reason to widen the evaluator ad hoc.

## 5. Views and screening

- `screening_profile` on a `view` entry names a platform-scoped profile key in
  `catalogs/screening_profiles.yaml`, not inline detector logic.
- `camera_requirements.mode` must be one of `world`, `strategic`, `city_screen`, `diplomacy`,
  `congress` — the same enum as `camera.mode` above.

## 6. Retired claims

`uv run civsim store coverage` counts every action declaration and every claimed screen id as a
claim the harness must be able to demonstrate live. A claim that describes an interaction the game
does not have can never be demonstrated, so it is **withdrawn** here rather than left standing as a
permanent coverage warning. This list is the history: what was claimed, when it was retired, and
why. Retiring a claim is a catalog change like any other — bump `catalogs/VERSION`.

| Retired | Claim | Reason |
|---|---|---|
| 2026-09-21 (`2026.09.5`) | action `prompts.city_state_quest`, screen id `prompt.city_state_quest` | **The claim was wrong.** There is no city-state quest popup in the shipped UI at all: quests arrive as notifications (`NotificationTypes.CITYSTATE_QUEST_COMPLETED`, `base/assets/ui/panels/notificationpanel.lua:134`, `:253`) and are read in the browsable `CityStates` partial screen (`base/assets/ui/partialscreens/citystates.lua`, `<LuaContext ID="CityStates"/>` at `base/assets/ui/ingame.xml:50`), and nothing in Civ VI accepts or declines a quest. "Accept or decline it in that prompt" described an interaction that does not exist. |
| 2026-09-21 (`2026.09.5`) | action `prompts.religion_selection`, screen id `prompt.religion_selection` | **Duplicate overclaim.** Founding a religion is a notification that fires `LuaEvents.NotificationPanel_OpenReligionPanel()` (`base/assets/ui/panels/notificationpanel.lua:1322`) and opens the *same* `ReligionScreen` the launch bar opens for browsing (`base/assets/ui/religionscreen.lua:1426-1455`, `:1606-1609`; `base/assets/ui/launchbar.lua:142`). An open screen is not a blocking prompt, and the real interactions are already claimed as `religion.found_religion` / `religion.select_belief` / `religion.select_pantheon` in `catalogs/actions/religion.yaml`. |
| 2026-09-21 (`2026.09.6`) | action `prompts.natural_disaster`, screen id `prompt.natural_disaster` | **Added, measured live.** Gathering Storm's natural-disaster cinematic (`dlc/expansion2/ui/additions/naturaldisasterpopup.xml`, `/InGame/NaturalDisasterPopup`) blocked play and made the game refuse every save at game turn 42 while the probe reported `world`; a human dismisses it with its header `Close` button. Acknowledged through that button (real click at its own rectangle). |
| 2026-09-21 (`2026.09.8`) | action `prompts.era_dedication`, screen id `prompt.era_dedication` | **Added, measured live.** Gathering Storm's era dedication chooser (`dlc/expansion2/ui/additions/dedicationpopup.lua`, `/InGame/DedicationPopup`, reached through `<AddUserInterfaces>` at `dlc/expansion2/expansion2.modinfo:302-306`, which is why it appears in no shipped `ingame.xml`) blocked play for 41 steps in gameplay block 20 while the probe reported `world`; the model read it off the delivered frame and was refused at 40 of those steps. Answered by clicking a commemoration card and then `Confirm`, which the popup keeps disabled until the allowed number are ticked (`dedicationpopup.lua:200-203`, `:206-217`). Its X dequeues without dedicating anything (`:225-227`) and is a recorded fallback only. |
| 2026-09-21 (`2026.09.8`) | action `prompts.congress_intro`, screen id `prompt.congress_intro` | **Added, measured live.** The World Congress "Begin Voting" welcome card (`dlc/expansion2/ui/additions/worldcongressintro.xml:13`, `/InGame/WorldCongressIntro`) came up over the Classical-era review at game turn 57 and stalled the run as `UnknownScreenEncountered` — the state was watched but mapped to no id. Its one button runs `OnClose` (`worldcongressintro.lua:26-29`), which dequeues the card **and** raises `WorldCongressIntro_ShowWorldCongress` to open the session; the `DequeuePopup` fallback only does the first half, and says so. |
| 2026-09-22 | screen id `game_over` (no action) | **Added, measured live.** Persia was eliminated at game turn 67 and Firaxis's full-screen `EndGameMenu` (`base/assets/ui/ingame.xml:120`, `/InGame/EndGameMenu`) came up. It was on no watchlist, so the probe took its `#open == 0` branch and the production chain answered `screen = "world"`, `raw_screen_id = "InGame"`, **`recognized = true`**, `has_blocking_prompt = false` with the modal demonstrably up — the first time the detector claimed a board was clear while it was blocked, and a candidate contributor to today's `verification_failed` column. Mapped as a **terminal-state screen, not a prompt**: `game_over` carries no `prompt.*` prefix, so `has_blocking_prompt` stays false and no option is ever offered; the game-over path (`lua/ingame/game_over.lua`) owns it. `EndGameMenu` is placed **first** in `CIVSIM_SCREEN_WATCHLIST` so nothing open behind it can name the view instead. The underlying allowlist defect — a screen not on the watchlist reads as *no screen*, so the probe cannot tell "nothing is blocking" from "nothing I know about is blocking" — is **not** fixed by this entry and is written up in `specs/002-civ-playing-harness/spikes/gameplay-2026-09-22/block-04/screen-identity-false-negative.md`. |
| 2026-09-21 (`2026.09.12`) | action `prompts.ai_diplomatic_approach` — answer route and `verification_predicate` | **Corrected, from the store.** Three goal runs stuck 16 of 16 steps on this action after a reload onto a first-meeting greeting; across all 16 steps of `run-d0933ca8…` the recorded `prompt_options` were byte-identical, so the answer never reached the game. Two defects. (a) The answer went out as a host click on the conversation button, a path never demonstrated on a leader scene; it now resolves the choice's own `CHOICE_*` key from the game's `DiplomacySelections` rows (`DB.Query`, as `diplomacystatementsupport.lua:77-84` reads them) and makes the call its own handler makes — `AddResponse` for `CHOICE_POSITIVE`/`NEGATIVE`/`IGNORE`, `CloseSession` for `CHOICE_EXIT` (`diplomacyactionview.lua:489-534`) — with the click kept as a recorded fallback. (b) The predicate asked for the conversation to be gone, but a correct answer leaves it open with the leader's reply; it is now `target not in prompt.options`, true both when the leader has replied and when the session ended, false when nothing changed. Also: a first meeting's decline is `CHOICE_EXIT` under its own text, so matching the string "Goodbye" never caught it. |

Both retirements are evidenced, with file:line citations into Firaxis's shipped UI Lua, by
`specs/002-civ-playing-harness/spikes/screens-unmapped-2026-09-21.md`. The same write-up examined
`strategic` and `prompt.congress_vote`, and kept both as claims annotated as documented gaps.
`prompt.congress_vote`'s gap **closed** on 2026-09-21 (`2026.09.8`): live play showed the stage is
readable as the phase buttons' own visibility, without touching the private stage variable the
write-up had (correctly) ruled out — see §4's `game.current_screen` entry. `strategic` remains a
documented gap, because the interaction is real and only the probe's reach is missing; retiring it
would hide a gap the harness still owes.
