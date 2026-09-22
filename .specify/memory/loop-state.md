# Loop state — overwrite on every hypervisor cycle (last: 2026-09-22 ~00:45 EDT — day loop running)

This file is the compact, current state of the spec-completion loop. The append-only history is
`hypervisor-log.md`; the play record is GitHub issue #3; coordination is issue #1. A fresh session
reads the constitution, this file, then issue #1's tail, and continues.

## Owner directives in force
- **2026-09-22:** run the spec-completion loop across all specs as a hypervisor + mini-hypervisor
  swarm — an orchestrator that delegates context-heavy coding and never gets nitty-gritty itself;
  parallelize, go out of order, divide as useful; flag the owner only on a complete and total
  blocker. **"You have the run of steam and civ for an extended period of time."** Launching the
  client is authorized; Linux holds the Steam account (posted on #1).
- Deliverable (2026-09-21, still in force): the harness **visibly playing**, breadth of demonstrated
  actions over play quality; every claim on the board observed on the live client, "not observed"
  where not.
- Stochastic testing selects **only from actions of a reachable state**. Directed testing
  complements it: goal runs at **specific game items** with a read-back success predicate.
  Agents directing overall strategy stay out of scope.
- Autoplay the game's own AI to reach deep saves (~t100/t150/t200); run goal runs from them.
- Spend: the OpenRouter cap is the agent's for the week; **never ask**; the provider's refusal is the
  only stop. ≈ $13.40 of the cap used through 2026-09-21.
- The T201 soak is parked ("I don't even care right now").
- Owner-bound still: accepting legal/ToS dialogs; the autoplay fast-forward permission
  (`! uv run python specs/002-civ-playing-harness/spikes/autoplay_ff.py --turns 2`).

## ⚠️ Model budget (found 2026-09-22)
**The Anthropic monthly spend limit for Fable 5.1 is exhausted** — an agent spawned on `fable`
dies instantly with HTTP 429 (two lanes lost this way at day start). Every `Agent` call must pass an
explicit `model`: `opus` for orchestration and judgment, `sonnet` for bounded coding. This session
itself was switched to Opus 5. Separate from the OpenRouter budget, which funds in-game model calls.

## Standing rules for every agent (learned the hard way)
- **Never wait on a background-task notification.** Run long steps in the foreground under
  `timeout` (< 600 s per command), or poll a detached process's log in a bounded loop. Live stages
  send the hypervisor a heartbeat (SendMessage) at least every 20 minutes.
- **Live runs execute from a clean detached worktree, never the shared working tree.**
  `/home/matt/CivSolver-live` is a `git worktree` the hypervisor advances to each blessed commit
  (`git -C /home/matt/CivSolver-live checkout --detach <hash>`). Run from that directory with
  `PYTHONPATH=/home/matt/CivSolver-live/src UV_PROJECT_ENVIRONMENT=/home/matt/CivSolver/.venv uv run
  --no-sync python -m tests.live.<driver> … --store /home/matt/CivSolver/civsim-match-store.db` and an
  absolute output dir under the main tree's `spikes/gameplay-<date>/`. Nobody edits the worktree.
- **Test command** (the only accepted form): `timeout 900 uv run pytest -q -p no:cacheprovider
  -o faulthandler_timeout=120` (Lua-executing tests: prefix `uv run --with lupa`). A run that does
  not complete is a hang, named by the faulthandler dump; a pytest process older than 15 minutes is
  killed by pid. **Green at `5464571`: 2147 passed / 18 skipped / 0 failed.**
- Git on a shared tree: explicit paths, `git add <paths> && git commit -- <paths>` back to back,
  push after each commit, never `-A`/stash/reset/rebase/force. Catalog-shape pins move with the catalog.
- Host: never `pgrep -f`; `import` only under `timeout`; the tuner is one connection; popups are
  not persisted in saves; the UI lays out at 1024×768 (scale 1.875 × 1.5625 on 1920×1200).
- **A fixture the system under test keeps writing to is not a fixture**, and a test that skips at
  runtime has stopped testing (003 T053, 2026-09-22). **A method that exists is not safe from every
  context or on every object** — guard like Firaxis, from the context Firaxis uses (the segfault).
  **"Produces a value" is not "produces the right value"** — an empty list is a value.
- `pwsh` is absent: hand-derive the speckit helper JSON; do not edit `.specify/feature.json` while
  lanes run different features concurrently (it still points at 003).

## Lanes running now (2026-09-22)
1. **002 LIVE** (opus mini-hypervisor) — owns the client. Bring-up → load `…-end2` (t56) → one
   crash-watched turn at head → `cities.set_production` → builder chain → Settler/second city →
   first-meeting answer → T260 → the client-gated live tests (T191/T192/T194/T198/T177, T193 last)
   → T200 quickstart → T237. Owns `lua/**`, `run/**`, `host/linux/**`, `tests/live/**`, `spikes/**`,
   `validation-results.md`.
2. **002 HEADLESS** (opus mini-hypervisor) — analyze → converge → implement everything needing no
   client. Owns loader/recovery, host-generic, `operator/**`, `civsim_web/**`, `store/**`, the 002
   prose artifacts and the structure of `tasks.md`.
3. **001 + 003 re-check** — DONE, see below.

## Results this cycle
**001 and 003 re-checked and both were NOT converged despite arriving 75/75 and 46/46.** Nine
findings, two HIGH; all nine closed at head `5464571`. 001 now 78/78, 003 now 53/53.
- **003 T047 (HIGH):** FR-006's "the store exposes no delete/edit operation" was enforced by
  *omission* — no check of any kind. A `delete_turn_cycle()` added tomorrow would have left all 2138
  tests green. Now a published `MUTATING_OPERATIONS` surface plus a structural partition of both the
  Protocol and the adapter, revert-confirmed.
- **003 T053 (HIGH):** the SC-005 migration test had **aged out of testing anything** — it read the
  live `civsim-match-store.db`, which ordinary use migrated to 1.1 on 2026-09-21, and then
  `pytest.skip`ped on the only machine that has the file. Repointed at the frozen `.bak` snapshot.
  Doing so caught a real unseen interaction: the open-time orphan sweep pauses a lockless run during
  the read-back, so "every run reads back unchanged" is true of the migration and false of a default
  open. Claims separated; W6 now asserted against a real file.
- Reports: `specs/001-unified-web-interface/analyze-2026-09-22.md`,
  `specs/003-match-tracking-store/analyze-2026-09-22.md`.

## Queue behind the lanes
Other-platform, cannot close on this node: T050 Windows adapter, T051 macOS adapter, T099 R6
win/mac, T199 R20 cross-platform save, T224 macOS pixel extraction, T257 Wayland portal.
Owner-adjacent: T237 production bring-up wiring, the autoplay permission. Parked: T201 soak.
**Not started, owner's call:** the constitution charters five deliverables; specs exist for three.
Deliverable 4 (optimization / Monte Carlo layer) and deliverable 5 (`GUIDEBOOK.md`, which Principle V
makes a hard gate on deliverable 4) have no spec — `/speckit-specify` them when the owner rules.
