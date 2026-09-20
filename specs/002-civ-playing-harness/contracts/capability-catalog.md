# Contract: Capability Catalog (the parity boundary)

**Feature**: `002-civ-playing-harness` | **Schema version**: 1

The catalog is the human-parity boundary in the form a person can read and audit. Code binds to
catalog entries; it never reaches the game directly. A capability that is not declared here has no
path into the agent's context and no path into the game.

Files live under `catalogs/`, are loaded and validated at startup, hashed, and recorded on every run
(FR-022).

```text
catalogs/
├── VERSION                  # Single line, e.g. "2026.09.1"
├── observations/*.yaml      # kind: observation
│   └── views.yaml           # kind: view  (visual observations)
└── actions/*.yaml           # kind: action
```

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
screening_profile: default             # which detector profile applies (R7)
```

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
```

**The bespoke rule** (FR-028, SC-020): `path: bespoke` with an empty or missing `firetuner_gap`
fails catalog load. The gap statement must say what the capability does, what it reads or writes,
and why Firetuner could not do it. This is why SC-020's "100 % coverage" is checkable by loading the
catalog rather than by reviewing code.

Example of a compliant bespoke entry:

```yaml
capability_id: saves.save_game
path: bespoke
implementation_ref: src/civsim_harness/saves/dialog_driver.py
reads: [save directory listing]
writes: [named .Civ6Save file]
firetuner_gap: >
  No save-to-named-file call is reachable from the GameCore_Tuner or InGame Lua contexts;
  enumeration output recorded in research R5. Named per-turn saves are required by FR-007,
  so the in-client Save Game dialog is driven with synthetic input and the result verified
  on the filesystem before the turn proceeds.
```

## Load-time validation (all failures abort startup, not turn 1)

1. Every declaration has a non-empty `parity_basis`.
2. Every `capability_id` resolves; every `path: bespoke` has a non-empty `firetuner_gap`.
3. `declaration_id` values are unique across all files.
4. Action entries have both `availability_predicate` and `verification_predicate`.
5. Observation and view entries have a valid `output_schema`.
6. Predicates reference only symbols the evaluator exposes — no arbitrary evaluation.
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
