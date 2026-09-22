# The `verification_failed` column: what it actually measured

**Audit date** 2026-09-22 · **Store** `civsim-match-store.db` (store_id `99b987a632d544aead0ba359df5928a8`,
schema 1.1) · **Read-only throughout.**

## The question

The harness records each attempted action with an outcome. Two of those values matter here:

- `unavailable_to_human_now` — a **pre-dispatch** refusal. The action was never sent. This vocabulary
  is sound and is not in scope.
- `verification_failed` — the action **reached the game** and the harness could not confirm its effect
  inside its bound.

`ACTION_CONFIRM_TIMEOUT_S = 4.0` (`src/civsim_harness/run/decision_loop.py:237`, one non-end-turn call
site at `:959`) governs every in-turn order, while `END_TURN_CONFIRM_TIMEOUT_S = 200.0` (`:233`) was
raised on measured evidence that a turn advance lands 11.5–155 s after dispatch. So `verification_failed`
may mean *the action worked and we looked too early*.

**The question this audit answers: across the whole store, how many actions recorded
`verification_failed` actually took effect?**

## Provenance, and what it is safe to conclude from

| | |
|---|---|
| Original, before any read | `/home/matt/CivSolver/civsim-match-store.db`, mtime **2026-09-22 09:54:54.953748529 -0400**, 11 980 800 bytes |
| Snapshot 1 (the audit's working copy) | taken 10:45 by `cp`; `PRAGMA integrity_check` → `ok`; 49 runs / 105 turn cycles / **670 decision steps** |
| Original, re-checked at 11:02 | mtime **2026-09-22 11:02:08.228740348 -0400**, 12 005 376 bytes — **another lane wrote to it during the audit** |
| Snapshot 2 (taken after that write) | `integrity_check` → `ok`; 50 runs / 106 turn cycles / **671 decision steps** |
| Difference | exactly **one** new step: a `turn.end_turn` recorded `applied`, in a new run `run-e8c96e29c847`. |

The original was never opened in write mode by this audit; its mtime moved because a different lane is
writing to it, not because of anything here. **Every number below was computed on snapshot 1 and
re-verified against snapshot 2**, which carries the identical outcome tally for the population under
study: `verification_failed` 281, `unavailable_to_human_now` 268, `applied` 113 → 114,
`out_of_parity_camera` 8.

**Method note worth preserving.** Each step's own recorded `verification_predicate` was re-evaluated
against every later observation in the same run **using the harness's real bindings** —
`civsim_harness.act.predicates.build_predicate_bindings`, `evaluate_predicate` and
`civsim_harness.act.verify.observed_snapshot` — not a reimplementation. A reimplementation would have
shared the assumptions of the thing under test, which is the defect this audit exists to measure.

## The three counts

Of **281** steps recorded `verification_failed`:

| Verdict | Count | |
|---|---:|---|
| **Took effect** | **25** | the declared post-condition is visible in a later observation of the same run |
| **Did not take effect** | **215** | the predicate is false in every later observation, *and* its own read fields were readable in at least one of them |
| **Undeterminable from the record** | **41** | no later observation, or the predicate's read fields were absent/unbound in all of them |

Folding in the out-of-band camera evidence described below (reading `camera.read_state` directly instead
of through the broken binding) moves 17 of the undeterminable: **28 took effect / 226 did not / 27
undeterminable**. Both splits are given because they rest on different evidence; the first is the answer
to the question as asked, the second is the answer once we stop asking through a binding that does not
exist.

Of the 25 took-effect steps, **10 have clean attribution** (no competing dispatch of the same action
between the failure and the first observation showing the effect) and **15 are ambiguous** — chains of
repeated identical attempts where one of them worked and the record cannot say which. The ambiguous 15
are 8 × `prompts.tech_civic_completed`, 4 × `research.set_tech`, 2 × `turn.end_turn`,
1 × `prompts.era_dedication`.

### Per action

| Action | took effect | did not | undeterminable | n |
|---|---:|---:|---:|---:|
| `cities.set_production` | 0 | 110 | 3 | 113 |
| `prompts.ai_diplomatic_approach` | 0 | 77 | 5 | 82 |
| `turn.end_turn` | 4 | 5 | 9 | 18 |
| `research.set_tech` | 5 | 11 | 0 | 16 |
| `units.move_to` | 7 | 2 | 4 | 13 |
| `camera.zoom` | 0 | 0 | 10 | 10 |
| `diplomacy.send_delegation` | 0 | 9 | 1 | 10 |
| `prompts.tech_civic_completed` | 8 | 0 | 0 | 8 |
| `camera.set_view_mode` | 0 | 0 | 7 | 7 |
| `diplomacy.declare_war` | 0 | 0 | 2 | 2 |
| `policies.slot_policy` | 0 | 1 | 0 | 1 |
| `prompts.era_dedication` | 1 | 0 | 0 | 1 |
| **total** | **25** | **215** | **41** | **281** |

With the camera out-of-band reading substituted: `camera.zoom` 2 / 8 / 0 and `camera.set_view_mode`
1 / 3 / 3.

**A negative here is a positive statement about a search that could have matched.** No
`did_not_take_effect` verdict rests on a field the store never emits. For `cities.set_production`, the
predicate reads `city.production_queue`; that field is present on **609 of 609** city entries store-wide
and holds `["UNIT_BUILDER"]` in **43** of them, so an effect would have been visible had one occurred —
and the three runs where `set_production` failed (`run-02168773ad`, `run-26d265f6f2`, `run-ba3ad80d69`)
are disjoint from the three where it was `applied` (`run-d3d0368b0c`, `run-e90d69673d`, `run-e9d52051ce`).

## Dispatch-to-visible-effect latency — the number nobody had

**What the store can and cannot resolve.** Observations are assembled once per decision step, so the
record samples the game roughly every 10–30 s. It can therefore give an *upper bound* on how long an
effect took to become visible, never the value itself. Two anchors are used, both directly in the record:
`verified_at` (the instant the harness declared failure) and the `assembled_at` of the first observation
that shows the effect.

Clean-attribution took-effect steps, as brackets on "how long after the harness gave up the effect was
first visible":

| Action | run | bracket (s) |
|---|---|---|
| `units.move_to` | `run-d2184c445c3a` | (0, 21.12] |
| `units.move_to` | `run-d2184c445c3a` | (0, 22.50] |
| `units.move_to` | `run-de13afc601b9` | (0, 23.23] |
| `units.move_to` | `run-de13afc601b9` | (0, 23.48] |
| `units.move_to` | `run-de13afc601b9` | (0, 24.08] |
| `units.move_to` | `run-d2184c445c3a` | (0, 25.19] |
| `units.move_to` | `run-227998759f75` | (9.63, 30.15] |
| `research.set_tech` | `run-d2184c445c3a` | (0, 12.29] |
| `turn.end_turn` | `run-9505f323bbf8` | (0, 1.70] |
| `turn.end_turn` | `run-e9d52051ce7b` | (1.75, 12.76] |

Including the 15 ambiguous-attribution cases, the full distribution of "first visible, measured from
`verified_at`" is n=25, min 1.70 s, median 25.19 s, max 112.26 s; measured from the step's own
`started_at`, min 2.60 s, median 48.49 s, max 145.26 s.

**Actionable reading.** One bracket — `units.move_to` in `run-227998759f75` — has a **lower** bound of
**9.63 s**: the effect was demonstrably *not* visible 9.63 s after the harness gave up, and *was* visible
by 30.15 s. That single row is sufficient to retire `ACTION_CONFIRM_TIMEOUT_S = 4.0` as a bound for
`units.move_to`; every other move bracket is consistent with anything in (0, ~21–25] s. The live lane's
own probe (recorded in the store's commentary at `decision_loop.py:940`, a warrior on its destination at
+1 s) narrows the *typical* case to about a second — the two findings are not in conflict, they bracket
a distribution whose tail the 4 s bound does not cover.

**What the store cannot tell you** is the shape of that distribution between 0 and ~20 s, because it
never looks there. Setting the real bound needs a probe that re-reads at sub-second cadence; this audit
supplies the ceiling, not the curve.

## Mechanisms behind the 281

Classified by signature rather than by count; a step can match more than one.

| Mechanism | steps | |
|---|---:|---|
| **M1 — landed after the window closed** | 25 | the 4 s bound (or, pre-bounded-confirm, a single read at +0 s) expired before the effect appeared |
| **M2 — dispatched into a modal it does not answer** | 9 | `camera.zoom` ×5 and `camera.set_view_mode` ×2 into `prompt.tech_civic_completed`; `diplomacy.declare_war` ×2 into `prompt.diplomatic_approach` |
| **M2? — consecutive-failure streak spanning unrelated actions** | 57 | the blocked-board fingerprint: a maximal run of ≥3 consecutive `verification_failed` steps covering ≥2 distinct declarations |
| **M3 — predicate reads a field the record never carries** | 19 | `camera.set_view_mode` 7, `camera.zoom` 10, `diplomacy.declare_war` 2 |
| **M4 — the game itself refused the order** | 3 | `dispatch_result.ok = false`: 2 × `unknown_prompt`, 1 × `unknown_policy` |
| **M5 — none of the above signatures** | 190 | see below |

18 steps carry more than one tag and are flagged rather than forced into a bucket (7 are M1+M2?, 9 are
M2+M3, 2 are M2?+M4).

**On modal blindness specifically.** The fingerprint described by the live lane — an unlisted screen
reading as *no screen* — **cannot be positively identified in this store**, and that absence is itself
the point. All 670 observations carry `recognized: true`; no `EndGameMenu` or other end-game
`raw_screen_id` appears anywhere; only two `unknown_screen` events were ever emitted
(`TechCivicCompletedPopup`, `WorldCongressIntro`), which is the *opposite* signature — there the probe
did say it did not know the screen. A harness that is blind to a modal produces a record indistinguishable
from a harness on a quiet board, so this store cannot be used either to confirm or to bound mechanism 2.
What it does show is the nine M2 steps above, where the harness **was not blind** — it recognised the
blocking prompt and dispatched an unrelated order into it anyway. That is an availability-gate gap, not
a screen-identity gap, and it is separately fixable.

**The 190 M5 steps** are not a mystery bucket; they are dominated by two documented failures that simply
do not match any of the three signatures:

- **`cities.set_production` ×110.** The order never landed. An operator probe recorded in the store
  (`run-ba3ad80d69`, `operator_intervention` at 2026-09-22T00:21:07Z) script-issued the same BUILD with
  correct parameters and read the queue back at +1/+3/+6/+10 s: `cur_name: UNIT_BUILDER` at **+1 s**. So
  the game accepts the operation and the queue is readable within a second — the harness's own orders
  were not landing, which is consistent with the `city_not_found` refusals already documented at
  `decision_loop.py:925`. This is a dispatch defect, not a timing defect, and the 4 s bound is innocent
  of it.
- **`prompts.ai_diplomatic_approach` ×56 (77 did-not-take-effect in total).** Two of these carry the
  game's own answer, `dispatch_result: {"reason": "unknown_prompt", "ok": false}`, and two
  `operator_intervention` records state the cause outright: *"prompt.diplomatic_approach maps to no Lua
  state yet"*. The prompt could not be answered at all; play was cleared by hand.

## The coverage headline

`store coverage` counts an action as *demonstrated live* when it has at least one `ExecutionOutcome.APPLIED`
on record (`src/civsim_harness/store/coverage.py:350`). Reproduced against this store: **15 of 41**
(36.6 %) — `cities.select`, `cities.set_production`, `prompts.ai_diplomatic_approach`,
`prompts.boost_unlocked`, `prompts.congress_intro`, `prompts.era_dedication`, `prompts.era_transition`,
`prompts.natural_disaster`, `prompts.tech_civic_completed`, `research.set_tech`, `saves.save_game`,
`turn.end_turn`, `units.found_city`, `units.move_to`, `units.select`.

### The numerator does not move. The denominator does.

**"15 of 41 is a floor, not a count" was the wrong framing and should not be carried forward.** Counting
every took-effect step as applied adds **zero** actions to the demonstrated set: all five actions with a
took-effect step (`units.move_to`, `turn.end_turn`, `research.set_tech`, `prompts.tech_civic_completed`,
`prompts.era_dedication`) were already among the fifteen. Mechanism 1 **deepens the evidence for actions
already on the board; it does not widen the board.** With the camera out-of-band evidence, `camera.zoom`
and `camera.set_view_mode` would cross from 0 to ≥1 — but only by a method the scorecard does not use and
should not silently adopt; recorded here as the size of the prize for fixing the binding, not as a
correction to the number.

What is genuinely wrong with the headline is its **denominator**: it counts actions no run could ever
score. Six of the 41 have a `verification_predicate` reading a field that appears in **none** of the 670
observation bundles. That is a fact about our catalog, not a fact about the game, and it has a different
remedy: one needs a run, the other needs a predicate.

| Category | actions | what it means |
|---|---|---|
| **Binding never written** | `camera.move`, `camera.set_view_mode`, `camera.zoom` | `build_predicate_bindings` binds the `camera` namespace to `{}` unconditionally (`act/predicates.py:450`, commented as a catalog gap) — **even though `camera.read_state` emits `mode` (670/670), `zoom` (669/670), `target_is_revealed` (670/670) and `target_plot` (427/670) in the bundles.** The data is there; the binding is not. Cheapest fix on the board. |
| **Unsatisfiable from the tuner context** | `diplomacy.declare_war`, `diplomacy.make_peace` | `other_player.diplomatic_state` appears **zero** times in 670 bundles. `lua/gamecore/diplomacy.lua:70` sets it from a `pcall`'d `Player:GetDiplomaticAI()` that does not exist in `GameCore_Tuner`, and its own comment says the field is then *"reported as null"* — but a nil Lua table field is not encoded as null, it disappears, so the key is **absent** rather than present-and-null. **This is reported as unsatisfiable *from this context*, not unverifiable by construction** — Firaxis' own diplomacy screens run in `InGame`, and this project has already moved three observation bodies there for exactly this reason. A live probe of the `InGame` accessor is pending and will settle it. |
| **Subject never occurred** | `espionage.assign_mission` | `spy.mission` is unreadable only because `espionage.state` reads `{"spies": []}` in **all 670** bundles. No spy ever existed. This is *not* an unverifiable action — it is an action nobody could attempt — and it is deliberately kept out of the bucket above. |

**A latent false positive in the same pair.** The two diplomacy predicates are not symmetric in how they
fail. `declare_war` reads `other_player.diplomatic_state == "war"`, which against an absent field is
`None == "war"` → **False**: it under-reports, recording a landed order as refused. `make_peace` reads
`other_player.diplomatic_state != "war"`, which against the same absent field is `None != "war"` →
**True**: it would record a no-op as **applied**. Verified directly against a store observation with a
met player (`other_player` binds to
`{'exists': True, 'has_met': True, 'civilization': 'CIVILIZATION_AUSTRALIA', 'player_id': 1, 'has_delegation': False}`).
It has never fired because the availability predicate — which reads the same absent field — refuses the
action first, so `make_peace` is unreachable rather than wrong today. Fixing the context without fixing
the predicate shape would turn an unreachable action into a silently-lying one.

**The corrected board, with its composition:**

- as published: **15 of 41** (36.6 %)
- excluding the five actions whose predicate cannot be satisfied as the harness currently reads the game:
  **15 of 36** (41.7 %)
- also excluding `espionage.assign_mission`, for which no subject has ever existed in any recorded run:
  **15 of 35** (42.9 %)

None of the excluded six is among the demonstrated fifteen, so the numerator is unaffected in every
variant. **Do not sum the mechanisms into one number.** The part that is "worked but we looked too early"
is 25 steps across 5 already-demonstrated actions. The part that is "we could not have known" is 19 steps
across 3 actions whose predicates cannot resolve. They are different facts with different fixes.

### The fourth outcome the board is missing

The scorecard today has *demonstrated* and *not demonstrated*. It needs a third: **UNVERIFIABLE** — an
action whose declared post-condition cannot be read back at all. Filing those five alongside actions that
simply have not been tried tells a reader the harness is less capable than it is, **and** tells them
nothing about which of the two they could fix today.

## Worked example: the war declaration

`run-5cf209a76e73420d80efae6295cbc83d`, step 4, 2026-09-22T14:31:03Z, turn 67, `diplomacy.declare_war`
against player 3 (Georgia):

```
dispatch_result: {"ok": true, "mechanism": "PlayerOperations.DIPLOMACY_DECLARE_WAR", "target_player_id": 3}
verification:    predicate  other_player.diplomatic_state == "war"
                 result false, confirm_timeout_s 4.0, confirm_attempts 4, confirm_elapsed_s 5.899
                 last_read {"other_player.diplomatic_state == \"war\"": false}
```

The game's own Lua **accepted the order and said so**. The relation body it was verified against reads
`{"player_id": 3, "has_met": true, "has_delegation": false, "civilization": "CIVILIZATION_GEORGIA"}` —
there is no `diplomatic_state` key in it, and there is no at-war field anywhere else in the store either.
The confirm loop polled four times over 5.9 s and every read said the same thing, because the field does
not exist. The declaration almost certainly landed — the capital was captured the same turn — and it
**could never have been recorded as applied**, whatever bound it had been given.

This is the cleanest demonstration available that `verification_failed` does not mean "did not happen".

**A second, smaller one in the same shape.** Three camera steps have their effect visible *in the very
read the harness declared failure on*: `run-68ab7ccdf07b` `camera.set_view_mode strategic` with
`dispatch_result {"ok": true, "mode": "strategic", "mechanism": "UI.SetWorldRenderView"}` and
`camera.zoom 0.05` with `{"ok": true, "zoom": 0.05, "mechanism": "UI.SetMapZoom"}`, and
`run-55e5bfeab48b` `camera.zoom 0.05` — in each case `camera.read_state` in that same observation already
showed the requested value. The harness had the answer in its hand and could not read it.

## The end turn that is *not* mechanism 1

Step 7 of the same run, `turn.end_turn`:

```
dispatch_result: {"ok": true, "result_is_informative": false, "path": "UI.RequestAction"}
verification:    confirm_timeout_s 200.0, confirm_poll_s 2.0, confirm_attempts 69, confirm_elapsed_s 201.957
                 turn_number stayed 67
```

**The harness waited three and a half minutes.** This does not belong with the war declaration and must
not be cited alongside it. It is a genuine did-not-take-effect, and it is evidence that the 200 s bound
is doing its job — not evidence against it. The turn did not advance because, on the most likely reading,
the game had already ended.

## The game end left no trace in the store

All 670 bundles in the main store read `game.outcome_state: {"is_game_over": false, "outcome": "none"}`.
No end-game `raw_screen_id` appears anywhere. In the stray store, the same reading still holds at
14:34:35Z, **three and a half minutes after the war declaration**.

The most consequential event in the match — a defeat, the capital captured — is absent from the record.
The benign explanation (no run was active when it resolved, so no observation was taken) does not survive
contact with the stray store, where a run *was* active and still read `is_game_over: false`. **A match can
end and the store can be silently complete-looking about it.** That is stated here as a finding, not
diagnosed: it should be raised as its own task against `game.outcome_state` and the screen watchlist.

Related and separate: **every step of that run carries
`capture_failed: "no game window is currently resolved to capture"`.** That is not a withheld frame —
a withheld frame means the screening gate looked and declined. This means there was nothing to look at.
The two are different facts and the record should not let them read alike.

## Findings that rest on the stray store

`/home/matt/CivSolver-live/civsim-match-store.db` was created by accident today. It is **not** a duplicate.
It holds one run, `run-5cf209a76e73420d80efae6295cbc83d` (config `goal-move_unit_to_plot-20260922T143051Z`,
14:30:52–14:34:35Z, 7 decision steps, 19 run events, 9 captures, 1 save point), under a different
`store_id` (`fa51759501654956bb618095faa8ed4c`), and **zero** of its rows appear in the main store — no
shared `run_id`, `decision_step_id`, `event_id` or `capture_id`. The main store's newest run started
13:53:28Z, before it.

Everything in the two sections above — the `declare_war` worked example, the 202 s end turn, and the
`capture_failed` observation — **rests on the stray store and on nothing else.** The main store's own two
`declare_war` steps (`run-7f5446ef3e9d`, turn 1) carry no `dispatch_result` at all and are classified
undeterminable here. A reader should know that provenance until the merge lands.

**Ruling recorded:** the stray store gets merged through the sanctioned export/import path, and nothing
is deleted until the merge is verified. The live lane owns that worktree. Until then, the only record of
the match ending lives outside the primary artefact.

## What remains undeterminable, and why

| Reason | steps |
|---|---:|
| No later observation exists in the run — the failing step was the last one recorded | 18 |
| `camera.zoom` — predicate reads `camera.zoom`, bound to `{}` (resolved out of band below) | 10 |
| `camera.mode` — same | 7 |
| `unit.plot` / `unit.queued_path` unreadable in every later observation (no unit selected) | 4 |
| `other_player.diplomatic_state` — field absent from every bundle | 2 |

Of the 17 camera steps, reading `camera.read_state` **directly**, out of band, resolves 14: three took
effect (listed above), eleven never reached the requested value in any later read, and three are
genuinely undeterminable because the camera was *already* in the requested state, so no observable change
was required. `unit.queued_path` is worth noting on its own: the predicate reads it, and `units.state`
emits `has_queued_orders` instead — a second, narrower instance of the same absent-field problem, though
it never mattered because `unit.plot` carried the check.

The 18 "no later observation" cases cannot be improved from the store as it stands. The post-dispatch read
that would answer them is taken and then discarded when the turn cycle ends; only the newer
bounded-confirm records preserve anything of it, as `verification.last_read`.

## The pattern, stated once

In three subsystems today the same shape appeared: **a `pcall` returning nil produces a field that is
absent, and absence is indistinguishable from "legitimately not applicable"** — exactly as an unlisted
screen reads as *no screen*, and exactly as a binding that was never written reads as *no data*. In every
case a mechanism that did not run is recorded identically to a mechanism that ran and found nothing.
This audit is the thing that made it visible, because re-evaluating a predicate against the record is the
only way to notice that the record could never have satisfied it.

## Recommended tasks

1. **Bind the `camera` namespace** from `camera.read_state` in `build_predicate_bindings`. The data is in
   every bundle. Makes three catalog actions verifiable; no new Lua.
2. **Probe `GetDiplomaticAI()` from `InGame`** and move the diplomacy relation read there if it answers.
   Settles `declare_war` and `make_peace`.
3. **Raise `ACTION_CONFIRM_TIMEOUT_S`** — 4.0 s is below the measured ceiling for `units.move_to`
   (one bracket's *lower* bound is 9.63 s). Take a sub-second-cadence probe to set the value properly
   rather than guessing from this audit's upper bounds.
4. **Add UNVERIFIABLE to the coverage vocabulary**, and never publish a denominator that mixes it with
   "not demonstrated".
5. **Fix `cities.set_production`'s dispatch parameters** — 110 non-landings, with an operator probe
   showing the same BUILD lands in 1 s when issued correctly.
6. **Gate dispatch on the blocking prompt** — nine orders were sent into a modal the harness had already
   recognised.
7. **Investigate `game.outcome_state`** — it read `is_game_over: false` three and a half minutes after the
   game had, in all likelihood, ended.
8. **Merge the stray store** and verify before deleting anything.

---

*Every claim above was looked up against the artefact it is about: all 670 (and 671) bundles, not a
sample, with no confirming search truncated.*
