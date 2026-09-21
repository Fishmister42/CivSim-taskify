# Linux model-driven runs — Claude Sonnet 5 plays through the production harness

2026-09-21, 00:33–01:30 EDT. Civilization VI 1.0.12.9 (Aspyr, native Linux), BBG 7.5.0 active,
X11/Cinnamon. Production `OpenRouterProvider`, real key, `anthropic/claude-sonnet-5` primary,
`claude-opus-5` fallback (never invoked). Driver: `tests/live/demo_landed_run.py --provider
openrouter --turns 3` — the same production composition root as the landed-code demo, with the
real provider in place of the fake. Analysis: `../first-model-driven-runs-linux.md`.

> **Real model calls, real decisions, real spend.** Every decision below was produced by the
> model from the fourteen observation declarations the harness read off the live client. **No
> image reached the model**: the host tier is `SUPPORTED` (T252) and every capture was withheld
> by the provenance gate (no camera look-at getter on this build) — text only, labelled
> `visually_degraded` on each run. The operator steps and the configuration-from-read-back are
> the same as the landed-code demo and are described in `../demo-evidence-linux-landed/README.md`.

## The attempts, as the store recorded them

| attempt | run | outcome | calls | spend | what the model did | what stopped it |
|---|---|---|---|---|---|---|
| 1 | `run-e8cf4b9a…` | **paused** (`BackstopEndTurnNotConfirmed`) | 8 | $0.084 | `units.found_city` ×8, no target | Lua got a nil unit id → `unit_not_found`; the backstop ended the turn and its single readback ran before the client had confirmed it (the turn *had* ended) |
| 2 | `run-bba7a243…` | **finished** (3 turns, game 6→9) | 24 | $0.241 | `units.found_city` ×24, no target | refused before dispatch: `unit.is_selected` was a predicate field no observation produced |
| 3 | `run-ea014cb6…` | **finished** (3 turns, game 9→12) | 24 | $0.397 | `units.found_city` ×24, no target; the catalog was now shown to it | refused: the README §4 "selected unit" rule was documented, never implemented |
| 4 | `run-d1d3e263…` | **finished** (3 turns, game 12→15) | 21 | $0.408 | **`units.found_city {"target": 65536}` → `applied` — Pasargadae founded**; then `units.move_to` with the unit id as target ×11, `research.set_tech` with no target ×7, one `turn.end_turn` | move: the plot target bound no subject; research: no target (the catalog text had not said a tech is chosen by name); end turn recorded `verification_failed` by the same early readback the backstop had already been cured of |

Read back from the client after attempt 4, on a fresh connection: `{"turn": 15, "cities": 1,
"city_name": "Pasargadae", "research": "TECH_POTTERY"}`. The city is the first model decision to
land in the game through the full production path: model → decision parse → availability
predicate → Lua dispatch → far-side verification (`not unit.exists`: the Settler was consumed).

Each attempt's own files sit in `attempt-N-*/`: `harness-landed-run.gif` and key frames through
the production `capture_window()` path, `timeline.txt`, `results.json`, and the `run-config.yaml`
the driver wrote from the live read-back.

## What each attempt changed in the harness (all measured first, all in commits on `live/linux`)

1. `run/turn_cycle.py`: the backstop re-reads the game, bounded, until the end-turn predicate holds.
2. `lua/gamecore/units.lua`: `is_selected` from `UI.GetHeadSelectedUnit()`; `agent/context.py`:
   the action catalog (ids, summaries, the `target` convention) is rendered into the observation
   text — the model had been shown the role and the observation only.
3. `act/predicates.py` + `run/decision_loop.py`: a unit/city action with no `target` resolves to
   the selected unit/city once, before dispatch, so dispatch, execution and verification agree.
4. `act/predicates.py`: a `target` that names no entry (a plot, a promotion, a production item)
   binds the selected subject; `lua/ingame/unit_orders.lua`: a lone plot argument is the
   destination; `run/decision_loop.py`: the agent's own end turn gets the same bounded
   confirmation as the backstop's; four action summaries now state their `target` shape.

## Honest limits

- The model never saw the screen. Until T252 lands and a camera look-at getter is found, every
  Linux run is text-only and says so.
- `model_calls` table rows are 0 for every run; the `ModelCall` records (cost, tokens, latency,
  model served) live inside each decision step's bundle. Reported, not chased.
- The stranded runs from earlier tonight (`run-b2c485cc…`, `run-ce0cc7a0…`, `run-76435621…`)
  stay `paused` in the store — the honest record of the attempts that found the T213 gaps.
- $0.72 spent through attempt 3, $1.13 through attempt 4, of the pre-spent $80 cap.
