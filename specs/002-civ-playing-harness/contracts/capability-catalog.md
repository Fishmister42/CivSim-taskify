# Contract: Capability Catalog (the parity boundary)

**Feature**: `002-civ-playing-harness` | **Schema version**: 1

The catalog is the human-parity boundary in the form a person can read and audit. Code binds to
catalog entries; it never reaches the game directly. A capability that is not declared here has no
path into the agent's context and no path into the game.

Files live under `catalogs/`, are loaded and validated at startup, hashed, and recorded on every run
(FR-022).

```text
catalogs/
├── VERSION                  # Single line, e.g. "2026.09.14"
├── capabilities.yaml        # every IntegrationCapability (see the schema below)
├── screening_profiles.yaml  # the per-platform detector profiles views resolve against
├── observations/*.yaml      # kind: observation
│   └── views.yaml           # kind: view  (visual observations)
└── actions/*.yaml           # kind: action
```

*(Corrected 2026-09-22: the tree omitted both `capabilities.yaml` — where every
`IntegrationCapability` this document specifies actually lives, per
`capability/loader.py::_discover_capability_files`, which reads `capabilities.yaml` and/or a
`capabilities/` directory — and `screening_profiles.yaml`, which `screening_profile` below
resolves against. A reader looking for the capability entries found only declaration files.)*

## Declaration schema

```yaml
declaration_id: units.move_to          # stable, dotted, unique across the catalog
kind: action                           # observation | view | action
summary: >
  Order a selected unit to move to a target plot.
parity_basis: >                        # REQUIRED, non-empty — the in-client human path
  Select the unit, then left-click a reachable plot within its movement range.
context: InGame                        # GameCore_Tuner | InGame
capability_id: units.orders            # implementing IntegrationCapability
availability_predicate: >              # actions: when a human could do this right now
  unit.is_selected and unit.movement_remaining > 0 and target in unit.reachable_plots
verification_predicate: >              # actions: how the effect is confirmed
  unit.plot == target or unit.queued_path.destination == target
output_schema:                         # observations/views: shape of the produced value
  type: object
  properties: { ... }
introduced_in_version: "2026.09.1"
```

### Field rules

| Field | Required for | Rule |
|---|---|---|
| `declaration_id` | all | Unique; immutable within a version |
| `kind` | all | One of `observation`, `view`, `action` |
| `parity_basis` | all | **Non-empty.** Load fails otherwise — this is the enforcement point for FR-016/FR-017 |
| `context` | all | Must match what the implementation actually needs; an observation requiring `InGame` must say so |
| `capability_id` | all | Must resolve to a declared `IntegrationCapability` |
| `availability_predicate` | actions | Evaluated before execution; false ⇒ reject with `unavailable_to_human_now` (FR-017) |
| `verification_predicate` | actions | Evaluated after execution; determines `applied` / `partially_applied` / `rejected` (FR-011) |
| `output_schema` | observations, views | JSON Schema; the produced value is validated against it |
| `target_kind` | actions (optional) | T256, 2026-09-21: what the single `parameters.target` names — `none`, `plot`, `unit_id`, `city_id`, `player_id`, `resolution_id`, `individual_id`, `spy_id`, `name`, `option` or `number`. Rendered to the agent as one concrete example per action (`agent/context.py`); additive, an action without it renders as before. Forbidden on observations and views |
| `target_hint` | actions (optional, requires `target_kind`) | One short human-facing sentence after the example, e.g. "a destination plot from the selected unit's reachable_plots -- the plot, never the unit's id" |

**Predicate grammar** (`availability_predicate`, `verification_predicate`; full namespace/field
vocabulary in `catalogs/README.md` §4, which this must agree with): literals (numbers, strings,
`true`/`false`, `null`, list literals), `.`-attribute access, the operators `and`/`or`/`not`/
`==`/`!=`/`<`/`<=`/`>`/`>=`/`in`, and binary `+`/`-` between two numeric operands (e.g.
`observed_turn_number + 1`, used by `end_turn` below). No function calls, no multiplication or
division, no arithmetic beyond binary `+`/`-` on numeric operands, no assignment — this is what
load-time validation rule 6 below enforces.

## `view` entries (visual observations)

A `view` declares a camera state and what it shows. Because images can carry whatever is on screen,
views carry extra fields:

```yaml
declaration_id: views.city_screen
kind: view
parity_basis: Click a city banner to open its city screen and look at it.
camera_requirements:
  mode: city_screen                    # world | strategic | city_screen | diplomacy | congress
  zoom_range: [min, max]               # must be within what the standard UI allows
  target_must_be_revealed: true        # FR-026 — cannot point at unrevealed plots
screening_profile: platform            # HOW the detector profile resolves — see below (R7)
```

**`screening_profile` names a resolution *rule*, not a profile — and this is the field most likely
to mislead you.** `parity/screening.py::resolve_screening_profile` looks the declared value up in
`catalogs/screening_profiles.yaml` **only when it is literally `default`**, which always means the
strictest profile regardless of host. **Any other value resolves by the running host platform**:
`profiles[<host platform>]`, falling back to `profiles.default` when that platform has no entry.
The declared string itself is never used as a key. So:

- `screening_profile: default` ⇒ always `profiles.default`, the strictest profile.
- `screening_profile: platform` ⇒ the host's own profile. This is what all five shipped views
  declare (`catalogs/observations/views.yaml`), and `platform` is deliberately **not** a key in
  `screening_profiles.yaml` (its keys are `windows`, `macos`, `linux`, `default`) — it is a word
  meaning "resolve by host", not a lookup.
- `screening_profile: windows` ⇒ **silently the *host's* profile, not the Windows one.** On a Linux
  host this resolves to `profiles.linux`, with no error and no warning. There is no way to pin a
  view to a foreign platform's profile, and asking for one is not rejected.

*(Corrected 2026-09-22: this field was documented as `screening_profile: default   # which detector
profile applies`, as if the value named a profile. It does not. The `windows` case above is the one
that bites: an author writes the name of the profile they want, gets a different profile, and
nothing anywhere says so. `screening_profiles.yaml`'s own `resolution_rule` block states the real
rule; this contract now matches it.)*

A capture may be shown to the agent only if it carries a `view_declaration_id` resolving here, its
camera state satisfies `camera_requirements`, and screening passed (FR-024 – FR-026). Captures are
per decision step, so a view is resolved and screened afresh at each step rather than once per turn
(FR-015).

## `end_turn` — the action that used to be control flow

FR-008 makes ending the turn a decision the agent issues, not something the harness does when it
judges the turn finished. That makes it an action like any other, and it needs a declaration:

```yaml
declaration_id: turn.end_turn
kind: action
summary: End the current turn and pass play to the other civilizations.
parity_basis: >
  Click the end-turn button in the lower-right action panel, or press its hotkey.
context: InGame
capability_id: turn.control
availability_predicate: >
  game.is_local_player_turn and not game.has_blocking_prompt
verification_predicate: >
  game.turn_number == observed_turn_number + 1 or game.is_waiting_for_other_players
introduced_in_version: "2026.09.1"
```

Two details are doing real work here. The availability predicate refuses an end-turn while a
blocking prompt is up, because the game itself would refuse it and FR-010 requires the prompt to be
answered as a decision first. And the verification predicate is what distinguishes a turn that
actually ended from one where the click was swallowed — without it, the harness would record the
turn as ended and then observe a board that had not moved.

**A turn ended by the no-progress backstop produces no `end_turn` decision.** The backstop is
recorded on the turn itself (`outcome: ended_on_no_progress`), not as a synthesised agent action —
fabricating one would be exactly the defaulted-move-recorded-as-a-decision that FR-042 and SC-012
forbid, and would destroy the distinguishability SC-022 requires.

## IntegrationCapability schema

```yaml
capability_id: units.orders
path: firetuner                        # firetuner | bespoke
implementation_ref: lua/ingame/unit_orders.lua
reads: [unit state, plot reachability]
writes: [unit orders]
firetuner_gap: null                    # REQUIRED and non-empty when path == bespoke
parity_basis: null                     # OPTIONAL, non-empty when present — see below
```

**The bespoke rule** (FR-028, SC-020): `path: bespoke` with an empty or missing `firetuner_gap`
fails catalog load. The gap statement must say what the capability does, what it reads or writes,
and why Firetuner could not do it. This is why SC-020's "100 % coverage" is checkable by loading the
catalog rather than by reviewing code.

**The rule also runs the other way, and load fails just as hard** (`models/catalog.py`'s
`_bespoke_requires_gap`): `path: firetuner` with a `firetuner_gap` that is **not** `null` raises
`firetuner_gap must be absent (null) when path == firetuner`. A `firetuner` entry annotated
`firetuner_gap: "n/a"`, `""` or "none needed" aborts startup, not turn 1. Both directions are one
validator, because a gap statement on a Firetuner path is a contradiction: there is no gap.
*(Documented 2026-09-22 — this contract previously published only the bespoke direction, at the
list item below and in the paragraph above, so an author writing a well-meaning `"n/a"` met an
abort this document never warned of.)*

**`parity_basis` is optional here.** Every `ParityDeclaration` that resolves to this capability
already carries its own required, non-empty `parity_basis`, so omitting the field on the capability
means it is inherited from those declarations — this is the normal case, and is why none of the
catalog's entries restate it. Set it only to override or clarify at the capability level; if
present, it must be non-empty, same as the declaration-level field.

Example of a compliant bespoke entry — **hypothetical, and deliberately so.** It is written against
a capability that does not exist, so it cannot drift into describing a module that was never built:

```yaml
capability_id: example.hypothetical_capability   # illustrative only — not a shipped entry
path: bespoke
implementation_ref: src/civsim_harness/<subpackage>/<module>.py
reads: [what the capability reads]
writes: [what the capability writes]
firetuner_gap: >
  What was probed, in which context, and what it returned; why no Firetuner-reachable call
  satisfies the requirement; and what this capability does instead. A gap statement names a
  measured negative result, not an expectation of one.
```

**For what is actually declared bespoke today, read `catalogs/capabilities.yaml` and grep for
`path: bespoke`** — the catalog is the authority, and this contract deliberately does not restate
its contents.

*(Corrected 2026-09-22: this example previously showed `saves.save_game` as `path: bespoke` with
`implementation_ref: src/civsim_harness/saves/dialog_driver.py` and a `firetuner_gap` citing
"research R5". Every part of that was counterfactual. **R5 returned Outcome A**: the T077 spike ran
against a live Linux client on 2026-09-20 and found `Network.SaveGame(gameFile)` in the `InGame`
context writes a real `.Civ6Save` — four consecutive calls, four valid, size-stable files
(`spikes/r5-save-path.md`). R5 was a spike with three outcomes, not a finding of absence
(`research.md` R5). Accordingly the shipped catalog declares `saves.save_game` as `path: firetuner`
/ `lua/ingame/save_game.lua` / `firetuner_gap: null`; `src/civsim_harness/saves/` contains no
`dialog_driver.py` and never did; and `catalogs/actions/saves.yaml` records that under Principle II
that positive result makes the dialog driver **"forbidden, not merely unnecessary"**. The contract's
only worked example of the bespoke rule was pointing readers at a forbidden, non-existent module.)*

## Load-time validation (all failures abort startup, not turn 1)

1. Every declaration has a non-empty `parity_basis`.
2. Every `capability_id` resolves; every `path: bespoke` has a non-empty `firetuner_gap`, **and
   every `path: firetuner` has `firetuner_gap: null`** — a non-null gap on a Firetuner path aborts
   load just as a missing one on a bespoke path does (`models/catalog.py`).
3. `declaration_id` values are unique across all files.
4. Action entries have both `availability_predicate` and `verification_predicate`.
5. Observation and view entries have a valid `output_schema`.
6. Predicates reference only symbols the evaluator exposes — no arbitrary evaluation, no function
   calls; arithmetic is limited to binary `+`/`-` between numeric operands (see "Predicate grammar"
   above and `catalogs/README.md` §4 — the two must state the same grammar).
7. `catalogs/VERSION` is present and the computed content hash is recorded.

## Run-time enforcement

| Point | Behaviour | Requirement |
|---|---|---|
| Preflight | Every capability the run could use resolves to a declaration, or the run does not start | FR-023, SC-006 |
| Context assembly | The assembler accepts capability results only; there is no raw-Lua input type. It runs **once per decision step**, with no incremental-update path that would skip the filter | FR-018, FR-008 |
| Forbidden-field guard | Assembled context asserted against the red-team list before it leaves the harness — every step, not every turn | FR-019, FR-020, SC-008 |
| Action dispatch | Unregistered or unavailable action ⇒ rejected and recorded with reason; never performed. A rejection also increments the turn's no-progress counter | FR-017, FR-014 |
| Image entry | Screening passes and the view resolves, or the image is withheld. Per step; an earlier step's image may not be substituted | FR-025, FR-015, SC-009 |
| Turn ending | Only `turn.end_turn` ends a turn by decision. The backstop ends it without one, recorded on the turn | FR-008, SC-022 |
| Run record | Catalog version + content hash written to the run | FR-022, SC-007 |

**Per-step, not per-turn, is the point worth internalising.** Every enforcement above used to run
once a turn and now runs once a step, which for a long turn means hundreds of times. That is not
overhead to be optimised away by caching the filtered context between steps — the whole reason the
loop exists is that the board changed, so a cached context would be showing the agent a board that
predates its own last action.

## Adding a capability mid-project

Required by the spec's edge case on capabilities added later: a new capability enters the catalog
with its parity declaration **before first use** (FR-029), which bumps `catalogs/VERSION`. Runs
before and after remain distinguishable by the version and hash recorded on each (FR-022). A
capability used before it is declared cannot occur — it has no code path.
