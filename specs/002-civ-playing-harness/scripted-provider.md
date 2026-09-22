# The scripted decision provider (T326)

**Status:** implemented 2026-09-22. Code: `src/civsim_harness/provider/scripted.py`,
`src/civsim_harness/models/provenance.py`, the tier split in
`src/civsim_harness/store/coverage.py`. Tests: `tests/unit/test_scripted_provider.py`,
`tests/unit/test_scripted_coverage_separation.py`. Example script:
`tests/live/scripts/builder_from_capital.yaml`.

**Not yet run against a live client.** Everything below is a claim about the code and the suite.
Nothing here is an observation of the game.

---

## The problem it exists to solve

The harness has three decision providers selected by `goal_run --provider`: `fake`, `stochastic`,
`openrouter`. Only `openrouter` consults a model. `fake` and `stochastic` are stubs whose own
recorded reasoning ends *"No model was consulted and no information outside this request was
read."*

Goals (`--goal <id>`) carry an objective written in English plus a success predicate. **The
objective is only ever read by a model.** With a stub provider the objective is inert: `--goal`
selects how the run is *scored*, never what the run *does*. A stub samples the action catalog and
the goal grades whatever happened.

The consequence, MEASURED on the live board 2026-09-22: the game was at turn 65 with one city and
zero units. Five of thirteen goals were blocked on having a unit (`found_second_city`,
`move_unit_to_plot`, `select_city_then_unit`, `promote_unit`, `use_a_builder`). Producing a unit
requires the chain `cities.select` -> `cities.set_production(UNIT_BUILDER)`. Under the coverage
policy, **`cities.select` was drawn 0 times in 133 world-screen steps.** Model play was blocked
for separate reasons (the owner-gated image-delivery gate, plus a retry wedge). So the board could
not reach a unit by any available means, and a large part of the action catalog was not merely
undemonstrated but **untestable**.

A unit did appear on the board at game turn 70 by some route the harness did not perform: the
capital's production queue read empty at every observation including that turn, and no production
action landed. **That arrival is not evidence the chain works.** It is the question the script is
here to answer.

## What it is

A provider that executes a **fixed, declared action sequence** through the harness's own dispatch
path. Its purpose is **harness capability testing** — proving that an action chain works end to
end — not gameplay.

## Why this is legitimate, and not an operator taking the agent's decisions

This reasoning is load-bearing. It is preserved verbatim in the module docstring of
`provider/scripted.py`, where anyone reading the code will meet it.

- An operator clicking in the game client **bypasses the harness**. Nothing is exercised, nothing
  is recorded, and the only thing proven is that the game works. A scripted provider **exercises
  all of it**: the action's declared `availability_predicate`, argument normalisation, dispatch
  through the capability executor, the declared `verification_predicate`'s re-read of the board,
  and the store write.
- **The only thing supplied from outside is the choice.** The mechanism stays entirely ours,
  which is exactly what a capability test must measure. A test that also supplied the mechanism
  would be measuring the operator.
- **Principle I (human-parity, NON-NEGOTIABLE) is untouched.** A script grants no capability a
  human lacks: every step names a catalogued action, every action still passes its own
  availability predicate against the live board, and every dispatch still goes through the
  human-parity path. A declared step for an action the game is not offering is refused, loudly
  and on the record, exactly as an agent's own such choice would be.
- **Principle IV (seeded, reproducible experimentation) is served.** A declared sequence is
  strictly more reproducible than a seed: re-running the same script against the same save
  reproduces the same decisions without depending on a sampler's stream, a policy version, or
  what the board happened to render.

## The non-negotiable guardrail

**A scripted landing proves the ACTION works and says NOTHING about whether an agent would choose
it.** Scripted landings are recorded in a separate tier and never enter the breadth/coverage
scorecard as applied-by-choice.

### Where provenance is recorded, and why there is no new flag

Every call the adapter serves reports `model_served = scripted/declared-sequence-v1`
(`models/provenance.SCRIPTED_MODEL_REF`). `ProviderChain` carries that through
`dataclasses.replace` without touching it; `provider/accounting.build_model_call` copies it
verbatim; the store persists it both in the `model_calls` table and inside the step's own
`bundle_json`. It is therefore **per-step, durable, and never inferred from a run configuration**
that might be missing or edited.

A new `Decision.decision_provenance` field was considered and **deliberately not added**. It
would have needed a default to keep already-recorded bundles readable, and a defaulted provenance
flag is exactly this project's worst-known defect shape — *an optional parameter with a
safe-looking empty default that every unit test supplies and the one production call site does
not*. A scripted decision that forgot to set it would have recorded itself as agent-chosen: a
record **better than the truth**, which is the invisible, credibility-destroying direction.

Deriving from `model_served` has no absent case at all, so
`models/provenance.provenance_of` is a **total** function with no "unknown" branch to resolve
wrongly. And a `Decision` cannot be separated from its `ModelCall`:
`store/port.DecisionStepBundle._cross_record_consistency` refuses to hold a decision whose
`model_call_id` disagrees with the model call beside it. That invariant already existed; this
design only reads it.

The one remaining failure mode points the safe way. For a scripted landing to be *misread as
chosen*, the adapter would have to fail to stamp the name — impossible, because
`DecisionResponse.model_served` is a required field of a frozen dataclass set from a module
constant on the single response the adapter ever constructs (asserted across all four of its
exit paths). The opposite error — some other adapter naming itself `scripted` — would
**under-report** the agent's breadth, which is visible and recoverable.

### Why the scorecard separation is structural, not a convention

The rule that matters here is **"a rule is not a control"**: a safety property should hold by the
absence of an edge, not the correctness of a flag.

`store/coverage.py` previously accumulated every step into a single `_Tally`. Filtering scripted
steps out at each call site would have made the property depend on every present and future
caller remembering to filter. Instead:

1. There are **two separate accumulator objects** — `_Tally` (chosen) and `_ScriptedTally`
   (scripted) — held in a `_Sinks` pair. They share no counter.
2. `_walk_run` resolves `provenance_of(bundle.model_call)` **once per step**, at the single place
   a step is read, and hands it to exactly one of the two. There is no third branch and no
   fall-through.
3. The chosen tier's rows are `ActionCoverage`. The scripted tier's rows are
   `ScriptedActionCoverage`, **a different type with no `applied`, no `attempts` and no
   `demonstrated`**. The two idioms that produce a coverage headline —
   `sum(row.applied for row in rows)` and `sum(1 for row in rows if row.demonstrated)` — raise
   `AttributeError` against it rather than quietly returning a mixed number.
4. `CoverageScorecard.headlines()` is computed only from `actions`, `observations`, `views`,
   `screens` and `images`, none of which a scripted step can reach. The scripted tier has its own
   line, worded so it cannot be quoted as a coverage claim.
5. `RunCoverage.actions_applied` keeps its name and narrows to the chosen tier, so a caller that
   has read that field since before scripted runs existed keeps getting the number it always
   meant. `scripted_landings` and `decision_provenance` are the new, separate fields.

The consequence that matters: **someone adding a new count next month, who has never heard of
scripted runs, gets a correct number by construction.**

The failing test written first — `test_a_scripted_landing_is_not_counted_as_a_chosen_landing` —
read `AssertionError: assert 1 == 0` against
`ActionCoverage(declaration_id='cities.select', applied=1, ...)` before the split existed. Its
positive twin varies **only** `ModelCall.model_served.provider` and nothing else: same fixture
function, same action list, same outcomes, same turn shape. A twin that varied the action id or
the turn number would have passed no matter how the split were implemented.

### What is deliberately NOT split

Observations, screens, watched states and the capture records themselves are counted across every
step, scripted ones included. Those are claims about the harness's **read** path, and a scripted
run exercises it identically. Excluding them would under-report what the harness has been shown
to do — the visible, recoverable direction. Counting a scripted landing as a chosen one would
over-report, which is not. Images **are** split, because "images delivered to the agent" is a
claim about an agent reading a picture and no agent read anything on a scripted step.

## Declaring a script

A script is a YAML file, fully validated against the catalog **at load time** by
`load_action_script`. Everything that can be wrong with it is wrong before a run starts: an
unknown `declaration_id`, a declaration that is an observation rather than an action, a target of
the wrong shape for the action's declared `target_kind`, a target supplied to an action that
takes none, a parameter key other than `target`, an unknown or missing `on_unavailable` policy,
`attempts_per_step` where it means nothing, an empty step list. Each raises
`ScriptValidationError` naming the offending step by index. Nothing is corrected, defaulted or
guessed.

```yaml
script_id: builder_from_capital
on_unavailable: retry          # required — halt | continue | retry
attempts_per_step: 6           # required for retry, refused for the other two
steps:
  - declaration_id: cities.select
    parameters: {target: 65536}
    note: open the capital's panel
  - declaration_id: cities.set_production
    parameters: {target: UNIT_BUILDER}
  - declaration_id: turn.end_turn
    repeat: 8                  # expanded at load into 8 concrete declared steps
```

    uv run python -m tests.live.goal_run --goal build_a_builder --provider scripted \
        --script tests/live/scripts/builder_from_capital.yaml --turns 12 OUT_DIR

**A script has no empty form.** `ActionScript` requires `steps` with no default and raises on an
empty sequence; `ScriptedModelProvider(script)` takes it positionally with no default. A
scriptless scripted provider — which would silently behave as "end every turn forever" — is not
constructible. `build_provider("scripted")` raises `ValueError`, and `goal_run` refuses the flag
combination at argument-parse time, before the tuner is even connected.

## When a declared step's action is unavailable

**Never a silent skip.** In every policy the action is issued at least once regardless, so the
harness's own dispatcher records the refusal with the game's own reason. The declared policy only
decides what the *script* does next:

| `on_unavailable` | after the refusal is recorded |
|---|---|
| `halt` | stop the script; every later call returns the end turn, stating where it stopped and how many declared steps were never reached |
| `continue` | advance to the next declared step |
| `retry` | keep the cursor on this step for up to `attempts_per_step` issues in total, then halt |

`retry`'s bound lives on the provider, which lives for the whole run — not in a local that a
re-entry would hand a fresh ladder (the guard-scope defect found twice on 2026-09-22).

## Absence and unobservability do not share a representation

A declared step that was **issued and refused** leaves a record in the store. A declared step the
script **never reached** leaves none — and an absent record is otherwise indistinguishable from
"ran and did nothing". So it is stated explicitly, in two places:

- `ScriptedModelProvider.ledger()` carries one row per declared step with an explicit
  `ScriptStepStatus`: `pending` (still running, not yet its turn), `issued_available`,
  `issued_unavailable` (with the reason the request gave — itself distinct from
  `NOT_LISTED_REASON`, "this request did not list the action at all"), or `not_reached`.
  `pending` never reads as `not_reached` while the script is still running. `goal_run` writes the
  ledger, its summary and an explicit scripted-landing warning into the goal's `results.json`.
- Every decision the provider returns after the script stops carries the ledger summary in its
  own `reasoning` — *"script 'X' HALTED at declared step 3 of 10 (cities.set_production) because
  …; 7 later step(s) were never reached, and nothing was observed about them"* — so the **store**,
  not only the process's memory, holds the statement.

## Known limitations, stated rather than discovered later

- **The cursor is monotonic across the run and never rewinds.** A script is a sequence of
  decisions for a run, not for a turn. If the harness abandons and replays a turn attempt, the
  steps already issued stay issued. The ledger records what was actually sent, which is the
  honest record; reconstructing what a fresh attempt "should" have sent would record something
  that did not happen.
- **It does not answer blocking prompts of its own accord**, because that would be a choice it
  was not given. A script that expects a prompt declares the answering action as one of its own
  steps. A prompt nothing answers will refuse the current step; the declared policy then decides,
  and the harness's no-progress backstop ends the turn. Under `retry` this consumes
  `attempts_per_step` issues, so a prompt at the wrong moment can halt a script — the halt is
  explicit and on the record, which is the behaviour we want from a capability test.
- **Board-specific targets are the script author's responsibility.** `cities.select` needs the
  capital's own `city_id`; `cities.set_production` needs an item from *that* city's
  `available_productions`. A wrong value is not silent — the action is dispatched and its own Lua
  answers `city_not_found`, and the refusal lands on the record — but it costs the step.
