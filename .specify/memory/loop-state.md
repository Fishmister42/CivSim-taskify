# Loop state — overwrite on every hypervisor cycle (last: 2026-09-21 21:27 EDT)

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
- Steam/client: released 20:30, **re-taken 20:45 at the owner's word for a builder re-trial; the window is
  open-ended — "you'll know when you have to stop, I'll ping you"**. Never wind down on the clock; release
  with a `status: done` on #1 only when the owner pings. On later days, check before launching.
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
| Live S7 (Fable fork) | **client SEGFAULTED** (kernel: `Civ6 segfault at b0 in libGameCore_XP2.so`, 20:46:35, during Stage 6's first turn on worktree 454b2f8 — the first live sweep with tonight's repaired bodies; the harness saw only ConnectionResetError, stop_reason None). Steam would not respawn the client (2 tries); trying `-applaunch`, Steam dialog check, Steam restart (20-min cap). Then: load `…-end2`, one 1-turn coverage block at 454b2f8 under crash watch; if it crashes, same block at 14f7418 to confirm the bisect; then the guard patch | client | 21:08 |
| crash diff (Fable fork, headless) | `git diff 14f7418..454b2f8 -- lua/`: every new engine call that runs at turn start / probe / sweep, ranked for null dereference on this board (no governors, no religion, no congress, no spies; city-state/barbarian player slots), with Firaxis' own guard for each; guard patch + lupa tests; `spikes/client-segfault-2026-09-21.md` | `lua/**`, tests | 21:25 |
| orphan hygiene (Opus, retried ×2 after API 500s) | orphan detection on write-mode open + runner start; `civsim store repair [--dry-run]` | `run/lifecycle.py`, `run/runner.py`, `store/`, `operator/store_cli.py`, tests | 21:21 |

**Rule from the crash:** a method that exists is not a method that is safe to call on every object; every
new engine call is guarded the way Firaxis guards it, and "unverified live" now means "may crash the
client" until a one-turn crash-watched block has run it. **Do not play from 454b2f8 unguarded; the last
hash known not to crash on this board is 14f7418.** Head `454b2f8`+ (main tree fine for headless work).
Coverage at 20:29: actions applied live 11 of 41, images 287 of 619 steps, spend $13.40 cumulative.

## Queue, in order (2026-09-22)
1. Owner's OK to launch. 2. From the worktree at head: verify live `cities.set_production` (once the
dispatch-answer commit lands) → `--goal use_a_builder` (build → use) → Settler → `found_second_city`;
the first-meeting answer (`path: add_response`, `choice_key`, `offered_after` changed); the 14 repaired
actions as play reaches them (policies after Code of Laws, delegation, camera zoom/view). 3. Loader:
post-defeat main menu refuses `Network.LoadGame` and its exit modal ignores synthetic input → build the
relaunch into Principle VII recovery. 4. Web `/compare` render-budget test is load-sensitive on this box.
5. Autoplay fast-forward: owner runs `! uv run python specs/002-civ-playing-harness/spikes/autoplay_ff.py --turns 2`.
6. `debug_overlay` corner false positive. 7. 002 client-gated tasks T177/T191–T194/T198/T200. T201 parked.

## State
Head: see `git log -1`. Coverage (18:00): actions applied 9 of 38, attempted 23 (9 of the 14
never-applied were blind draws at unavailable actions; 5 real: camera ×3, send_delegation,
ai_diplomatic_approach), images delivered 128 of 399 steps. Spend today $5.64. Client: up, game
turn 53 → being autoplayed; saves `civsim-gameplay-2026-09-21-t053` and milestones as produced.
