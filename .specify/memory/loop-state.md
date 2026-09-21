# Loop state — overwrite on every hypervisor cycle (last: 2026-09-21 19:53 EDT)

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
| diplomacy fix (Opus, resumed) | live negative: the first-meeting STATEMENT answer never confirms (16/16, run-d0933ca8); diagnose from the store whether options changed after an answer (verifier too strict: a correct statement leaves the conversation open with the exit next) or the response never lands (needs the real click); record path/reason on the statement branch | diplomacy answer Lua, `act/verify.py` predicate for the prompt, tests | 19:50 |
| productions (Opus, resumed) | generalise the bounded verification re-read (~2 s) to every action so a lagging read is never a refusal | `act/verify.py`, `run/turn_cycle.py`, `run/decision_loop.py`, `tests/unit/test_verify_confirm.py` | 19:35 |
| accessor audit (Opus) | report landed (a606729: 28 phantom names, 10 fields permanently `[]`, 14 of 38 actions could never work — posted to #3); fixes in file-scoped commits: congress, government, great_people, religion bodies + `empire_orders.lua`, then the allowlist + CI test | `lua/**`, `catalogs/observations/*.yaml`, `lua/ACCESSORS.txt`, tests | 19:17 |
| Live S4 (Fable fork) | builder chain `--goal use_a_builder` running from 59af4a2 (polling in the foreground); entry 10 on its result; Settler + second city if time; end save `civsim-gameplay-2026-09-21-end` and client InGame by 20:55 | client; `spikes/gameplay-2026-09-21/` | 18:00 |

**Landed 18:16–19:44:** availability rendering + coverage policy; goal driver + 13 goals, all unblocked;
scorecard honesty; suite hang fixed + pytest timeouts; production list (VERIFIED LIVE) and
`cities.set_production` (59af4a2; live probe confirmed the game accepts the table); popup mappings —
era card + dedication chooser VERIFIED LIVE; `units.build_improvement`; `units.promote` fixed;
game-over detection (f31fb5b); accessor audit report (a606729). **Live worktree at 59af4a2.**
Coverage (S4, 19:37): actions applied live 10 of 41, attempted while available 17, images delivered
220 of 499 steps. Loader/host follow-ups: post-defeat menu refuses `Network.LoadGame`; its exit modal
ignores synthetic input (relaunch = 17 min recovery). Still blocked: autoplay (owner permission).

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
