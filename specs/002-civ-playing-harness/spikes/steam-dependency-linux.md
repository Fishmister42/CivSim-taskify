# Spike — Steam is a hard dependency of the Linux live node

**Date:** 2026-09-20
**Host:** native Aspyr build, Steam app 289070, Linux.

Recorded because it constrains the **scheduling of every live task**, not because anything is broken.

## Verdict

**Civilization VI cannot be launched without a running, logged-in Steam client.** There is no
Steam-free path to the live node, and the workaround the game itself suggests does not apply to an
end user.

## What was measured

The binary was run directly, with Steam installed but signed out:

```
cd ".../steamapps/common/Sid Meier's Civilization VI"
LD_LIBRARY_PATH="$PWD" ./Civ6
```

It links `libsteam_api.so` and references seven symbols:

```
SteamAPI_Init                 SteamAPI_RegisterCallResult   SteamAPI_UnregisterCallback
SteamAPI_RegisterCallback     SteamAPI_RestartAppIfNecessary SteamAPI_UnregisterCallResult
SteamAPI_RunCallbacks
```

The process started and mapped a window, which is initially encouraging and is **not** a successful
launch. Log:

```
[S_API FAIL] SteamAPI_Init() failed; no appID found.
Either launch the game from Steam, or put the file steam_appid.txt containing the correct appID
in your game folder.
```

The window it mapped (1241×100) is a DRM refusal dialog:

> This game needs Steam. Please make sure that Steam client is running, logged on Steam account that
> has this game in Library, then run the game again. The game will now exit.

It waits on `OK` and exits. **A live process and a mapped window are not evidence of a running
game** — this was briefly mis-reported as a successful Steam-free launch before the window was
actually read, which is the entire argument for reading the frame rather than the process table.

`steam_appid.txt` is a developer convenience that only defeats `SteamAPI_RestartAppIfNecessary`; it
does not satisfy `SteamAPI_Init`, which needs a live authenticated client. Nothing further was
attempted: anything that would get past this is DRM circumvention and is out of scope.

## The operational consequence

Steam permits an account to be **in a game on only one machine at a time**. Launching Civ VI here
prompts to close whatever the account is playing elsewhere, and vice versa.

So the live node is unavailable whenever the owner is gaming on that account. Observed 2026-09-20:
a launch attempt from this node collided with the owner's own session and was aborted by him.

**Steam offline mode is not a workaround from a signed-out state.** Offline mode is built on cached
credentials, and signing out clears them (`config/loginusers.vdf` drops to 12 bytes). Steam requires
a fresh online sign-in before it will offer to go offline again.

**Family Sharing is not a workaround either** — borrowing is blocked precisely while the lender is
playing, which is the conflicting case.

## What this means for scheduling

- Live Civ-dependent tasks must be **scheduled around the owner's own Steam usage**. They cannot be
  assumed available on demand, and an unattended overnight run can be pre-empted by the owner simply
  playing something.
- Client-free work should be preferred whenever the client is contended. The XComposite capture path
  (`r6-xcomposite-readback-linux.md`) was built and verified end-to-end this way, against ordinary
  X11 windows, with no client at all.
- **The only durable decoupling is a second Steam account owning its own copy of Civilization VI**
  on this box. That is a purchase and an owner decision; it is recorded here as an option, not a
  recommendation.

## Launch preconditions, restated

1. Steam client running **and signed in** on this machine.
2. The account not currently in a game on another machine.
3. `EnableTuner 1` in `AppOptions.txt` (survives a clean exit; disables achievements).
4. `PlayIntroVideo 0` — set 2026-09-20, saves ~40 s per restart. Edit only while the game is closed,
   or the exit write clobbers it.
