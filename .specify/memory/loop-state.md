# Loop state — overwrite on every hypervisor cycle (last: 2026-09-21 19:22 EDT)

This file is the compact, current state of the spec-completion loop. The append-only history is
`hypervisor-log.md`; the play record is GitHub issue #3; coordination is issue #1. A fresh session
reads the constitution, this file, then issue #1's tail, and continues.

## Owner directives in force (2026-09-21)
- Deliverable: the harness **visibly playing**, breadth of demonstrated actions over play quality;
  every claim on the board observed on the live client, "not observed" where not.
- Stochastic testing selects **only from actions of a reachable state** (predicate holds against
  the current observation). Directed testing complements it: agents puzzle through **specific game
  items** (found a second city, slot a policy, promote a unit, found a pantheon, send a delegation)
  as goal runs with a read-back success predicate. Agents directing overall strategy stay out of scope.
- Autoplay the game's own AI to reach deep saves (~t100/t150/t200); run goal runs from them.
- Spend: the OpenRouter cap is the agent's for the week; run rate is far below it — **increase it**.
  Never ask before spending; the provider's refusal is the only stop.
- Steam/client: the owner wants Steam back at about **21:00 EDT on 2026-09-21**; release it with a
  `status: done` post on #1 before then. On later days, check before launching.
- The T201 soak is parked ("I don't even care right now").

## Standing rules for every agent (learned the hard way today)
- **Never wait on a background-task notification.** Run long steps in the foreground under
  `timeout`, or poll a detached process's log in a bounded loop. Live stages send the hypervisor a
  heartbeat (SendMessage) at least every 20 minutes; the hypervisor's cron nudges any silent agent.
- **Live runs execute from a clean detached worktree, never the shared working tree.** A neighbour's
  half-edit cost a paid block (a `cost` key tripped the parity filter). `/home/matt/CivSolver-live` is a
  `git worktree` the hypervisor advances to each blessed commit (`git -C /home/matt/CivSolver-live
  checkout --detach <hash>`). Run from that directory with
  `PYTHONPATH=/home/matt/CivSolver-live/src UV_PROJECT_ENVIRONMENT=/home/matt/CivSolver/.venv uv run
  --no-sync python -m tests.live.<driver> … --store /home/matt/CivSolver/civsim-match-store.db` and an
  absolute output dir under the main tree's `spikes/gameplay-<date>/`. Nobody edits the worktree.
- **Test command** (the only accepted form): `timeout 900 uv run pytest -q -p no:cacheprovider
  -o faulthandler_timeout=120` (Lua-executing tests: prefix `uv run --with lupa`). A run that does
  not complete is a hang, named by the faulthandler dump; a pytest process older than 15 minutes is
  killed by pid. Suite must complete green before a merge past `7b7eb04`.
- Git on a shared tree: explicit paths, `git add <paths> && git commit -- <paths>` back to back,
  push after each commit, never `-A`/stash/reset/rebase/force. Catalog-shape pins move with the catalog.
- Host: never `pgrep -f`; `import` only under `timeout`; the tuner is one connection; popups are
  not persisted in saves; the UI lays out at 1024×768 (scale 1.875 × 1.5625 on 1920×1200).

## Agents running now
| lane | scope | files | started |
|---|---|---|---|
| productions (Opus, resumed) | live: the production LIST works but `cities.set_production` issues BUILD and the queue stays `[]` (9 of 9 rejected); diagnose against `productionpanel.lua`'s exact parameter table (insert mode suspected), bounded queue re-read, operation's own reason | `lua/ingame/city_orders.lua`, `act/verify.py`, tests | 19:18 |
| game-over detection (Opus) | `IsAlive`/end-screen read before the turn-start save → `stop_resolution` defeat/victory; RunEvent schema; Phase 13 task; spike note incl. post-defeat `Network.LoadGame` refusal | `run/game_over.py`, `lua/ingame/game_over.lua`, `run/turn_cycle.py`, `run/runner.py`, `run/composition.py`, `models/records.py`, schemas, `data-model.md`, `tasks.md` | 18:52 |
| accessor audit (Opus) | every Lua body's accessors checked against Firaxis' shipped UI Lua; phantoms replaced with the panel's own accessor or `<field>_reason`; checked-in cited allowlist + CI test | `lua/**`, `lua/ACCESSORS.txt`, spike `lua-accessor-audit-2026-09-21.md`, tests | 19:17 |
| Live S4 (Fable fork) | recovered from the t59 defeat by relaunch (17 min; fresh-menu load 38 s); goal runs from the worktree; builder chain blocked on set_production; running change_research / send_delegation / save_named_game; one labelled probe of the panel's own BUILD call; entry 9 due; wind down 20:45 | client; `spikes/gameplay-2026-09-21/` | 18:00 |

**Landed 18:16–19:15:** availability rendering + coverage policy (3aad0c8, 307a630); goal driver + 13
goals (fc16b0c, ed4c793), all unblocked (cfc6cee, ba9ac6e); scorecard honesty (88923e9); suite hang
fixed + pytest timeouts (70dacae); production list (1d0b372, VERIFIED LIVE); popup mappings
(764b768) — `prompts.era_transition` and `prompts.era_dedication` **VERIFIED LIVE** (the model answered
both in run-26d265f6); `units.build_improvement` + `unit.available_builds` (720e30d); `units.promote`
fixed + `available_promotions` real (6606d4b). **Live worktree at 6606d4b.** Systemic finding: three
bodies called methods Civ VI does not have behind guards that returned silent `[]` (productions,
promotions, a production param key) — audit in flight. Observed live: defeat at t59 recorded as a
save error (fix in flight); post-defeat menu refuses Lua loads and its exit modal ignores synthetic
input (loader/host follow-ups). Still blocked: autoplay (owner permission).

## Queue, in order
1. Suite green (gate). 2. Goal runs live on the milestone saves until ~20:45, Sonnet 5, back to
back, posted to #3 as entries. 3. Structural blockers: `city.available_productions` always `[]`;
camera readback (3 actions never verify); dedication chooser unmapped. 4. Verify live: greeting
answer via `AddResponse`, pre-save clearance. 5. 002 client-gated tasks T177/T191–T194/T198/T200.

## State
Head: see `git log -1`. Coverage (18:00): actions applied 9 of 38, attempted 23 (9 of the 14
never-applied were blind draws at unavailable actions; 5 real: camera ×3, send_delegation,
ai_diplomatic_approach), images delivered 128 of 399 steps. Spend today $5.64. Client: up, game
turn 53 → being autoplayed; saves `civsim-gameplay-2026-09-21-t053` and milestones as produced.
