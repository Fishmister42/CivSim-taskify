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

- `lua/gamecore/*.lua` runs in `GameCore_Tuner` — read-only, faster, cannot issue orders or reach
  UI-bound surfaces.
- `lua/ingame/*.lua` runs in `InGame` — the only context that can issue orders, change production,
  or reach UI-bound surfaces (diplomacy, World Congress, screens).
- A declaration's `context` field must match the Lua file that backs its `capability_id`.

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
  `diplomacy`, `congress`, `strategic`, `unknown`, …). The claimed vocabulary is
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
  - `prompt.congress_vote` — **documented gap, 2026-09-21**: the probe cannot currently report it.
    `WorldCongressPopup` is one context for every stage of a congress and the stage
    (`m_CurrentStage`/`m_CurrentPhase`) is private to that context's own Lua state, so
    `IsHidden()` cannot distinguish the forced vote from browsing the session; `congress` already
    maps to that state. See the same write-up ("`prompt.congress_vote` — unmappable") and the
    comment above `prompts.congress_vote` in `catalogs/actions/prompts.yaml`.
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

`unit.*` (subject unit of a unit action)
- `unit.exists` (boolean)
- `unit.is_selected` (boolean)
- `unit.owner_is_local_player` (boolean)
- `unit.plot` (plot-ref: `{x, y}`)
- `unit.movement_remaining` (number)
- `unit.reachable_plots` (list<plot-ref>)
- `unit.queued_path` (object or null: `{destination: plot-ref}`)
- `unit.can_found_city` (boolean)
- `unit.available_promotions` (list<string>)
- `unit.charges_remaining` (number or null)

`city.*` (subject city of a city action)
- `city.exists` (boolean)
- `city.is_selected` (boolean) — produced by overlaying `cities.selection` (InGame,
  `UI.GetHeadSelectedCity()`) onto the `cities.state` entry, since `cities.state` runs in
  GameCore_Tuner where `UI` does not exist (T255, 2026-09-21); `unit.is_selected` needs no
  overlay because `units.state` runs InGame and reports it itself
- `city.owner_is_local_player` (boolean)
- `city.production_queue` (list<string>)
- `city.available_productions` (list<string>)
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
  `observed_turn_number` (`catalogs/actions/turn.yaml`) and `observed_diplomatic_favor` for
  `player.diplomatic_favor` (`catalogs/actions/congress.yaml`).

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

Both retirements are evidenced, with file:line citations into Firaxis's shipped UI Lua, by
`specs/002-civ-playing-harness/spikes/screens-unmapped-2026-09-21.md`. The same write-up examined
`strategic` and `prompt.congress_vote`: those two are **kept** as claims and annotated as
documented gaps (§4's `game.current_screen` entry), because the interactions are real and only the
probe's reach is missing — retiring them would hide a gap the harness still owes.
