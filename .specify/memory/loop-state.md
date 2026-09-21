# Loop state — overwrite on every hypervisor cycle (last: 2026-09-21 18:50 EDT)

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
| goal-driver fix (Opus, resumed) | `tests/live/goal_run.py` aborts: `resolve_provider()` now needs `policy` (307a630); add `--provider-policy`; fix two stale blocked-goal pins | `tests/live/goal_run.py`, `tests/unit/test_goal_runs.py` | 18:47 |
| builder charge (Opus) | selected unit's available builds in `units.state` + `units.build_improvement` from the unit panel's own operation | `catalogs/actions/units.yaml`, `catalogs/observations/units.yaml`, unit Lua, tests | 18:25 |
| game-over detection (Opus) | defeat at t59 was recorded as `SaveVerificationError`; detect `IsAlive`/end screen before the turn-start save → `stop_resolution` defeat/victory, lifecycle finished, no gap; `EndGameMenu` screen id left to the screens lane; adds a Phase 13 task line | `run/turn_cycle.py`, new `lua/ingame/game_over.lua`, `models/`, `store/`, `tests/integration/` | 18:52 |
| Live S4 (Fable fork) | after Persia's defeat at t59, labelled operator reload of the last healthy save (~t42); coverage blocks from the worktree now; goal chain (Builder → Settler → second city) once the driver fix lands; entries 8+ on #3; wind down 20:45 | client; `spikes/gameplay-2026-09-21/` | 18:00 |

**Landed 18:16–18:48:** availability rendering + coverage policy (3aad0c8, 307a630); goal driver + 13
goals (fc16b0c); scorecard honesty (88923e9); suite hang fixed + pytest timeouts (70dacae); production
list fixed (1d0b372) and **VERIFIED LIVE** on run-fbd6c25e — `available_productions` lists 8 items incl.
UNIT_BUILDER and UNIT_SETTLER; blocked markers removed from the two production goals (cfc6cee); popup
mappings — dedication chooser, congress intro/vote by control visibility, "Goodbye" → CloseSession,
first-match watchlist order (764b768). **Live worktree at 764b768.** Observed live: first game over
(defeat, t59) — recorded wrongly as a save error, fix in flight. Still blocked: `use_a_builder`
(action in flight); autoplay (owner permission).

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
