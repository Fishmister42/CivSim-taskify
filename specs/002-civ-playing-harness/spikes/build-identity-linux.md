# Which build number is authoritative? Neither — they are different quantities

**Date:** 2026-09-20 · **Host:** Linux live node · Native Aspyr build.

The open question was: *"the client shows `1.0.12.9 (564030)` at the main menu but `(363760)` in-game
and in every artifact it writes — which is authoritative?"*

**The question has a false premise.** They are not two answers to one question; they are two
different values that happen to share a `1.0.12.9` prefix.

## Measured

**The running client reports `564030`, in-game as well as at the main menu:**

```
  OK    UI.GetAppVersion()    = 1.0.12.9 (564030)      <- read in the InGame state
```

The main-menu footer shows `1.0.12.9 (564030) - MPH / 179`. **So the earlier claim that the client
shows `363760` "in-game" is wrong** — the in-game Lua API agrees with the main menu.

**`363760` is what this same client stamps into the artifacts it writes:**

```
$ strings civsim__cycle__probe.Civ6Save | grep '1\.0\.12'
1.0.12.9 (363760)
```

That save was written **today at 17:36** by the very client that reports `564030`. Same machine,
same process, same minute. So `363760` is not a stale file from an older install and not a different
client — it is a constant this build writes into save and `.Civ6Cfg` headers, under the label
`Saved By Version`.

The most likely reading, stated as inference not fact: `363760` is a **save-format / compatibility
version** — the build whose artifact format this one still writes — while `564030` is the
**executable build**. Confirming that would need a second client version to compare against, which
this host does not have.

## 🔴 Why this matters for T174/T175 build agreement

**A build-agreement check that compares a save's `Saved By Version` against a live client's
`UI.GetAppVersion()` will mismatch every time** — including on one machine, one client, one minute,
with the save just written. There is no configuration in which those two values agree.

So the two must never be compared with each other. Concretely:

- **Client-to-client comparability** (are two hosts running the same game?) → compare
  `UI.GetAppVersion()` on both. That is the executable identity.
- **Artifact compatibility** (can this client load that save?) → compare `Saved By Version` against
  `Saved By Version`, or against a known-compatible set.
- **Never** cross the two.

Worth checking against the Windows host, which runs `1.0.12.68 (1023995)`: if its saves *also* stamp
`363760`, the save-format reading is confirmed and artifact compatibility across the two hosts may be
fine even though the executables differ by four hundred thousand revisions — which would change the
cross-host comparability picture materially. If its saves stamp something else, the two values track
each other and this is simply a display inconsistency.

**That is a cheap check on the Windows side and it is the next step on this question.**
