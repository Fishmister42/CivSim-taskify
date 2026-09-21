# The first model-driven runs — Claude Sonnet 5 plays (or tries to), Linux, 2026-09-21

Production `OpenRouterProvider`, real key (owner-provided this night), `anthropic/claude-sonnet-5`
primary / `claude-opus-5` fallback, through the unmodified production composition root
(`tests/live/demo_landed_run.py --provider openrouter --turns 3`). Every number below is read from
`civsim-match-store.db`, the OpenRouter `auth/key` endpoint, or the client on a fresh connection.
Evidence per attempt under `demo-evidence-linux-model-driven/attempt-N-*/`.

**Text only.** No image reached the model in any attempt: the host tier is `SUPPORTED` (T252) and
every capture was withheld by the provenance gate (no camera look-at getter on this build — see
`demo-evidence-linux-landed/README.md`). The model played from the fourteen observation
declarations alone.

## Attempt 1 — `run-e8cf4b9a…` — paused, $0.084, 8 calls

Turn 1: eight decisions, all `units.found_city`, all with sound reasoning ("turn 5, no capital,
settler at (43,30) on grass with hills and forest — found it now"), all `parameters: {}`. Each
reached the Lua with a nil unit id and answered `unit_not_found`; the no-progress backstop fired at
8, dispatched `turn.end_turn`, read the turn counter the instant the dispatch returned — still 5 —
and raised `BackstopEndTurnNotConfirmed`. A fresh connection a minute later read **turn 6**: the
turn had ended. **Finding:** the client confirms an end turn asynchronously, after the AI turns;
the backstop's single readback was too early. Fixed: bounded re-read (45 s / 2 s) until the declared
predicate holds; still fails closed. Also found: `model_calls` rows are 0 for every run — the
`ModelCall` is persisted inside each decision step's bundle (cost, tokens, latency all there), not
in the `model_calls` table `write_model_call` targets. Reported, not chased.

## Attempt 2 — `run-bba7a243…` — FINISHED, $0.241, 24 calls, 80,232 in / 8,054 out tokens

Three harness turns (game 6 → 9), each ended by the backstop after eight `units.found_city`
decisions — now **refused before dispatch**: `execution.outcome = rejected`,
`rejection_reason = unavailable_to_human_now`. The availability predicate
`unit.is_selected and unit.owner_is_local_player and unit.can_found_city` binds `unit` from
`parameters.target`, which the model never sent, and even with a target `is_selected` was a field
no observation produced (`act/predicates.py` says so in its own docstring). So every unit and city
action in the catalog was structurally unavailable, on every host, since the catalog was written.
The backstop's confirmation held every time. Fixed: `units.state` reports `is_selected` from
`UI.GetHeadSelectedUnit()`; the observation text now ends with the action catalog (ids +
summaries + the `target` convention) — the model had been shown the role and the observation
only, and was guessing ids.

## Attempt 3 — `run-ea014cb6…` — FINISHED, $0.397, 24 calls, 106,344 in / 18,457 out tokens

Same shape, game 9 → 12. The model now saw the catalog (8.3 KB of observation text per step; the
found-city line said "with no target, the currently selected unit is used") and, reasonably,
still sent no target — one reasoning even names "unit 65536". Refused again on the unbound `unit`.
**Finding:** `catalogs/README.md` §4 already specifies the rule — "the action's own `target` (or,
for a unit/city action, the unit/city the human selected before issuing it)" — and
`build_predicate_bindings` never implemented the parenthesis. Fixed: `resolve_selected_subject_target`
resolves the selected unit/city ONCE in the decision loop when `target` is absent, so dispatch,
execution and verification all see the same subject; a run with nothing selected is still refused,
never guessed.

## What three attempts cost and proved

$0.72 of the $80 cap. 56 model decisions, every one a coherent, correctly-reasoned choice about a
real game state the harness read from the client — and none of them reached the game, for three
reasons that were each invisible to 1,648 green tests: an asynchronous confirmation, a field the
predicate vocabulary named but nothing produced, and a documented rule nobody implemented. The
fake provider's "end the turn" demo could not have found any of them.


## Attempt 4 — `run-d1d3e263…` — FINISHED, $0.408, 21 calls — **Pasargadae founded**

Turn 1, step 1: `units.found_city {"target": 65536}` → `applied / changed_state`. Read back on a
fresh connection after the run: `{"turn": 15, "cities": 1, "city_name": "Pasargadae",
"research": "TECH_POTTERY"}`. The first model decision to land in the game through the whole
production path. The remaining 20 steps found the next gaps: `units.move_to` sent the *unit's* id
as `target` where the catalog wants the destination plot (refused: the plot predicate); seven
`research.set_tech` with no target (the catalog text had not said a tech is chosen by name); and the
model's own `turn.end_turn` recorded `verification_failed` by the same early readback the backstop
had already been cured of (the turn had ended: 14 → 15). Fixed: the binder falls back to the
selected subject when the target names no entry; four action summaries state their target shape;
the agent's end turn gets the bounded confirmation.

## Attempt 5 — `run-8bc17e7e…` — PAUSED at turn 3, $0.294, 16 calls — the first honest stall

Turns 1–2: sixteen `units.move_to {"target": 131073}` — still the warrior's id, not a plot,
despite the catalog line now saying `{"target": {"x": .., "y": ..}}`; refused each time on
`target in unit.reachable_plots`, correctly. Turn 3: the civic **Code of Laws completed**, the game
put up `TechCivicCompletedPopup`, and the harness raised `UnknownScreenEncountered` — the screen is
on `screens.lua`'s watchlist but maps to no catalog screen id, so the run **stalled visibly**
(`record_completeness_status: has_gaps`) rather than pressing anything. That is the FR-010
behaviour the spec asks for, seen live for the first time. **Finding:** the catalog needs an
acknowledge action for tech/civic-completed popups (the `prompts.era_transition` shape) and a
`prompt.*` mapping for `TechCivicCompletedPopup` — a human clicks it away; the agent currently
cannot. The client was left on that popup for the morning to see.

**Net for the night:** $1.42 of $80. One city founded by a model decision; one move-order
convention the model does not yet follow (the id-vs-plot ambiguity of a single `target` field is a
catalog-design smell worth a ruling); one unmapped popup that stops play; images still withheld.
