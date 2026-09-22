# Breadth map — what is actually in reach, and why the rest is not

Maintained by the continuous-play runner lane, 2026-09-22. Current as of **game turn 30**, store
`civsim-match-store.db`, coverage **16 of 41 actions demonstrated live / 30 of 41 ever attempted**.

> **⚠️ CAVEAT ADDED 2026-09-22 (headless, T322) — the headline number is UNCHANGED, the evidence
> under one of its entries is not.** `prompts.ai_diplomatic_approach` verified with
> `target not in prompt.options`, a negative over a container satisfied by the option list simply
> emptying. Replayed against the corrected predicate, **7 of its 14 store-wide `applied` records
> fall** (they confirmed against `prompt_options: []` with the engine's `raw_screen_id` unchanged
> at `DiplomacyActionView`). **It still qualifies as demonstrated live on the surviving 7**, two of
> which are block 37's, so the 16 does not move. Recorded here because "demonstrated live" is a
> *threshold* metric — one `APPLIED` qualifies (`store/coverage.py:350`) — and a threshold metric
> hides exactly this kind of change. **The general risk this raises for every other row: the count
> is only as good as each action's verification predicate, and 20 more shipped predicates carry the
> same bare-negative shape** (`capability/verification_shape.py::KNOWN_NEGATIVE_VERIFICATIONS`).
> Sharpest of those is `prompts.era_dedication`, also in the "demonstrated live" list, whose own
> catalog note records that the chooser's X dedicates nothing.

**Why this file exists.** "Never attempted" is one label covering **three different causes with
three different fixes**. A zero in the attempts column beside a zero in the refusals column reads
as "nothing to see here" and can equally mean *structurally impossible*. That is absence and
unobservability sharing a representation — in the artefact we use to measure ourselves.

---

## Cause A — the availability predicate can never be true

The action cannot be drawn on any board, in any run, by any provider. **A fix to the sampler or
more play time will not move these.**

| action | predicate | why it can never hold |
|---|---|---|
| `research.set_civic` | `target in player.researchable_civics` | `research.state.researchable_civics` reads `[]` on **six** probes (turns 10, 14, 14, 15, 19, 22) while `researchable_techs` is populated at each and the game's own World Tracker shows civics progressing (Craftsmanship → Foreign Trade). **Symptom matches the `available_promotions` body gap fixed at `6606d4b`; root cause NOT established — this lane did not read the Lua body.** |

**Watch for more of these.** The signature is: a list empty on **every** record, a populated
sibling in the same body, and a declaration whose availability depends on it.

---

## Cause B — the enabler is starved by the coverage sampler

The predicate *can* be true, but only after another action runs first — and `--provider-policy
coverage` systematically avoids that action **because it is well covered**.

**Mechanism, verified from the catalog text rather than inferred from behaviour:**

```
cities.set_production   availability: city.is_selected and city.owner_is_local_player
                                      and target in city.available_productions
units.build_improvement availability: unit.is_selected and unit.owner_is_local_player
                                      and unit.charges_remaining > 0
                                      and target in unit.available_builds
```

Both are gated behind a **selection** action.

| enabler | applied | sampler behaviour | dependent action | outcome |
|---|---|---|---|---|
| `cities.select` | **39** (highest in the catalog) | **avoided** — 0 draws in block 17's 43 steps | `cities.set_production` | 0 draws; capital idle turns 14 → 28 |
| `units.select` | **5** (low) | **drawn** — 5 draws in the same block | `units.move_to` | 3 draws, 2 applied |

**Same structure, opposite coverage, opposite outcome — a controlled comparison from a single
block.** A coverage-seeking sampler is blind to prerequisite chains: it under-draws the common
enabling action *precisely because it is common*, and thereby starves the rare action that
depends on it.

Affected, or plausibly so: `cities.set_production`, `cities.purchase_with_gold`,
`cities.purchase_with_faith`, `units.build_improvement`, `units.promote`.

**These are reachable by a model block** — block 09 (Sonnet 5) chained `cities.select` at t1/s3
into `cities.set_production` at t1/s4, which no stochastic block has ever done.

---

## Cause C — genuinely not reachable at this point in the game

Nothing is wrong. The game has not produced the precondition yet.

| action | needs | status at turn 30 |
|---|---|---|
| `congress.cast_vote` | a World Congress in session | not until the Medieval era |
| `espionage.assign_mission` | a spy | `espionage.state.spies` = `[]` |
| `great_people.recruit` | great-person points | all classes at 0 points, 0/turn |
| `policies.assign_governor` | a governor title | `available_governors` = `[]` |
| `policies.change_government` | a second government | `available_governments` = `[]` |
| `religion.select_belief` | a religion founded | `religion_founded: false` |
| `units.promote` | a unit with earned promotions | `available_promotions` = `[]` on both units |

**`units.promote` is the nearest of these.** A red-bordered unit with a `!` marker was two tiles
from the capital at turn 22 (operator screenshot, no claim attached); combat would produce a
promotion and make it drawable.

---

## Demonstrated live (16)

`cities.select` · `cities.set_production` · `prompts.ai_diplomatic_approach` ·
`prompts.boost_unlocked` · `prompts.congress_intro` · `prompts.era_dedication` ·
`prompts.era_transition` · `prompts.natural_disaster` · `prompts.tech_civic_completed` ·
`religion.select_pantheon` · `research.set_tech` · `saves.save_game` · `turn.end_turn` ·
`units.found_city` · `units.move_to` · `units.select`

## Attempted, never applied (14) — and they are not one group either

- **Proven to have taken effect, scored `verification_failed`:** `units.found_city` (the city
  exists — two independent channels), `cities.set_production` (the queue changed, entry 15).
  *These are scoring defects.*
- **Proven NOT to have taken effect, scored `verification_failed`:** `policies.slot_policy` —
  8 dispatches, all four cards still in `available_policies` at turn 22, and no yield movement
  that a slotted card would cause. *This is a different defect and a fix for the first group
  will not touch it.*
- **Blocked by a head, not by the game:** `camera.move` / `camera.zoom` / `camera.set_view_mode`
  — 0 applied of 41+ attempts, and **cannot move while the live worktree sits at `c211605`**,
  which predates the camera binding work. Untestable by this lane today.
- **Needs a met civilisation:** `diplomacy.declare_war`, `diplomacy.make_peace`,
  `diplomacy.send_delegation` — all five major-civ relations read `has_met: false` at turn 22.
  *A city-state (Kong) HAS been met and does not appear in `diplomacy.state` at all; whether
  that filtering is deliberate is not established, and the body cannot say which it means.*
- **Needs its prompt to appear:** `prompts.congress_vote`, `prompts.declare_war_response`,
  `prompts.great_person_selection`, `prompts.great_work_created`, `prompts.pantheon_selection`,
  `prompts.unit_promotion`, `religion.found_religion`.

---

## Screens

`NaturalWonderPopup` — **on `CIVSIM_SCREEN_WATCHLIST` (screens.lua:307), absent from
`CIVSIM_SCREEN_ID_BY_STATE`.** Encountered at turn 30 (`NATURAL WONDER DISCOVERED — Chocolate
Hills`). The probe answered `screen: unknown`, `recognized: false`, and the run **stalled visibly
with the raw id** — FR-049 working. **This is the control case for `EndGameMenu`, which was on no
watchlist and answered `world`, `recognized: true` with a defeat modal up. Same defect class,
opposite outcome, one variable: watchlist membership.**

**Caveat carried with it:** the probe reported `has_blocking_prompt: false` with a full-screen
modal up — correct by design, since only `prompt.*` ids set that field, but **a consumer reading
that field alone still gets a confident false all-clear.** The honesty lives entirely in
`recognized: false`.
