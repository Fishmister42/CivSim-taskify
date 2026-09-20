# Contract: Run Configuration

**Feature**: `002-civ-playing-harness` | **Schema version**: 1

A run is reproducible only if its definition is complete and exact before it starts. This format is
what the operator writes, what deliverable 4 will generate programmatically for branch and ablation
work, and what every run record references (FR-001).

## Format

```yaml
schema_version: 1
config_id: auto                        # generated if omitted

seed_set: shuffle-classic-2026q3       # optional; when set, must agree with the fields below
map_seed: "1849275663"
civilization: CIVILIZATION_ROME
leader: LEADER_TRAJAN
ruleset: RULESET_EXPANSION_2
mod_set:
  - { id: bbg-community-balance, version: "4.2.1" }

map_settings:
  map_type: MAPTYPE_CONTINENTS
  map_size: MAPSIZE_SMALL
  resources: RESOURCES_STANDARD
game_settings:
  game_speed: GAMESPEED_STANDARD
  starting_era: ERA_ANCIENT
  victory_types: [SCIENCE, CULTURE, DOMINATION, RELIGIOUS]
difficulty: DIFFICULTY_PRINCE
opponents:
  major_count: 5
  city_state_count: 10

stop_condition:
  type: turn_reached                   # turn_reached | game_outcome | operator_stop
  turn: 50

model_config:
  primary: { provider: openrouter, model: "anthropic/claude-opus-5" }
  fallbacks:
    - { provider: openrouter, model: "anthropic/claude-sonnet-5" }
  request_params: { temperature: 0.7 }

guidance_set: GUIDEBOOK.md@a1b2c3d     # optional; content-addressed at load

no_progress_step_limit: 8              # consecutive rejected/no-change steps that end a turn
recovery_attempt_limit: 3
min_free_disk_gb: 25
```

**There is no turn time budget, and adding one is a regression.** A turn runs for as many decision
steps and as long as the agent needs (FR-008, FR-014). `no_progress_step_limit` is the only thing
that ends a turn the agent has not chosen to end, and it counts *consecutive steps that accomplished
nothing* — rejected, or verified as changing no game state — resetting to zero on any step that
changed the board. A 500-step productive turn never approaches it; an agent looping on an illegal
order reaches it in eight steps. Sizing it is a real trade: too low truncates an agent working
through genuinely constrained options, too high burns model calls on a stuck one.

`min_free_disk_gb` is a precondition, not a cleanup trigger. Below it the run halts in a recorded
state; nothing is deleted to make room, because no save is eligible for removal until its run is
archived (FR-036, research R17).

## Validation — all at preflight, never mid-run

| # | Rule | Requirement |
|---|---|---|
| **V1** | Every required field present and non-null. A partially specified run does not start | FR-001 |
| **V2** | Every configured element applies to the game **exactly**. Seed unavailable, ruleset or mod set not active, setting unsupported ⇒ the run fails before turn 1 with the mismatch recorded | FR-002, US1 §2 |
| **V3** | When `seed_set` is set, `civilization`, `leader`, `ruleset`, and `mod_set` must match the set; a mismatch is an error, not an override | FR-031 |
| **V4** | Every model in `primary + fallbacks` passes the image and context preflight | FR-039 |
| **V5** | Every observation and action the run could use resolves to a parity declaration | FR-023, SC-006 |
| **V6** | `guidance_set`, if set, resolves to content and is recorded by hash with the run | FR-021 |
| **V7** | The store answers `ping()` | FR-051, edge case: store unreachable |
| **V8** | No run is already active on this client or this run identity | FR-006 |
| **V9** | **No credentials.** The configuration carries none; a key-shaped value here is a validation error | FR-043 |
| **V10** | **Game build matches.** The client's composite build — **platform and version** — equals the seed set's `game_build`, or a recorded `BuildAcceptance` covers that exact transition. Otherwise the run fails before turn 1 | FR-002, FR-031, R20 |
| **V13** | **Host is supported.** The resolved platform support tier is `validated` or `supported`; an `unsupported` host is rejected, naming the missing capability, since FR-007 forbids a turn without a verified quicksave | FR-007, R19 |
| **V11** | Free disk space is at or above `min_free_disk_gb`, and the estimated footprint of the run's saves and captures fits within it | Edge case: disk fills; R17 |
| **V12** | `no_progress_step_limit` ≥ 1. There is no upper bound and no default that silently caps a turn | FR-014 |

**V2 is the one that matters most and is easiest to weaken.** The spec is explicit that an
approximate run is worse than no run — a silently different game produces data that looks
comparable and is not. Preparation reads back the actual game setup through declared observations
and compares field by field; any difference aborts with the mismatch recorded, rather than
proceeding with a warning.

**Phase-dependent settings are verified in-game only, never from the front end** (T218 live
evidence, T250 ruling). The same getter can name two different facts by game phase:
`Modding.GetActiveMods()` returned 0 entries at the front end and the real 22 in-game on the same
host, and the major-opponent count decomposed 6-vs-16 across the same boundary
(`GetAIPlayerCount()` in-game counts city-states, Free Cities, and Barbarians too). A front-end
read is therefore not a weaker version of the fact — it is a different fact — and two
differently-modded hosts both recording 0 mods at preparation time would be falsely judged
comparable (Principle IV). `mod_set` and `opponents.major_count` accordingly carry an
in-game-only phase declaration: a front-end read-back never dispatches them (they are reported
*phase-deferred*, a distinct bucket never conflated with a missing getter, and fail closed if
compared anyway), and their V2 comparison runs at the post-load, pre-turn-1 read-back — the run's
first in-game moment, which is where the composition root's single V2 pass already executes. A
genuine in-game mismatch on these fields still fails the run before turn 1, exactly like any
other field. `map_settings.resources` is the inverse case: live-confirmed unobservable in
*either* phase (every candidate getter returns nil), it is recorded on the run as accepted on
seed-set agreement alone (`v2_unobservable_fields`, distinct from the no-getter
`v2_unverified_fields`) rather than failing the run over a value nothing can read.

## Branch configuration

A branch is a run configuration plus a lineage reference (FR-033):

```yaml
branch_from:
  run_id: run_01J8X...
  turn: 23
```

When `branch_from` is present, the seed, civilization, ruleset, mod set, map, and game settings are
**inherited from the parent and may not be overridden** — the branch starts from the parent's save,
so restating them differently would describe a position that does not exist. What a branch may
change is `model_config`, `guidance_set`, `stop_condition`, `no_progress_step_limit`,
`recovery_attempt_limit`, and `min_free_disk_gb`. This is exactly the set deliverable 4's ablation
work needs to vary, and no more.

A branch may only be taken from a save point that still exists. Branching from a run whose saves
were removed after archival is rejected with the missing save named, never silently retargeted to a
nearby turn (FR-036).

## Seed set definition

```yaml
schema_version: 1
name: shuffle-classic-2026q3
civilization: CIVILIZATION_ROME
leader: LEADER_TRAJAN
ruleset: RULESET_EXPANSION_2
mod_set: [{ id: bbg-community-balance, version: "4.2.1" }]
game_build: "win/1.0.12.9"             # composite: platform / version
seeds: ["1849275663", "2273910055", ...]

accepted_build_changes: []             # appended to only by an explicit operator action
```

`civilization`, `leader`, `ruleset`, and `mod_set` are immutable after creation. Changing one
creates a new seed set — otherwise runs inside a set stop being comparable, which is the only
reason the set exists (FR-031, spec Assumptions).

### The build pin (FR-002, FR-031, research R18)

`game_build` is recorded when the set is created and checked against the live client at every run's
preflight. **A mismatch fails the run before turn 1 by default**, because Civ VI patches itself and a
balance change mid-set makes halves of the set incomparable while the seed, civilization, ruleset,
and mod set all still match — a silent corruption invisible in the data afterwards.

An operator who judges the patch irrelevant to their experiment may accept it for that set:

```yaml
accepted_build_changes:
  - acceptance_id: acc_01J9...
    from_build: "win/1.0.12.9"
    to_build:   "win/1.0.12.11"
    accepted_by: researcher
    accepted_at: 2026-09-19T18:04:00Z
    reason: "Patch notes affect only multiplayer matchmaking."
```

Three properties make this an audit trail rather than a mute button:

1. **Acceptance is scoped to one set and one composite transition.** There is no global override,
   which would be indistinguishable from not having the check — and because the identity is
   `platform/version`, accepting a version bump never implicitly accepts a platform change (R20).
2. **Every dependent run records `game_build` and `game_build_acceptance_ref`**, so a run is
   auditable from its own record without reconstructing the set's history.
3. **A set with a non-empty `accepted_build_changes` is not uniform** and must report as such. Its
   runs partition by build — which is the property that stops a mixed set from reading like a clean
   one.
