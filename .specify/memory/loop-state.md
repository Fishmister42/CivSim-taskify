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
  -o faulthandler_timeout=120` (Lua-executing tests: prefix `uv run --with lupa`). **Redirect it to a
  log and poll that log in a bounded loop** rather than piping to `tail` — `-q` plus `| tail -N` makes
  a correct 225–270 s suite **completely silent for longer than the watchdog's 3-minute kill ceiling**,
  so it looks identical to a wedged process while working perfectly. A growing log is the liveness
  signal. (Scope note: the owner's 3-minute criterion governs **harness game runs**, not an agent's
  shell commands — but a silent-by-construction command is a hazard either way, and the poll form is
  already what the no-waiting rule asks for.) A run that does
  not complete is a hang, named by the faulthandler dump; a pytest process older than 15 minutes is
  killed by pid. **Green at `5464571`: 2147 passed / 18 skipped / 0 failed.**
- Git on a shared tree: explicit paths, `git add <paths> && git commit -- <paths>` back to back,
  push after each commit, never `-A`/stash/reset/rebase/force. Catalog-shape pins move with the catalog.
- **A commit is verified in a clean detached worktree at the committed hash, never against the shared
  working tree** (adopted 2026-09-22). With two lanes holding uncommitted `src/` edits the suite reads
  half-saved files: two runs minutes apart returned 90 and then 98 failures with *different* failing
  modules. So: commit, push, `git worktree add --detach /home/matt/CivSolver-verify <hash>`, run the
  accepted suite command there, revert on red. Never verify in `/home/matt/CivSolver-live` — a bad
  commit there can be picked up by a run before it can be walked back. The rule always meant "the tree
  this commit produces is green"; reading a neighbour's unfinished file was never what it verified.
  - **Use a BARE `uv run`** — no `--no-sync`, no `UV_PROJECT_ENVIRONMENT` override, no `PYTHONPATH`.
    That is the **live-run** form: it borrows the main venv and resolves imports to the *shared* tree,
    so a "verification" run that uses it silently tests the wrong source. Confirmed by
    `uv run python -c "import civsim_harness; print(civsim_harness.__file__)"` from the worktree:
    bare → `CivSolver-verify/src/...`; with the overrides → `CivSolver/src/...`. The hypervisor and the
    live lane each fell into this an hour apart, and the hypervisor then broadcast a wrong "the rule is
    broken" correction after diagnosing an error message instead of running that one command.
  - **A fresh worktree needs `uv sync --all-groups --all-extras` before the suite means anything.**
    Groups and extras are **different axes** here: `jinja2` lives in the `civsim_web` dependency *group*,
    not an extra, so `--all-extras` alone resolves 53 packages, installs **nothing**, and the false red
    persists identically. Two web modules fail collection and it reads as a defect at the committed hash.
    Third distinct way this check misfired in one day. Two tells: **a sync that installs nothing while
    the failure does not move is not addressing the failure**; and confirm the verifier itself
    (`civsim_harness.__file__` resolves into the worktree, and a group-only import succeeds) before
    trusting any result. **Keep an INDEPENDENTLY-PRODUCED green at any head you intend to advance to** —
    different worktree, different venv, same hash. The independence is where the value is: two greens
    from the same environment corroborate nothing, since they share whatever that environment got wrong,
    which is the exact failure mode that produced the false red. **A reference that shares your setup is
    not a control.** So whoever verifies a head publishes its count *and its environment* — worktree,
    venv, sync command — and the next lane can tell whether its own result is evidence or an echo.
  - **A clean worktree isolates the CODE; it does not isolate the MACHINE.** Treat a **timing or budget**
    failure as **inconclusive** until re-run on a quiet box; a **functional** failure is real immediately.
    At `c211605`: 3 failed / 2165 passed under a concurrent live model run and another lane's suite, all
    three timing-or-budget, all 46 passing alone in 42 s. Without this clause the rule fails in a way
    that does not matter, trains everyone to discount it, and stops being a check.
- **Only lane leads commit and push. Nested sub-agents never do**, however finished the work looks.
- **"What measurement is this number from?" is a standing question at review, not a courtesy.**
  Three times on 2026-09-22 a threshold was set, or nearly set, without measuring what it bounds: the
  45 s end-turn confirm bound against an advance landing 75–155 s; a 5.5 s reconnect budget nearly
  widened against a 90 s observed window; and **the replacement 200.0 itself**, which clears every
  observed sample but whose ceiling is unmeasured and is labelled ASSUMPTION pending a direct probe.
  **Two of the three were caught only because someone asked for the measurement before the number.**
  **Converse, and it is the stronger half: when a number is unmeasured, the first move is not always to
  measure it — ask whether the fix needs the number at all.** A fix that removes the wait from existence
  beats a well-measured one that waits, because it cannot rot when the figure drifts. Worked example:
  the chain-leg refusal tail needs no measurement if the legs **reuse the session** and never reconnect —
  correct whether the tail is 2 s or 90 s. A measurement only constrains fixes of the form "wait long
  enough" or "retry enough times". Say so in the brief, or the fix gets "improved" into a retry.
- **A reconciliation pass closes every lane-day**: before reporting, verify each task claimed complete
  against the **code**, not against the agent's report, and tick or leave open with a reason. Twice on
  2026-09-22 a task was reported complete and was not, and both times the gap was found by reading the
  artifact. Ticking from an unverified report is the unearned claim this project keeps removing.
- **Check a claim in the form the artifact actually uses.** A `grep` for a prose sentence returned zero
  because the sentence **wraps across two lines** — the false-claim passage was still there, verbatim,
  and read as fixed. A negative search result is evidence only if the search could have matched.
- **A closing counterpart is required for every action that opens a modal or full-screen view**, and
  its verification must confirm the view closed. The harness opened a diplomacy session it could not
  exit: `CloseSession()` answers `ok: true` and does nothing, `IsSessionActive()` does not exist on
  this build, and a human closes the view with one Escape. An action that can strand the game is worse
  than one that fails.
- Host: never `pgrep -f`; `import` only under `timeout`; the tuner is one connection; popups are
  not persisted in saves; the UI lays out at 1024×768 (scale 1.875 × 1.5625 on 1920×1200).
- **A fixture the system under test keeps writing to is not a fixture**, and a test that skips at
  runtime has stopped testing (003 T053, 2026-09-22). **A method that exists is not safe from every
  context or on every object** — guard like Firaxis, from the context Firaxis uses (the segfault).
  **"Produces a value" is not "produces the right value"** — an empty list is a value.
- `pwsh` is absent: hand-derive the speckit helper JSON; do not edit `.specify/feature.json` while
  lanes run different features concurrently (it still points at 003).

## 🛑 Release-blocking, open (found 2026-09-22) — images are gated OFF by design
**The content screening gate cannot detect the FireTuner window in production, on any platform.**
`parity/screening.py`'s declared-text technique fires only when a category's tokens are a subset of
`detected_text_tokens`, which defaults to `frozenset()` and which **no production caller ever
supplies** — the one production call site is `run/decision_loop.py:486-498`. On the live Linux
profile only `debug_overlay` is detectable; `firetuner_window`, `developer_console`,
`harness_owned_ui`, `linux_panel` and `linux_notification_toast` are structurally invisible. This is
the enforcement path for FR-025, FR-030, SC-009 and SC-019, and SC-009 makes any finding
release-blocking. **287 model calls already carry an image, delivered through that gate.**
Sibling (HIGH): the source gate's process-identity check sits behind `expected_process is not None`,
also never supplied, so "this frame came from the game's own window" never runs on a real capture.

**Standing order until cleared by the hypervisor: the gate fails closed and images do not reach the
agent. Nobody re-enables image delivery by any route.** Play continues without frames; the harness
ran for weeks without them. A retro-audit of the 287 delivered frames runs from a read-only copy of
the store (`spikes/frame-retro-audit-2026-09-22.md`). T194 against the real client proves future
frames are clean; it does **not** absolve the delivered backlog — keep the two claims separate.

**Root cause adopted as a project rule:** *an optional parameter with a safe-looking empty default
(`frozenset()`, `None`) that every unit test supplies and the single production call site does not.*
Three of the day's four most serious findings have exactly that shape and all converge on one call
site. A structural check over `src/` for it is being added beside the reachability roster.

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
