# Victory progress and Principle I — the ban is over-restrictive, but the fix is a redaction rule

**Date:** 2026-09-20 · **Host:** Linux live node · Evidence: the game's own shipped UI source.

`Game.GetVictoryProgressForTeam` was ruled out of parity on the grounds that it would hand the agent
rival information a human cannot get from the standard UI. **The game's own Victory Progress screen
calls that exact function**, so the question is not whether a human can see it — they can — but
*exactly how much* they see.

The answer is precise, and it is neither "ban it" nor "allow it".

## The human's screen calls the banned API, for every alive major team

`steamassets/base/assets/ui/partialscreens/worldrankings.lua:696-705` — the World Rankings /
Victory Progress partial screen:

```lua
	-- Gather team data
	local teamIDs = GetAliveMajorTeamIDs();

	local teamData:table = {};
	for _, teamID in ipairs(teamIDs) do
		local team = Teams[teamID];
		if(team ~= nil) then
			-- If progress is nil, then the team is not capable of earning a victory (ex: city-state teams and barbarian teams).
			local progress = Game.GetVictoryProgressForTeam(victoryType, teamID);
```

`GetAliveMajorTeamIDs()` (line 273) is every alive **major** civ's team, deduplicated — **with no
"have we met them" filter.** The only exclusion is `progress ~= nil`, which the comment explains is
city-states and barbarians.

**So the progress values of every living major civilisation are on a human's screen**, met or not.

## But identity is redacted for civs you have not met

`worldrankings.lua:481-495`:

```lua
function GetCivNameAndIcon(playerID:number, bColorUnmetPlayer:boolean)
	if(playerID == g_LocalPlayerID or playerConfig:IsHuman() or g_LocalPlayer == nil
	   or g_LocalPlayer:GetDiplomacy():HasMet(playerID)) then
		name = Locale.Lookup(playerConfig:GetPlayerName());
		...
	else
		name = bColorUnmetPlayer and LOC_UNKNOWN_CIV_COLORED or LOC_UNKNOWN_CIV;
		icon = ICON_UNKNOWN_CIV;
	end
```

and again at line 1010, where unmet players on a team get their leader icon and name explicitly
hidden:

```lua
		if g_LocalPlayerID ~= playerID and not g_LocalPlayer:GetDiplomacy():HasMet(playerID) then
			civInstance.LeaderIcon:SetHide(true);
			civInstance.LeaderName:SetHide(true);
```

Unmet civs appear as **`LOC_WORLD_RANKING_UNMET_PLAYER`** with `ICON_CIVILIZATION_UNKNOWN`.

## The rule this implies

| information | human can see it? | therefore |
|---|---|---|
| Victory progress values for **all** alive major civs | ✅ yes | in parity |
| Progress **attributed to a named civ** — met civs | ✅ yes | in parity |
| Progress **attributed to a named civ** — unmet civs | ❌ no | **must be redacted** |
| Progress for city-states / barbarians | n/a — always `nil` | n/a |

So the correct treatment is **not a ban** and **not free access**: expose victory progress for all
alive major civs, and **anonymise the identity of any civ the local player has not met**, exactly as
the screen does. That mirrors the UI's own asymmetry — it shows *the standings* while withholding
*who is where* for strangers.

`Player:GetDiplomacy():HasMet(playerID)` is the same predicate the UI uses, so the redaction can be
implemented against the identical source of truth rather than an approximation.

**Recommendation, for the owning side to rule on:** lift the blanket ban, replace it with the
redaction above. This is a Principle I question and the ban is theirs; nothing here is a unilateral
change.

## ❌ Correction to my own earlier probe

I previously reported this as *"inconclusive — returns `nil`"* and speculated it was because the game
was at turn 2 with no progress to report. **The `nil` was my bug.** The signature is:

```lua
Game.GetVictoryProgressForTeam(victoryType, teamID)   -- (victoryType FIRST, then team)
```

I called it as `(team, victoryType)` — arguments reversed. The earlier `nil` says nothing about
victory progress and should not be cited. The turn-2 explanation I offered was a plausible story
wrapped around my own mistake, which is worth noting as its own lesson: **an uninterpretable result
deserves a check of the call before a theory about the game.**

Re-measuring with the correct order against a game with real progress is still worth doing, but the
parity question above is settled by the UI source regardless of what the call returns.

## Victory types, for reference

From `GameInfo.Victories()` on this client:

```
VICTORY_SCORE 1428793787 · VICTORY_DEFAULT -1085549345 · VICTORY_CONQUEST 407109516
VICTORY_CULTURE -356759317 · VICTORY_RELIGIOUS 415516560 · VICTORY_TECHNOLOGY 353119609
VICTORY_CONCEDE -12499977 · VICTORY_DIPLOMATIC -494301917
```
