# Loop state — overwrite on every hypervisor cycle (last: 2026-09-21 19:08 EDT)

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
| goal-runs (Opus, resumed) | unblock `use_a_builder` now that `units.build_improvement` exists; re-check its predicate against the real unit fields; update the chain test | `tests/live/goals/use_a_builder.yaml`, `tests/live/goal_run.py`, `tests/unit/test_goal_runs.py` | 18:57 |
| builder charge (Opus, resumed) | fix pre-existing `units.promote` bug (promotion name lands in `unitId` → always `unit_not_found`) with the move_to normalisation guard | `act/executor`, unit Lua, `tests/unit/test_promote_dispatch.py` | 18:58 |
| game-over detection (Opus) | `IsAlive`/end-screen read before the turn-start save → `stop_resolution` defeat/victory; RunEvent schema; Phase 13 task line; spike note incl. the post-defeat `Network.LoadGame` refusal | `run/game_over.py`, `lua/ingame/game_over.lua`, `run/turn_cycle.py`, `run/runner.py`, `run/composition.py`, `models/records.py`, schemas, `data-model.md` | 18:52 |
| Live S4 (Fable fork) | after the t59 defeat the post-game main menu refused `Network.LoadGame`; relaunching the client (fallback) to load the ~t42 quicksave via the T248 path; then build_a_builder → Settler → found_second_city → use_a_builder; entries 8+ on #3; wind down 20:45 | client; `spikes/gameplay-2026-09-21/` | 18:00 |

**Landed 18:16–18:56:** availability rendering + coverage policy (3aad0c8, 307a630); goal driver + 13
goals (fc16b0c), `--provider-policy` forwarded + signature drift guards (ed4c793); scorecard honesty
(88923e9); suite hang fixed + pytest timeouts (70dacae); production list fixed (1d0b372) and VERIFIED
LIVE (8 items incl. UNIT_BUILDER/UNIT_SETTLER); production goals unblocked (cfc6cee); popup mappings —
dedication chooser, congress intro/vote, "Goodbye" → CloseSession (764b768); **first charge-spending
action** `units.build_improvement` + `unit.available_builds` (720e30d). **Suite green at head: 2187
passed / 9 skipped / 0 failed, no hang. Live worktree at 720e30d.** Observed live: first game over
(defeat, t59) recorded as a save error (fix in flight); post-defeat main menu refuses Lua loads
(relaunch needed — loader follow-up). Still blocked: autoplay (owner permission).

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
