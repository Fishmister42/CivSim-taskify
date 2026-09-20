# Principle I — does the tuner depend on, or widen with, `EnableDebugMenu`?

**Feature**: `002-civ-playing-harness` · **Executed**: 2026-09-20 · **Host**: Linux (native Aspyr)
**Client**: `1.0.12.9 (564030)`, Gathering Storm, BBG active

Raised because this host shipped with `EnableDebugMenu 1` already set in `AppOptions.txt`. The
concern: Principle I permits the agent only what a human could obtain through the standard game UI,
a debug menu is not standard UI, and **if the tuner depended on debug mode there would be a genuine
constitutional tension** — every run would be made in a non-parity configuration.

## Verdict

**No tension. The tuner is completely independent of the debug menu.**

| Question | Answer |
|---|---|
| Does port 4318 still listen with `EnableDebugMenu 0`? | **Yes** — bound 12.0 s after launch, same as with it on |
| Do `GameCore_Tuner` / `InGame` still resolve once a game is loaded? | **Yes** — and at the *same indices*, 10 and 132 |
| Does the callable Lua surface differ between the two modes? | **No difference found across every surface that can be compared** |

**`EnableDebugMenu 0` is therefore safe to require, and costs nothing.** It should be the default for
any run whose data is intended for trending, metrics, or optimization — not because the tuner
changes, but because a debug menu reachable at the client is non-standard UI on general principle.

## Method

The comparison is a genuine before/after on one host: full probe with `EnableDebugMenu 1`, clean
restart with `EnableDebugMenu 0`, identical probe, `diff`. Nothing else changed between the runs
except a new Play Now game (a different civ, which does not affect API surface).

Three separate surfaces were compared, because no single one is sufficient:

### 1. Curated symbol probe — 95 symbols × 2 contexts

All five probe groups from the [API sweep](./lua-api-verification-linux.md), covering turn control,
screen identity, congress/great-people/religion, map/unit/city managers, and the Lua standard
library:

```
IDENTICAL: GameCore_Tuner__P1_turn_control.txt      IDENTICAL: InGame__P1_turn_control.txt
IDENTICAL: GameCore_Tuner__P2_screens.txt           IDENTICAL: InGame__P2_screens.txt
IDENTICAL: GameCore_Tuner__P3_congress...txt        IDENTICAL: InGame__P3_congress...txt
IDENTICAL: GameCore_Tuner__P4_spotchecks.txt        IDENTICAL: InGame__P4_spotchecks.txt
IDENTICAL: GameCore_Tuner__P5_json_and_misc.txt     IDENTICAL: InGame__P5_json_and_misc.txt
```

10 / 10 byte-identical.

### 2. Full namespace enumeration — the genuinely enumerable surface

`Game` is iterable (53 members in `InGame`, 41 in `GameCore_Tuner`). Dumped in both modes:

```
IDENTICAL: GameCore_Tuner__namespaces
IDENTICAL: InGame__namespaces
```

This is the strongest of the three, because it is a *complete* listing of that namespace rather than
a lookup of names chosen in advance. If debug mode added a `Game.*` member, this would have caught it.

### 3. The Lua state table

```
IDENTICAL: state table (136 states, same indices)
```

Debug mode adds no Lua states and does not shift existing indices.

## The honest limit of this result

**This is not a proof that the two surfaces are identical, and it should not be quoted as one.**

An exhaustive enumeration is impossible in this sandbox: `_G` is nil, and `UI` / `Network` are opaque
userdata that yield zero entries under `pairs()` (see
[r5-save-path.md](./r5-save-path.md#method-and-why-the-obvious-approach-fails)). There is no way to
ask the client for its complete symbol list, so a debug-only member of `UI` or `Network` whose name
nobody guessed would not appear in any of the three comparisons above.

What can be said precisely:

- Every symbol the harness actually uses or plans to use is identical in both modes.
- The one namespace that *can* be enumerated completely is identical in both modes.
- The state table is identical in both modes.
- No evidence of any difference was found.

That is enough to drop the "the tuner might depend on debug mode" worry, which was the urgent
question. It is not enough to claim the client exposes nothing extra anywhere.

## Recommendation

1. **Require `EnableDebugMenu 0` for any run whose data feeds trending, metrics, or optimization.**
   It is free — nothing the harness needs is lost.
2. **Have preflight read and record the setting** through the host port rather than assume it. The
   value is in `AppOptions.txt` under `[Debug]`; the same file already has to be read for
   `EnableTuner`.
3. Recording it is worth doing **even once it is enforced**, so a run's parity configuration is
   reconstructible from its record alone rather than from a claim about how the host was set up.
4. Runs already taken with it on are not invalidated *by this evidence* — but they were taken in a
   configuration where a human at the client had access to non-standard UI, which is a separate
   argument from the Lua surface and not one this spike settles.

## Host state after this spike

`EnableDebugMenu` is left at **0** on this host. Original value was `1`; the pre-change file is
preserved at `AppOptions.txt.pre-civsim.bak`.

## Related finding: the on-screen FPS counter is **not** the debug menu

The `60 FPS` overlay in the client's top-right corner **persists with `EnableDebugMenu 0`**, so it is
not a debug-menu artifact. It is the **Steam overlay's** FPS counter —
`"InGameOverlayShowFPSCorner" "2"` in Steam's `localconfig.vdf` (corner 2 = top-right). Details and
the capture-hygiene consequence are in [launch-tuning-linux.md](./launch-tuning-linux.md).
