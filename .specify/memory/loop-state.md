# Loop state — overwrite on every hypervisor cycle (last: 2026-09-21 18:45 EDT)

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
| hang-fix (Opus) | suite hang in `test_recovery.py` crash-mid-turn since `c795039`; four image-path test failures named; add pytest hang defaults | `run/turn_cycle.py`, `run/decision_loop.py`, `tests/integration/`, `pyproject.toml` | 17:45 |
| availability (Opus) | render available/unavailable actions with reasons; `--provider-policy coverage` draws ONLY reachable, unapplied actions; scorecard counts blind draws separately | `agent/context.py`, `provider/stochastic.py`, `store/coverage.py`, tests | 17:55 |
| goal runs (Opus) | goal library incl. chain `build_a_builder` → `use_a_builder`; `tests/live/goal_run.py` driver with `--feasibility`; no live runs | `tests/live/goal_run.py`, `tests/live/goals/`, tests | 18:20 |
| productions fix (Opus) | `city.available_productions` always `[]`; fill from the production panel's own list; `cities.set_production` binds from it | `cities.state` Lua + `catalogs/observations/cities.yaml`, action Lua, tests | 18:30 |
| dedication chooser (Opus) | `/InGame/DedicationPopup` → `prompt.era_dedication` + `prompts.era_dedication` (option click + Confirm) | `lua/ingame/screens.lua`, `catalogs/actions/prompts.yaml`, acknowledge Lua, tests | 18:45 |
| Live S4 (Fable fork) | Sonnet 5 blocks from t54 now; goal runs when the driver lands; entries 8+ on #3; wind down 20:45 | client; `spikes/gameplay-2026-09-21/` | 18:45 |

**Autoplay spike (done, `a9ff610`): BLOCKED on a permission the owner must grant.** `AutoplayManager.SetActive(true)`
was refused by the session's permission classifier ("Modify Shared Resources"); the agent did not
work around it. Everything else is measured: autoplay exists on this build with Firaxis' methods
(`automation_standardtests.lua:328-334`), safety save `civsim-gameplay-2026-09-21-t054`, plan in
`spikes/autoplay-fast-forward-linux.md` + `spikes/autoplay_ff.py`. To unblock: the owner runs
`! uv run python specs/002-civ-playing-harness/spikes/autoplay_ff.py --turns 2` in the session, or
adds a Bash allow rule for that script.

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
