# Catalog Authoring Guide

How to add a new observation, view, or action declaration to `catalogs/`, and what happens if you
get it wrong. This is written from what this project actually hit while authoring the first 23
capabilities and their declarations — not generic advice about writing YAML.

The fixed reference for the predicate grammar and field vocabulary is `catalogs/README.md`. This
document is about the *process* of adding a declaration; that one is the *dictionary* you write
predicates against.

---

## 1. Start from the parity basis, not the Lua

Every declaration (`catalogs/observations/*.yaml`, `catalogs/observations/views.yaml`,
`catalogs/actions/*.yaml`) requires a `parity_basis`: a plain-language statement of what a human
player does in Civilization VI's **standard UI** to obtain the same information or perform the same
effect. For example, `turn.end_turn`'s is:

```yaml
parity_basis: >
  Click the end-turn button in the lower-right action panel, or press its hotkey.
```

Write this **before** you write a line of Lua. The order matters for the same reason Principle I
(Human-Parity Information & Action Boundary) is non-negotiable: a capability that already has
working Lua is much more tempting to declare than one that doesn't, and "does this actually match
something a human can do" gets asked less rigorously under that pressure. Asking it first, with no
implementation yet to defend, is the whole point.

**If you cannot write a truthful `parity_basis`, do not declare the capability.** That is the
correct outcome, not a gap to work around. If the data or action has no standard-UI equivalent —
it's debug-only, it's AI-internal state, it's something only Firetuner itself exposes — the answer
is "this capability does not belong in the catalog," not "write a `parity_basis` that's technically
defensible." A declaration with a hand-wavy or aspirational `parity_basis` is a live-fire violation
of Principle I waiting to be exercised, not a paperwork gap. `catalogs/README.md` §3.5 says the same
thing about the Lua source files themselves: they must never read unrevealed fog-of-war contents,
opponent internal state, hidden AI intent, undisclosed research/civics, RNG state, or debug data —
the source files are part of the parity boundary, not just the catalog entries pointing at them.

## 2. The seven load-time validations — and why they abort startup, not turn 1

`src/civsim_harness/capability/loader.py` (`load_catalog`) enforces seven checks on the whole
catalog tree every time it loads:

1. Every declaration has a non-empty `parity_basis`.
2. Every `capability_id` a declaration names resolves to an entry in `catalogs/capabilities.yaml`;
   every `path: bespoke` capability has a non-empty `firetuner_gap`.
3. `declaration_id` values are unique across every file.
4. Every action entry has both `availability_predicate` and `verification_predicate`.
5. Every observation/view entry has a valid `output_schema`.
6. Every predicate references only symbols the restricted evaluator actually exposes (the table in
   `catalogs/README.md` §4).
7. `catalogs/VERSION` is present, and the loader computes a content hash over every catalog file
   plus the resulting `declaration_ids` set.

Validations 1, half of 2 (the bespoke/`firetuner_gap` rule), 4, and the presence half of 5 are
structurally enforced by the `ParityDeclaration` and `IntegrationCapability` pydantic models
themselves — a malformed entry cannot even construct as one of those types. The loader wraps that
failure, and separately checks the things a single entry's own shape can't: cross-file
`declaration_id` uniqueness, `capability_id` cross-resolution, the deeper shape of `output_schema`,
the predicate symbol-table gate, and `VERSION`.

**Every one of these failures aborts startup, not turn 1.** A catalog defect discovered mid-run —
after the agent has already been fielding decisions against a broken or unauthorized declaration —
is a defect discovered too late. `load_catalog` raises `CatalogError` on the first failure it finds
and is called from run preflight; nothing downstream of it recovers from a validation failure. If
you add a declaration with a typo'd `capability_id`, or reuse an existing `declaration_id`, or write
a predicate referencing a namespace §4 doesn't define, the run refuses to start at all — `doctor`'s
`catalog: N undeclared` line and `0 undeclared` gate exist for exactly this.

## 3. `path: firetuner` is the default — `path: bespoke` needs a spike, not an assertion

Every `IntegrationCapability` in `catalogs/capabilities.yaml` declares a `path`: `firetuner` or
`bespoke`. As of this writing, **all 23 capabilities in the catalog are `path: firetuner`** — the
project has never yet needed a bespoke path for anything it declared.

Per Principle II (Firetuner-First, Skill-Extensible Harness), `firetuner` is not just the default —
it is what you reach for first, and `bespoke` is the exception that needs justifying, not the other
way around. A `path: bespoke` capability requires a non-empty `firetuner_gap` field, and the model
layer enforces that emptiness check structurally (validation 2 above). But an empty-string check is
a weak bar — the real requirement is stronger than "the field is non-empty":

**The `firetuner_gap` must be documented evidence from a spike, not an assertion.** "I couldn't find
a Lua call for this" is not evidence; a spike that actually probed the candidate namespaces under
`pcall`, recorded what came back, and reached a verdict is. Writing "no known Firetuner path" into
`firetuner_gap` without having actually tried is exactly the failure mode this rule exists to
prevent — it would let an untested assumption quietly become the reason a bespoke, harder-to-audit
control path shipped.

### The worked example: T077 / research R5

The save-game capability is the project's own cautionary tale here, and it cuts the other way from
what you'd expect. `contracts/capability-catalog.md`'s worked example named `saves.save_game` as
*the* example of a bespoke capability, on the assumption that Firetuner's Lua sandbox had no way to
trigger a save — screen/input automation looked like the only option.

T077 (research R5) ran the spike **before** any save-path work started, rather than after: a raw
socket client, independent of the harness, probed `Network.*`, `UI.*`, and `Game.*` under `pcall` in
both tuner contexts (`GameCore_Tuner` and `InGame`). The result
(`specs/002-civ-playing-harness/spikes/r5-save-path.md`):

- `Network.SaveGame` is a real function in `InGame`, and four consecutive calls produced four
  valid, size-stable `.Civ6Save` files.
- `Network.LoadGame` also exists as a callable function (though the spike only exercised the save
  half, not the load half — that's `saves.load_game`'s own open question, not this capability's).

**The outcome flipped the plan, not just filled in a blank.** Because the spike ran first and found
a working Lua save path, the bespoke input-automation save driver didn't become merely
*unnecessary* — it became **forbidden**: Principle II requires the Firetuner path once one is
known to exist, so `saves.save_game` is declared `path: firetuner` in `catalogs/capabilities.yaml`,
and a bespoke save driver is no longer a legitimate design option for this capability at all. That
is the shape every `firetuner_gap` candidate should go through: spike first, let the evidence decide
the path, and treat "we assumed bespoke was needed" as a defect if a spike was never actually run.

If you're adding a capability and suspect it needs `path: bespoke`, the process is: **run the same
kind of probe R5 did** (direct `pcall` name-probing against the relevant namespace — see
`catalogs/README.md` §3's "opaque namespace" note on why enumeration alone doesn't work), record
what you found in a spike doc under `specs/002-civ-playing-harness/spikes/`, and only then write
`firetuner_gap` citing that spike. A `firetuner_gap` with no spike behind it should not pass review,
even though the loader itself can't detect that distinction mechanically.

## 4. The predicate grammar

`availability_predicate` and `verification_predicate` are restricted boolean expressions evaluated
by `src/civsim_harness/act/predicates.py` — never arbitrary code. The fixed symbol table lives in
`catalogs/README.md` §4; the short version:

- Literals (numbers, strings, `true`/`false`, `null`, `[a, b, c]`), `.` attribute access,
  `and`/`or`/`not`, comparisons (`==`, `!=`, `<`, `<=`, `>`, `>=`), and `in` (list membership or
  scalar equality).
- Binary `+` and `-` between two numeric operands are **permitted**, deliberately and narrowly.
  `turn.end_turn`'s own `verification_predicate` is the reason: `game.turn_number ==
  observed_turn_number + 1 or game.is_waiting_for_other_players`. Forbidding arithmetic entirely
  would force "next turn number" to be precomputed in Python and handed in as a bespoke binding,
  pushing game semantics out of the declarative catalog — and previously made `turn.end_turn`, the
  single most important action in the harness, unable to ever verify as `applied`. Do not
  "simplify" this back to disallowing arithmetic; that regression already happened once.
- **Rejected**: `*`, `/`, any arithmetic beyond binary `+`/`-` on numeric operands, function calls,
  and assignment. A non-numeric operand to `+`/`-` (a string, an absent/`null` field) is a clean
  evaluation-time error — never silently coerced.

Use only the namespaces and fields §4 tables list (`game.*`, `player.*`, `unit.*`, `city.*`,
`target`/`target.*`, `other_player.*`, `congress.*`, `great_person.*`, `spy.*`, `prompt.*`,
`camera.*`, `observed_*`). A predicate needing a symbol not listed there is a sign the declaration
belongs to a different observation, not a reason to widen the evaluator ad hoc — validation 6 above
will refuse it at load time regardless.

## 5. The `UNVERIFIED` convention

Every `lua/**/*.lua` file was authored without a live Civ VI client available at time of writing.
Per `catalogs/README.md` §3.4, any Gameplay/Lua API call whose existence, exact name, or field shape
is uncertain is marked inline with `-- UNVERIFIED:` rather than presented as fact.

**An honest marker is worth more than a plausible guess, and this project has direct proof of
that.** A live sweep against a real client
(`specs/002-civ-playing-harness/spikes/lua-api-verification-linux.md`, and its raw output under
`spikes/sweep-raw/`) found that three calls a plausible-sounding pre-verification draft would have
reached for do not exist at all:

| Candidate | Actually resolves to |
|---|---|
| `Game.EndTurn` | `nil` — no such function |
| `Cities.GetCity` | `nil` — no such function |
| `Units.GetUnit` | `nil` — no such function |

The real end-turn action turned out to be `UI.RequestAction(ActionTypes.ACTION_ENDTURN)` (see
`catalogs/actions/turn.yaml`'s header comment and `lua/ingame/turn_control.lua`), whose return value
is always `nil` — which is itself the reason `turn.end_turn`'s `verification_predicate` has to
compare turn numbers rather than trust a return value. Had the pre-verification Lua simply guessed
at `Game.EndTurn` and shipped it unmarked, this would have been a silent runtime failure discovered
mid-run instead of a name correctable at review time. Mark what you haven't verified. When a live
sweep becomes possible for your change, resolve every `UNVERIFIED` it touches and update the comment
to say what was actually confirmed (see `lua/gamecore/units.lua`'s header for the pattern: it
records both what the sweep confirmed and what it found absent, even for symbols the file itself
never used).

## 6. The Lua sandbox constraints

Every file under `lua/` runs inside Civ VI's stock Lua sandbox in one of two tuner contexts
(`GameCore_Tuner` or `InGame` — see `catalogs/README.md` §2 for which capabilities need which).
Confirmed constraints, from the live sweep (`spikes/lua-api-verification-linux.md`, P5) and encoded
in every file's own header comment:

- **No `json`.** Neither context exposes a global JSON library. Every Lua file that emits a result
  carries its own hand-rolled `CivSim_JsonEncode` function (see `lua/gamecore/units.lua` for the
  pattern) rather than assuming a shared one exists.
- **No `require`.** Because there is no `require`, **every Lua file's body must be entirely
  self-contained** — you cannot factor a helper (including the JSON encoder above) out into a shared
  module and pull it in from multiple files. If two files both need the same helper, both files
  carry their own copy. This is not an oversight to fix later; it is a hard sandbox constraint every
  new Lua file has to plan around from the start.
- **No `io`.** No filesystem access from Lua.
- **No `debug`.** No introspection.
- **`_G` is `nil`**, and `UI`/`Network` are opaque to `pairs()` (0 entries even though `pairs_ok =
  true`) — only `Game` is iterable. This means you cannot enumerate a namespace's members
  programmatically to discover what exists; the only workable method is direct name-probing under
  `pcall`, recording `type()` for each candidate (exactly what R5 and the live sweep did). Plan a
  spike this way, not as an enumeration script.

Each Lua file defines exactly one function per served `declaration_id`, returning a plain Lua
table — never printing directly. `src/civsim_harness/capability/registry.py` and the Nexus client
select the function, call it, JSON-encode the result, and print it once between the
`---BEGIN:<nonce>---`/`---END:<nonce>---` sentinels; that dispatch is outside `lua/`'s own scope; see
`catalogs/README.md` §3.3.

## 7. Bumping `catalogs/VERSION` for a new capability (FR-029)

FR-029 requires that a skill or capability added on demand — to cover surface area not yet
declared — enters the catalog with a parity declaration **before first use**, under the same rules
as any other capability (all of the above), and that runs before and after the addition remain
**distinguishable by catalog version**.

Mechanically:

1. Add the new declaration(s) to the appropriate `catalogs/observations/*.yaml`,
   `catalogs/observations/views.yaml`, or `catalogs/actions/*.yaml` file, and the capability entry
   to `catalogs/capabilities.yaml` if it's a new `capability_id` (existing capabilities may back
   more than one `declaration_id` — see `catalogs/README.md` §1).
2. Set the new declaration's `introduced_in_version` to the **new** version string you're about to
   write into `catalogs/VERSION` — not the version currently there. Every existing declaration
   carries an `introduced_in_version` of `"2026.09.1"` (the catalog's current version); a new
   declaration's value is what makes "which runs could possibly have used this" answerable from the
   record alone.
3. Bump `catalogs/VERSION` itself to that new string.
4. Run `uv run python -c "from civsim_harness.models.export_schemas import export_all; export_all()"`
   if the change touched any of the exported record models (it usually won't for a catalog-only
   addition, but the schema export enforces additive-only evolution and will refuse a breaking
   change loudly rather than let it merge silently — see that module's own docstring).

**The version bump is a human action, and a forgotten one is exactly what `compute_content_hash`
(`src/civsim_harness/capability/version.py`) is for.** `CatalogVersion.content_hash` folds in the
bytes of every catalog file *and* the resulting `declaration_ids` set. If a new declaration is added
but `catalogs/VERSION` is left unbumped, the version *string* two runs report would be identical
while the content hash would differ — which is precisely the case SC-007 needs to be able to tell
apart, and validation 7 exists to catch. Don't rely on remembering to bump the string; treat the
content-hash mismatch as the actual backstop, and the string as the human-readable half of the same
guarantee.

A run's `CatalogVersion` record (version string + content hash + `declaration_ids`) is what every
later `audit parity`/`audit capabilities` run resolves its declarations against — see
`specs/002-civ-playing-harness/quickstart.md` Scenario 3. Adding a capability mid-project without
following this process doesn't just risk a load-time failure; it risks a run whose declared basis
can no longer be reconstructed from the record alone, which is the exact failure SC-007 is written
to make impossible.
