# Client crash ledger — spec-005

Recorded per `KEEPER-LOOP.md`: any segfault symbol other than the known one is a **new crash
class** and gets written down with the calls that preceded it.

Method, unchanged from the audit: `ip − library base`, then `nm -DC --defined-only` on the
shipped library, nearest preceding symbol. The resolver in `audit/repro_diplo_crash.py`
self-tests against crash #1 and reproduces its `+0x0` match exactly.

---

## Crash 1 — diplomacy. 2026-09-24 14:17:07Z. **Attributed, not reproduced.**

```
segfault at 8 ip 00007bf44226f8f0 error 4 in libGameCore_XP2.so[7bf440200000+3802000]
-> GameCore::Diplomacy::Action::Instance::GetType() const  (+0x0)
```

Preceded 1.6 s earlier by `send_diplomatic_action(other_player_id=2,
action="DECLARE_FRIENDSHIP")`, which returned `ACCEPTED|Cree accepted your friendship
declaration`. Fault address 8, `error 4` (read), faulting instruction `8b 47 08` =
`mov eax,[rdi+8]` — `GetType()` on a **NULL `Action::Instance`**.

Exact symbol match at offset `+0x0`, not a nearest-preceding approximation.

**Mitigation in force**: `send_diplomatic_action` is on the keeper's `--ban` list.
**Owed**: V3, the controlled reproduction. Harness built and self-validated; not yet run.

## Crash 2 — World Congress. 2026-09-24 ~16:24 local. **NEW CLASS.**

```
segfault at 0 ip 00007cb412e58018 error 4 in libGameCore_XP2.so[7cb410200000+3802000]
-> GameCore::Congress::Resolution::CalculateWinningPlayer()  (+0x1288)
```

Null read (`segfault at 0`, `error 4`) inside World Congress resolution scoring.

**Context, and it is suggestive rather than conclusive.** Shortly before this, a play block
deadlocked: **25 consecutive `end_turn` calls, every one `engine_refused`**, with the agent
narrating *"Diplomacy deadlock continues"* and *"Still blocked by Babylon diplomacy"* about
every 6 seconds. `queue_wc_votes` is in the applied-tool set for this game. A Congress was
plausibly mid-resolution while the turn could not advance.

**What I am not claiming**: the wall-clock also coincides with a keeper recycle and a manual
kill of the keeper process during a reload, so an unclean client teardown cannot be excluded
as the trigger. The *symbol* is not ambiguous — it is Congress resolution scoring — but
whether a tool call or the teardown reached it is not established.

**Not yet banned.** `queue_wc_votes` is a legitimate and rarely-reachable capability, and
banning it on one correlation would remove breadth we just spent effort proving (it is one
of the 12 action tools in the SC-002 tally). The honest position is: recorded, watched, and
banned if it recurs with a cleaner attribution.

---

## Pattern worth keeping

Both crashes are **null reads in `libGameCore_XP2.so` reached from tuner-driven calls**, and
so was this project's own pre-pivot crash
(`Government::GetPrereqCivicReference()`, 2026-09-21) — which was ours, in our own Lua,
before the candidate existed. Three instances, three different subsystems, two different
codebases.

That is the argument for treating this as **a property of driving this engine over the
tuner** rather than a defect either team wrote, and it is why the decision record does not
rest on the diplomacy crash being proven. Adopting the candidate does not buy us out of this
class; it inherits it, along with the candidate's auto-resume and restart-and-load recovery,
which is more than we had.
