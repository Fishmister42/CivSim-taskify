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
**Guarantee a safety property by the ABSENCE OF AN EDGE, not the correctness of a flag.** An
`operator_only=True` parameter makes Principle I rest on every future caller passing it right — the
"a rule is not a control" failure, demonstrated four times on 2026-09-22 including twice by the people
who wrote the rule. Instead build a path with **no delivery mechanism at all**: a capture whose result
has nowhere to go but the operator's eye and the artefact directory, structurally unable to reach a
provider request because nothing connects it. Ratchet it with an assertion that **no operator-capture
symbol is reachable from the provider-request path** — the reachability roster already exists here.
**A frame is the one observation that does not share the harness's failure mode** (on the `EndGameMenu`
board every harness-side signal agreed with itself and all were wrong), which is exactly why it must
never become an input to the thing it checks.

**The move that kept producing the better fix** (2026-09-22, four instances): **remove the dependency
rather than strengthen the thing it depends on.** Session reuse removes the refusal tail instead of
sizing a retry against it; the `atexit` backstop removes the need for terminal paths to funnel instead
of enumerating them; a fresh brief removes the need for an agent to authenticate a scope change
instead of improving its judgement; streaming the provider makes liveness a byproduct of the work
instead of tuning a tick interval. Each replaced a number, an assumption or a judgement with a
structure that cannot rot. **Ask it before measuring, before enumerating, and before writing a rule.**

**How to write one of these so it works** (2026-09-22): **a rule earns its place by replacing a
judgement with a lookup.** "Do you believe the lock fix is good?" gets a yes — the guard *is* good.
"Which line releases it on each exit path?" found the gap. The second question is narrower, duller,
and answerable without talking yourself into anything. Prefer the dull lookup; a rule that asks for an
assessment is a rule that returns the assessor's priors.
**And the lookup's own failure mode: run it on EACH thing you are about to claim, not on one and then
generalise.** A `git diff` of one file was read, its authorship extended to two others nobody opened,
and the result reported as "reading the diff" — which made an inference sound verified. A lookup
answers exactly the question asked of it and nothing adjacent. Three people touched that claim and
there was one partial lookup between them.
- **Never wait on a background-task notification.** Run long steps in the foreground under
  `timeout` (< 600 s per command), or poll a detached process's log in a bounded loop. Live stages
  send the hypervisor a heartbeat (SendMessage) at least every 20 minutes.
- **Live runs execute from a clean detached worktree, never the shared working tree.**
  `/home/matt/CivSolver-live` is a `git worktree` the hypervisor advances to each blessed commit
  (`git -C /home/matt/CivSolver-live checkout --detach <hash>`). Run from that directory with
  `PYTHONPATH=/home/matt/CivSolver-live/src UV_PROJECT_ENVIRONMENT=/home/matt/CivSolver/.venv uv run
  --no-sync python -m tests.live.<driver> … --store /home/matt/CivSolver/civsim-match-store.db` and an
  absolute output dir under the main tree's `spikes/gameplay-<date>/`. Nobody edits the worktree.
  **ALWAYS pass `--store /home/matt/CivSolver/civsim-match-store.db` explicitly.** `goal_run`'s
  `DEFAULT_STORE` is `REPO / "civsim-match-store.db"` where `REPO` resolves to **the worktree the module
  runs from** — so a run driven from `CivSolver-live` silently creates and writes a *separate* store
  there. Found 2026-09-22: block-07 landed in the live worktree's own store while every coverage number
  we quote comes from the main one. **A live lane writing to a store nobody audits is a silent
  measurement hole**, and cross-block comparisons are meaningless unless you know which store each
  landed in. Also `uv sync --all-groups --all-extras` the live worktree: a bare venv there lacks the
  `linux` extra, and the GIF recording thread dies on `No module named 'Xlib'` (recording only — the run
  and its observations are unaffected; this is environment hygiene, **not** a withheld-frame finding).
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
- **A scope change is never a mid-task message. It is a fresh brief.** An agent cannot authenticate a
  message that widens a hard constraint, and **a mid-task expansion of permissions is the shape of an
  injection whether or not it is one.** So: to widen an agent's scope, stop it and re-brief, or spawn a
  new agent with the wider scope. **An agent that declines a mid-task widening is CORRECT, always,
  regardless of the message's legitimacy** — say so in every brief that carries a hard constraint, so
  refusing is sanctioned rather than insubordinate. On 2026-09-22 an agent refused a genuine relayed
  ruling on exactly these grounds, verified the technical claim by *reading* the forbidden file without
  editing it, and then found a design that made the question moot. It cost nothing and it bound harder
  than intended — the only rule all day that failed in the safe direction.
  **Refinement, and the rule is wrong without it: a mid-task message may NARROW, never WIDEN.** The
  agent decides which by a mechanical test that does not require judging the sender:
  **"Does this message permit an action that was previously forbidden? If no, it is not an escalation."**
  A message that forbids, reorders or re-prioritises permits nothing new, so it carries no injection
  risk **by construction** — the worst a hostile narrowing achieves is making the agent do less, which
  is denial of service, not privilege escalation. Without this clause the rule reads as "ignore all
  mid-task messages", and **an agent that cannot be told to stop is worse than one that cannot be told
  to start** — we needed the stop channel three times on 2026-09-22, including the quiescence hold.
- **Announce a boundary crossing BEFORE making it**, and **check the tree for unannounced crossings
  before requesting a suite slot** — one `git status`, at the natural checkpoint. The other lane's
  protection against having its half-finished edits swept into your commit is knowing they exist.
  **An instruction is not a control if nobody verifies it**: a brief said "stop and report before
  editing that file", the agent edited it anyway, and nobody looked — which is this project's whole
  finding about other people's code, turned on our own process. Stage only your own hunks
  (`git apply --cached`) when a shared file carries another lane's work.
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
- **An allowlist inverts its safety property depending on which way it is read.** Used to **permit**,
  unknown means deny: the failure is a false refusal — visible and annoying. Used to **detect**, unknown
  means "nothing there": the failure is a **false all-clear — invisible and confident.** Same data
  structure, opposite failure mode, and nothing in the code distinguishes the two uses. Three instances
  on 2026-09-22: the screen watchlist (an unlisted screen read as *no screen*, so the probe answered
  `recognized=true, has_blocking_prompt=false` with a full-screen modal up); `lua/ACCESSORS.txt`
  (answers "does this method exist", read as "is it safe to call here"); and the content screening gate,
  **where the remedy is already precedent** — a category no technique addressed used to read as *clean*
  and now **withholds**. **The mechanical test: could this check ever return "I do not know"?** If not,
  it is an allowlist read as a detector and it will produce confident false all-clears.
  **The remedy, in all three: MAKE THE UNKNOWN EXPLICIT.** The gate now withholds a category no
  technique addresses; the probe must answer `unknown` rather than `world`; and **every observation body
  that `pcall`s an accessor must emit a `<field>_reason` when it could not answer, instead of omitting
  the field** — a nil Lua value vanishes from the JSON, and an absent field is indistinguishable from
  "legitimately not applicable". `882758e` already set that precedent, reporting
  `government_rows_without_hash` rather than a silent `[]`. A phantom becomes a stated gap.
- **Grade every close-out claim by evidence tier, never present them flat**: applied-and-verified
  (store record + reproduction) / applied-but-resting-on-an-assumption / not demonstrated / in flight
  and unproven / **retracted**. A summary that gives them the same confidence is this project's own
  pattern applied to its report. **Retractions stay in, attached to the lane that made them** — on
  2026-09-22 two of three were *measurement errors that looked like product defects*, and that rate is
  itself a finding: it is the strongest argument for the readback sweep, and a summary of only the
  survivors would present the lane as more reliable than its own evidence supports.
  **Put a correction WHERE THE CLAIM IS READ, not where corrections go.** An appended retraction at the
  bottom of a 1700-line ledger never reaches someone reading the entry it corrects. Correct in place,
  keep what was actually observed, and mark only the withdrawn consequence — for a published entry,
  prepend a marked block so the original text and its date still stand beneath it.
  **And name what HELD, not only what broke.** A finding feels like it needs recording and a success
  feels like the baseline, so a ledger drifts to defects-only — the same sampling bias as the refused
  column, applied to our own record. On 2026-09-22 a lane wrote up ten defects and zero successes
  before being asked. A record composed only of mechanisms that failed teaches that checks do not work,
  which is the opposite of the day's lesson. Write up a check that held with the same care as one that
  did not — and its remaining risk beside it, since praise without the caveat is how a working check
  gets broken.
- **A reconciliation pass closes every lane-day**: before reporting, verify each task claimed complete
  against the **code**, not against the agent's report, and tick or leave open with a reason. Twice on
  2026-09-22 a task was reported complete and was not, and both times the gap was found by reading the
  artifact. Ticking from an unverified report is the unearned claim this project keeps removing.
- **Check a claim in the form the artifact actually uses.** A `grep` for a prose sentence returned zero
  because the sentence **wraps across two lines** — the false-claim passage was still there, verbatim,
  and read as fixed. A negative search result is evidence only if the search could have matched.
  **Second half: a truncated search result is not evidence at all. Never pipe a confirming search
  through `head`/`tail`.** A `head -3` returned only definition sites and made a module-scope call look
  absent, nearly reversing a finding. Make it mechanical — the rule that says "notice when a count
  looks too small" needs judgement and will not fire; "do not truncate" is a lookup and will.
  **Third half, and it is the actual control: before trusting a negative grep, run the pattern against
  a case you KNOW is positive.** If it cannot find the thing already known to be there, its silence
  elsewhere means nothing. Four instances on 2026-09-22 shared this exact shape — *a negative result
  from a search whose pattern was never validated* — including one that cast doubt on another lane's
  correct work: the search was `log_event|logger`, the mechanism was `emit_confirm_liveness` in a
  dedicated `act/liveness.py`. A positive control for a search, same idea as a negative control for a
  check, and it dissolves all four.
- **A POSITIVE TWIN ONLY PROTECTS YOU IF IT VARIES THE DIMENSION THE VALIDATOR CONSTRAINS.** On
  2026-09-22 a validator for `implementation_ref` shipped with a positive case that varied the **path
  shape** and never the **capability kind** — so nothing in the test set declared a Python
  implementation, the twin could not fail, and the guard encoded the author's fixtures instead of the
  contract. It took the whole suite down at collection. **The same author had rejected an over-strict
  `lua/` prefix in that same validator hours earlier**, caught by six red tests; this one got through
  because the fixtures varied the wrong axis. **Ask what dimension the rule constrains, then build the
  twin along THAT axis.**
  **Worked example, three independent discoveries on 2026-09-22: counting running suites.**
  `grep -c "[p]ytest"` matches the **word** anywhere — other agents' monitor scripts whose text contains
  it, and the inspecting command itself. One lane polled ~15 min reporting 3–6 suites when one was
  running; the hypervisor's checkpoint gate waited 9 min **for its own launcher**, then its "fixed"
  version counted **its own polling commands**. **The correct matcher is `ps -e -o args= | grep -c
  "[b]in/pytest"`** — and that form already existed in the tree while two of us used the wrong one.
  Note why it hid: it failed **safe** (reported busy when clear), and **a fail-safe defect survives
  far longer than a dangerous one** because nothing goes wrong until you need the answer.
- **AUDIT THE FAIL-SAFE DIRECTION DELIBERATELY, because nothing else will make you.** The corollary is
  sharper than it first looks: a fail-safe defect survives **longest in exactly the mechanisms we trust
  most**, since a guard erring toward "not ready" or "withhold" never produces a visible incident.
  Three of 2026-09-22's findings share it — `firetuner_window` (withholds nothing while the coverage
  guard reports it addressed), the suite gate (busy when the box is clear), completeness's in-flight
  exemption (`complete` when data was lost). Each sat all day; the dangerous ones were found in minutes.
- **Distinguish a record WORSE than the truth from a record BETTER than it.** Nearly every finding on
  2026-09-22 under-reported — a real action recorded refused, a turn caused and not credited. Those are
  recoverable: the record can be corrected upward. **A predicate that evaluates True against an ABSENT
  field records a no-op as `applied` — and nothing in the data would ever reveal it.** Under-reporting
  is a measurement problem; fabricating is a credibility problem that poisons the sound claims too.
  **So for every predicate over a possibly-absent field, record which way absence resolves: False
  (under-reports, visible) or True (fabricates, invisible). The True list is the one that matters.**
  Live instance: `declare_war`'s `== "war"` under-reports; `make_peace`'s `!= "war"` on the same absent
  field fabricates, and is unreachable today only because availability reads that field first — **so
  fixing the context without fixing the predicate shape converts it into a silent liar.**
- **Beware the fix that manufactures its own confirmation.** Raising the action-confirm bound would
  have "fixed" `cities.set_production`'s 110 failures, and the improvement would have been read as
  proof the bound caused them — while the real defect (wrong dispatch parameters; +1 s when correctly
  parameterised) stayed in place. A wrong fix that moves the number is worse than none.
  **What makes it dangerous rather than merely wasteful: the number moving is taken as evidence for the
  hypothesis that motivated the fix, so the wrong explanation gets CONFIRMED and the question closes** —
  the real defect then sits behind a green metric with a documented cause. **The defence: predict the
  specific observable before making the change.** A movement that matches the prediction counts; a
  movement that merely goes the right way does not.
- **OPEN, HIGH: the store may misattribute which provider served a call**, and it is a **provenance**
  defect, not a cost one — cost is merely where it surfaces. `run-e8c96e29c…` is recorded as served by
  `openrouter/anthropic/claude-sonnet-5`, one call, unpriced, while its own `results.json` says
  `provider: fake`. **If the store cannot say whether a model or a stub made a decision, a coverage
  number built from those runs is not measuring what it claims.** It cuts both ways — a fake recorded
  as paid inflates spend, a paid recorded as fake understates it — and **nobody has established the
  direction**. Every spend figure quoted on 2026-09-22 carries this caveat until settled.
  **And the free blocks have MORE at stake than the paid ones**, not less: the whole argument for
  `--provider stochastic` is *"the claim under test is applicability, not whether a model chose it"*,
  which holds only if the record reliably says a stub was in the loop. **A stochastic block
  misrecorded as a model call is a Principle I question, not an accounting one — a decision attributed
  to a model that no model made.**
- **A BOUND THAT LIVES IN A LOCAL IS RENEWED BY RE-ENTRY. Sweep for allowances scoped narrower than
  the thing they bound.** Two independent discoveries in two modules on 2026-09-22, found hours apart
  by different agents: `chain.py`'s attempt ladder lived in a local, so re-entering `complete_step` for
  one step handed it a fresh ladder (fixed, `65bc562`); and `NoProgressTracker` is a per-invocation
  local while the decision loop runs **twice** for a turn cycle's first attempt (prompt clearance, then
  the attempt), both step lists concatenated under **one `turn_cycle_id`** — so one recorded turn can
  run two complete 8-step ladders and publish a streak of 8. Measured: a cycle with **16 consecutive
  rejected end-turns** under `limit=8`, climbing 1→8, restarting, climbing again; **61 trip events
  across 54 distinct (run, turn) pairs, 8 of them represented by no cycle outcome at all.** This is the
  guard-scope mismatch in its most sweepable form: **find every counter, budget or ladder held in a
  local, and compare its lifetime to the lifetime of the thing it is supposed to limit.**
- **A MEASUREMENT IS ONLY AS CURRENT AS THE TREE IT WAS TAKEN IN — re-measure before escalating.**
  On 2026-09-22 "HEAD is functionally red, ten failures" was escalated as blocking every lane. **The
  redness was real and the commit was wrong:** it was measured in a worktree pinned four commits behind
  HEAD, inside a red interval that two later commits had already closed. `bbe69e8`'s validator rejected
  15 fixtures declaring `implementation_ref="test"`; `36aa948` fixed them **two commits before HEAD**.
  HEAD verified green twice, 1363 passed. **A worktree's name is not its hash** — the one called
  `headnow` was at `8420dbc`. Check `git -C <wt> rev-parse HEAD` before quoting any result from it.
- **`decision_loop.py`'s docstring makes an absolute claim its own file contradicts.** It says there is
  *"no wall-clock check anywhere in this module, and there must never be one added"* — while
  `END_TURN_CONFIRM_TIMEOUT_S = 200.0` (`:233`) and `ACTION_CONFIRM_TIMEOUT_S = 4.0` (`:237`) live in it,
  both predating the window. They do not violate I16 **as the test operationalises it** — they bound how
  long *one dispatched action* is re-read, not how long a turn may run, which is why the 500-step test
  passes. **The detached-plus-poll ruling survives on its own merits (a SIGKILL is the leak path with no
  in-process fix); the reason given for it that morning — "a confirmation timeout cannot live in this
  module" — was false.** Another docstring standing in for a mechanism, this one asserting an absolute.
- **A MECHANISM THAT EXPLAINS THE EVIDENCE IS NOT THE MECHANISM THAT PRODUCED IT.** On 2026-09-22 a
  "deterministic suite hang" was escalated as blocking: **byte-identical truncation at ~60% across two
  runs with different outer budgets (870 s, 890 s)**, presented as ruling out contention. It does not —
  it rules out *those budgets*. **A fixed inner `timeout 590` kills at 590 s regardless of the outer
  budget and produces byte-identical truncation every time**, which is also exactly what a deadlock
  looks like. **The observation could not discriminate and was offered as the evidence that did.**
  `EXITCODE:143` (128+15, SIGTERM) settled it, and `tests/unit` ran **1294 passed with the suspect file
  included** — impossible if it deadlocked. A plausible mechanism was supplied and presented as
  investigation. **Before escalating, reconcile every report you already hold about the same artefact.**
- **TWO CORRECT RULES CAN PRODUCE A WRONG OUTCOME IN THE GAP BETWEEN THEM — and reviewing either in
  isolation finds nothing.** A category distinct from "a check that cannot fail": there, one mechanism
  is defective; here, **no individual rule is wrong and the result still is.** Worked example below —
  `timeout 900` in the accepted test command against "foreground commands stay under 600 s", split by
  agents at 590, against a suite that had outgrown it, producing a false *blocking* escalation.
  **So audit rules in PAIRS where they touch the same action**, and when an agent silently reconciles
  two of your rules by splitting the difference, that split is a finding — ask what it reconciled.
- **The suite has outgrown a 590 s wrapper.** A clean run is ~250 s; under a live client at ~89% of a
  core plus concurrent suites it exceeds 590. **My own rules conflicted** — the accepted command says
  `timeout 900`, the no-waiting rule says foreground commands stay under 600 s, and agents split the
  difference at 590 and manufactured a hang. **Run it detached with a polled log, or split:
  `tests/unit` then `tests/integration tests/contract`** (162 s and 173 s clean). **And a figure
  obtained with `--ignore=<file>` is NOT a full-suite green** — the exclusion is invisible in the number.
- **A NUMBER THAT MOVES ON ARGUMENT IS OSCILLATING, NOT CONVERGING. When a count is challenged,
  re-derive it from the artefact — do not reason about it.** On 2026-09-22 "actions proven to have
  landed while scored rejected" went **4 (unverified) → 2 (on a caveat) → 3 (on a relay) → 4
  (verified by walking unit positions out of each step's own observation)**. Every intermediate move
  rested on a **locally correct** argument — the move-legality objection was real, the "no later read"
  objection was reasonable — and **only the last step was a lookup.** The store had the answer
  throughout, in a column already queried twice for other purposes. Two of the four values were
  published before anyone read it.
- **AGREEMENT IS NOT INDEPENDENCE. Ask what each source MEASURES before counting it as corroboration.**
  On 2026-09-22 four sources agreed the 4 s action-confirm bound was wrong by an order of magnitude —
  and a sub-second probe reversed it. **They agreed because every one was a lower bound produced by
  something giving up**, and one of them (the owner's "30–60 s per interaction") was measuring a
  human-visible cycle dominated by 48.5 s model calls, not an action's settle time. Select and move
  actually settle in **under 0.17 s, observed on poll #1** — the instrument's floor, not the game's
  speed. Four numbers pointing one way *felt* like corroboration; sharing a failure mode is the one
  thing that makes agreement worthless. **The reversal is scoped: do NOT raise `ACTION_CONFIRM_TIMEOUT_S`
  uniformly — its classes differ by two orders of magnitude. It does NOT retire the 200 s end-turn
  bounds**, which rest on measured regimes (11.5–16.9 s fast, 75–155 s slow) and a genuine wait on every
  AI player. Someone reading "the bound work is suspended" would otherwise revert a supported change.
- **Gate on what the measurement is FOR.** A **functional** pass/fail is not load-sensitive: it needs
  only isolation and a pinned head. A **timing** result is, and no waiting rescues it here — 248 s
  loaded against ~225 s quiet is ~10% noise for pass/fail and meaningless against a 3.4× spread.
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
- **A wall-clock timeout kills a run mid-turn with no regard for what is on the board, and the
  board's value is not uniform.** Block 28 (2026-09-22) was healthy — nine turns, longest gap 105 s,
  last gap 41 s — and its outer `timeout 580` cut it off at turn 9 of a higher cap, **114 seconds
  after it had accepted `prompt.congress_intro`**. That cost `congress.cast_vote` on
  `prompt.congress_vote`, a never-attempted action on a never-encountered screen, on the one
  occasion both were reachable. **Stop a driver cleanly; never let its own outer timeout be the
  thing that ends it.** A timeout kill also orphans the run lock every time (the SIGTERM/atexit
  defect), so the board stays held after the process is already dead.
- **Absence in the log is not absence in the run.** I grepped a block's stdout for `congress`, found
  only the Lua context dump, and nearly called a subagent's true claim fabricated. The stdout carries
  progress lines and nexus warnings; **step records live in the store**. Check the instrument that
  would hold the evidence before concluding the evidence does not exist.
- **🔴 RETRACTED 2026-09-22 15:20 — the escalation below was wrong, and the way it was wrong is the
  lesson.** The claim was that `0989e3b` populating `researchable_civics` opens a fabrication path,
  because `research.set_civic` verifies `player.current_civic == target` and `None == None` is True,
  guarded only accidentally by the field being permanently `[]`. **The mechanism does not hold:
  `None in ["CIVIC_X"]` is False exactly as `None in []` is**, so the availability gate
  `target in player.researchable_civics` refuses a null target whether the list is empty or full.
  **The twin settles it and was in the store the whole time:** `research.set_tech` has the identical
  predicate pair, a populated `researchable_techs`, 70 applied, and **15 null-target decisions all
  refused at dispatch**. **The error was reasoning forward from a mechanism instead of looking up its
  already-unlocked twin** — do the lookup first; this project keeps paying for that one. `0989e3b`
  stays gated on the **segfault** reason, which was always the stronger of the two.
  **The `==`-against-null shape is still real as a fabrication axis** (T310 covered `!=`, `not in`,
  `not X`; T311 the left operand of `in`/`not in`; neither asked about `==`), and the central fix
  landed anyway at `d74a4c7`: **a comparison either of whose operands went missing is unevaluable,
  for every operator, in every declaration.** It **raises rather than returning False**, which is
  load-bearing — a False inside `not (target in X)` is negated straight back into a fabricated True,
  while unevaluable propagates out through the negation. **25 fabrications closed, three of them
  instances neither T310 nor T311 had enumerated**, found only because the fix was enumerated rather
  than targeted. An explicit `null` literal on either side is exempt, because the catalog needs to
  ask about absence **on purpose** (`spy.mission != null`) and the first draft destroyed that idiom.
- **A positive control that production cannot reach is not a positive control.** `camera.zoom`
  verifies `camera.zoom == target` and the engine answers `0.049999713897705` for a requested `0.05`,
  so working zooms record as refused (T321). Nothing was red because
  `tests/unit/test_predicates.py:850` **is** a positive control and **passes** — it compares `0.5`
  to `0.5`, an exactly-equal float the engine never produces. **FIXED 2026-09-22 (headless):** the
  tolerance is declared in `catalogs/actions/camera.yaml` itself (`camera.zoom - target <= 0.001 and
  target - camera.zoom <= 0.001`), never as an evaluator epsilon — a tolerance is a number someone
  can quietly widen, so it belongs where a reviewer diffs it. No evaluator change: `_eval_binop`
  already raises on a non-numeric operand, so absence stays **unevaluable** exactly as `d74a4c7`
  made it. Magnitude against the measurement: largest error on a zoom that LANDED 4.768e-07,
  smallest on one that genuinely did not 0.293 — six orders of magnitude apart.
  **🔴 THREE CORRECTIONS to the counts as first written, all found by replaying the store:**
  1. **The three camera actions are three different defects, not one.** `camera.zoom` is the float
     bug (39 of 47 confirm-failures had the camera ON target). **`camera.set_view_mode` compares
     STRINGS** — 66 of its 69 refusals replay **True** at HEAD, so a float tolerance cannot touch
     it and folding its 69 into T321 would be the same error as folding in `camera.move`.
  2. **None of the recorded camera refusals were caused by the float.** Every one of the 118 camera
     steps in the store was produced by a tree in which `build_predicate_bindings` hardcoded
     `"camera": {}` — the live worktree sat at `c211605` until ~15:14, and `44e8d01` (T308, the
     camera binding) is not its ancestor. So those counts are **T308's defect**, already fixed; the
     float defect is established **by reproduction at HEAD**, not by them. Quote it that way.
     **Falsifiable prediction: `camera.set_view_mode` should start applying from the next block run
     after 15:14. If it still records False, the binding is not the cause and the finding is live.**
  3. **`camera.move`'s 48 refusals are NOT demonstrably "the agent asked to look at fog."** The
     recorded targets are bare `{"x": .., "y": ..}` and **no observation schema in the catalog emits
     a plot carrying `is_revealed`**, so `target.is_revealed` resolves `None` → falsy, always. The
     refusal direction is correct and fail-closed, but the *reason* is a field the predicate never
     had, not a fogged plot — and the phantom-field ratchet cannot see it, because
     `UNCHECKED_PREDICATE_NAMESPACES` skips the whole `target` namespace by design. **Still out of
     scope for T321; now open on its own terms.** A fail-safe phantom, which is why it survived.
- **A query that returns the same empty answer for your control as for your subject is broken, not
  conclusive.** A sweep read `outcome: None` for everything including the control, because the
  execution record nests under `decision` rather than beside it. **The control caught a broken query
  that would otherwise have read as a dramatic finding.**
- **🛑 A Lua pattern class matches BYTES, and `%c` is locale-dependent, so it eats UTF-8.**
  `value:gsub('[%c"\\]', ...)` at `lua/ingame/great_people.lua:31` **and 26 other copies** escapes any
  byte `iscntrl()` accepts, which under the client's locale includes **C1, `0x80–0x9F`** — and UTF-8
  continuation bytes live in `0x80–0xBF`. **The two ranges overlap and the frame is severed.**
  Measured: the Great Artist **Kamāl ud-Dīn Behzād** breaks every observation read with
  `UnicodeDecodeError: can't decode byte 0xc4`. `ā` is `C4 81` — the trailing byte **is** C1, escaped
  to literal `\u0081`, lead byte left raw → **broken**. `ī` is `C4 AB` — trailing byte is not C1 →
  **survives**. **Same name, same frame, which makes it its own discriminating control.**
  Any game string with a byte in `0x80–0x9F` kills the tuner connection, so most of Latin Extended-A
  is a live hazard: leader, city, city-state and great-person names. **Board-state dependent**, which
  is why three runs died on it today and none before: the name entered the roster mid-block. **Not
  caused by the advance** — `nexus/codec.py` is unchanged across it and block 31 hit it at `c211605`.
  Fix: restrict the class to ASCII controls so bytes `>= 0x80` pass through, **as one shared helper,
  not 27 edits**. Trap: `%z` vs `\0` differs between Lua 5.1 and 5.2.
- **The path that records WHY a run failed must not be able to fail the same way the run did.**
  When the read above raised, `run/runner.py`'s error-recording path raised the **same**
  `UnicodeDecodeError`, leaving the run `lifecycle_state: playing` forever with its driver polling a
  status that would never change. **"Still playing" and "died and could not say why" must not share a
  representation.**
- **`nm -DC` silence is not evidence that an accessor is unbound.** No `l*Favor*` symbol exists on any
  player interface, yet `strings -a` finds `GetFavor` and Firaxis' own shipped XP2 UI calls
  `Players[id]:GetFavor()`. **Some Lua bindings exist only as name strings with no exported
  trampoline.** The symbol table is sound as a *positive* discriminator (that is how `CanProgress`
  was settled) and **unsound as a negative**. Do not run a sweep that reads silence as proof.

## 🛑 Release-blocking — ⚠️ THE STATED MECHANISM NO LONGER DESCRIBES `0e91a10` (re-read 15:55)
**Do not act on the section below without checking which head you are on.** At `0e91a10`, which the
live worktree now runs, **both halves of the defect are closed**: `0e91a10` carries T265, which
supplies the declared-text tokens at `run/decision_loop.py:513`, and `observe/capture.py:309` now
resolves `expected_process` itself so the source gate's identity check runs on a real capture.
Measured on this box: `list_window_titles()` returns 14 windows and 34 tokens (not None), every view
declares `screening_profile: platform`, and **`unaddressed_reject_categories(linux/platform) == []`**
— so the fail-closed rule no longer withholds, and **image delivery may be OPEN**.
**NOT established: whether a real frame then clears the provenance, geometry and image checks.**
Zero captures exist at this head. Block 31's 18 frames all failed `provenance_failure` at the
*previous* head, and `act/camera.py`, `lua/ingame/camera.lua` and `views.yaml`'s `hud_corners` all
changed in the advance. **GATE: before any block runs at this head, read ONE capture and establish
what happened to it.** Treat delivery as plausibly open until then.
**Also note P2 could never have discriminated**: block 31, at a head *without* `483017c`, already
showed `shown_to_agent {False: 18}` and `sum(image_count) 0`. **The presence of a withheld reason
cannot separate the content gate from the provenance gate — only the reason's value can.**

### The original finding, as recorded (true of `c211605` and earlier)
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
