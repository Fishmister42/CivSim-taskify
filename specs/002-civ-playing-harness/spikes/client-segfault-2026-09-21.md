# The client segfault of 2026-09-21 20:46:35 EDT — a NULL government definition

## What the kernel said

```
Sep 21 20:46:35 laptop kernel: Civ6 (WinID 2)[1915400]: segfault at b0 ip 000072a49ac80f80
  sp 000072a5eaff51c8 error 4 in libGameCore_XP2.so[72a498200000+3802000]
```

Steam removed the process at 20:46:45. The run (`run-024bc6135d9a4493bef4311fb472df74`, Stage 6,
worktree `454b2f8`) had begun turn 1 at 20:46:36; its store record holds two events —
`preparing → playing`, then `playing → paused` with `NexusError: connection closed by the game
client` — and **no save point**, so the client died inside the pre-save sequence of turn 1: the
game-over read, then the prompt probe, which assembles the full observation (every `*.state`
body) before the turn-start quicksave.

## Where in the engine, exactly

`ip − base = 0x2a80f80`. `nm -DC libGameCore_XP2.so` resolves that address to

```
0000000002a80f80 W GameCore::Definition::Government::GetPrereqCivicReference() const
```

and `objdump` shows its first instruction is `mov 0xb0(%rdi),%rax`: it read field `+0xb0` of
`this`, and `this` was NULL — "segfault at b0". So an engine method was asked about a
**government definition that resolved to nothing** and dereferenced it. The other two segfaults
in `dmesg` today (`ip 0 in Civ6`) are a different signature (the exit-modal kills).

The owner's own Steam session on another machine started at about the same minute. That did not
cause this: the faulting function names the code path, and it is ours.

## Which of our calls

Between the last hash known not to crash on this board (`14f7418`, Stage 5's run) and `454b2f8`,
the Lua that runs at turn 1 gained these engine calls (`git diff 14f7418..454b2f8 -- lua/`):

| body (context at the crash) | new engine calls | Firaxis' call site and guard |
|---|---|---|
| `government.state` (**GameCore_Tuner**) | `culture:IsGovernmentUnlocked(row.Hash)` for every `GameInfo.Governments()` row; `culture:GetCurrentGovernment()`; `IsPolicyObsolete / CanPolicyBeSlotted / IsPolicyBanned / IsPolicyUnlocked(row.Hash)` for every `GameInfo.Policies()` row; `GetNumPolicySlots`, `GetSlotPolicy(i)`; `player:GetGovernors():GetGovernorList()`, `governor:GetType/GetAssignedCity` | `governmentscreen.lua:2334`, `:2270-2271`, `:2284-2287`; `governorpanel.lua:58` — all from an **InGame** screen, all on rows the InGame `GameInfo` returns (numeric `Hash`), all after `displayPlayerID ~= -1` |
| `great_people.state` (**GameCore_Tuner**) | `Game.GetGreatPeople():GetTimeline()`, `CanRecruitPerson(player, entry.Individual)`, `player:GetGreatPeoplePoints():GetPointsTotal/PerTurn(row.Index)` | `greatpeoplepopup.lua:690-704, :728, :800-801` — InGame, `if displayPlayerID == -1 then return end`, `if pGreatPeople == nil then return end` |
| `religion.state` (**GameCore_Tuner**) | `Game.GetReligion():IsInSomePantheon / IsInSomeReligion(row.Index)`, `IsTooManyForReligion(row.Index, religion)` | `religionscreen.lua:462-464` — InGame, `IsTooManyForReligion` only with the player's own religion |
| `congress.state` (InGame) | `Game.GetWorldCongress():IsInSession()`, `GetResolutions(localPlayer)`, `Players[p]:GetFavor()` | `actionpanel_expansion2.lua:45` (unguarded, every turn), `worldcongresspopup.lua:573-574`, `toppanel_expansion2.lua:169` — InGame |
| `espionage.state` (InGame) | `unit:GetSpyOperation()` etc. | `espionageoverview.lua:88-101` — only on units whose `GameInfo.Units[...].Spy` is true; our body checks the same |
| `units.state` (InGame) | `UnitManager.GetActivityType(unit)` | `espionagechooser.lua:741-748` — InGame |

Ranked by "could this have produced a NULL `Government` definition":

1. **`culture:IsGovernmentUnlocked(row.Hash)` from `GameCore_Tuner`** — the faulting function is a
   `Government` definition accessor, and this is the only new call that makes the engine resolve
   one per row. Firaxis makes exactly this call, on exactly these rows, but from the InGame
   context; in the gamecore tuner state the row's `Hash` (or the culture object's definition
   table) did not resolve, and `pcall` cannot catch what happens next. `lua/gamecore/…` was the
   file's path, but the context is the yaml's `context:` key, and the audit moved `congress` and
   `espionage` to InGame on measured evidence while leaving these three on none.
2. `culture:IsPolicy*(row.Hash)` over every policy row, same context, same shape — the fault would
   have named a `Policy` definition; it did not, so not tonight's crash, but the identical class.
3. `Game.GetGreatPeople():GetTimeline()` / `Game.GetReligion():…` from `GameCore_Tuner` — Firaxis
   never calls either from a gamecore context; same class, not tonight's symbol.

## The guard patch (this commit)

- `government.state`, `great_people.state`, `religion.state` now run in **InGame**, the context
  every one of their accessors is called from in shipped code (the `cities.state` precedent,
  `1d0b372`).
- Every per-row engine predicate is called only with a numeric `Hash` / `Index`; a row without one
  is skipped and, when nothing could be answered, reported as `government_rows_without_hash` /
  `policy_rows_without_hash` rather than a silent `[]`.
- Every body returns `<field>_reason = "no_local_player"` before touching any player object when
  `Game.GetLocalPlayer()` is -1 — the check every shipped screen makes first.
- Lupa tests where the engine stub *faults* on a non-number and records what it was handed.

All of it is **UNVERIFIED LIVE** until a one-turn crash-watched block runs it from this hash; the
last hash known not to crash on this board is `14f7418`.

## The rule for the allowlist going forward

`lua/ACCESSORS.txt` answers "does this method exist". It does not answer "is it safe to call on
this object, in this context, with this argument". A method that exists is not a method that is
safe to call on every object: the engine trusts its callers the way Firaxis' own screens behave,
and a native fault is the one failure `pcall` cannot turn into a reason. So every new engine call
carries, in the same commit, the Firaxis call site it copies **and** the guard that call site
sits behind — the context it runs in, the `-1` check, the object nil-check, the numeric
`Hash`/`Index` — and "unverified live" is read as "may crash the client" until a crash-watched
block has run it.
