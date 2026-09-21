# Screen ids the shipped Civ VI UI has no state for (2026-09-21)

`civsim store coverage` reports a claimed screen id as unattestable when
`CIVSIM_SCREEN_ID_BY_STATE` in `lua/ingame/screens.lua` maps no Lua state to it: the identity probe
can never report it, so no run can ever demonstrate it, so any prompt action gated on it can never
become available. It flagged six. This note records what reading Firaxis's shipped UI Lua actually
found for each, so the ones that cannot be mapped are a **documented gap** rather than a silently
wrong catalog claim, and so nobody later "fixes" the coverage warning by inventing a mapping.

Everything below was read, not measured. Read from
`~/.steam/debian-installation/steamapps/common/Sid Meier's Civilization VI/steamassets/`
(Gathering Storm active, so `dlc/expansion2/ui/replacements/ingame.xml` is the live context list),
cross-checked against the live Lua-state enumeration in `spikes/r5-raw/00_states.txt`.

## A mechanism note that applies to all of them

`CivSim_Screens_State` resolves each watchlist entry as `ContextPtr:LookUpControl("/InGame/<name>")`.
That path is keyed by a `<LuaContext>`'s **`ID`** attribute; the Lua *state* is named after its
**`FileName`**. They are the same string for almost every context, and two where they differ were
therefore unprobeable and have been corrected in the watchlist:

| watchlist entry (was) | `ID` (now) | `FileName` / Lua state | evidence |
| --- | --- | --- | --- |
| `CivilopediaScreen` | `Civilopedia` | `CivilopediaScreen` | `base/assets/ui/ingame.xml:129`, `dlc/expansion2/ui/replacements/ingame.xml:142` |
| `InGameTopOptionsMenu` | `TopOptionsMenu` | `InGameTopOptionsMenu` | `base/assets/ui/ingame.xml:136`; Firaxis's own Lua looks it up as `/InGame/TopOptionsMenu` |

T213's note that "`CivilopediaScreen` is absent on this build" was reading the wrong name — the
state exists (index 109 in `00_states.txt`). Unverified live that the corrected paths resolve.

`Options`, `SaveGameMenu` and `LoadGameMenu` are Lua states but are not `<LuaContext>` children of
`InGame` in any shipped `ingame.xml`, so `/InGame/<name>` cannot reach them from the aggregate
probe either. They stay on the watchlist only as documentation for a future per-state dispatcher.

## `world` — resolved, not a gap

The probe answered `world_view`; `CIVSIM_KNOWN_SCREENS` and `catalogs/README.md`'s
`game.current_screen` field vocabulary both say `world`. The catalog contract wins: the probe now
returns `world`, and the Python fakes/fixtures that mirrored `world_view` were renamed with it.
`world_view` appeared in no catalog file at any point. Historical spike records
(`t213-observation-bodies-linux.md`, `gameplay-2026-09-21/README.md`) keep the old string because
they record what was measured at the time.

## `strategic` — unmappable

`<LuaContext ID="StrategicView" FileName="StrategicView"/>` (`base/assets/ui/ingame.xml:13`,
`dlc/expansion1/ui/ingame.xml:13`, `dlc/expansion2/ui/replacements/ingame.xml`) carries **no
`Hidden` attribute**, and `base/assets/ui/strategicview.lua` is six lines of comment header with no
code at all — no show/hide logic, no `ContextPtr` handlers. Its `IsHidden()` is therefore false at
the ordinary world view too, so watching it would make the probe answer `strategic` permanently.

Strategic view is a world **render mode**, not a screen: `UI.GetWorldRenderView()` /
`UI.SetWorldRenderView(WorldRenderView.VIEW_2D)`, which `lua/ingame/camera.lua` already reads and
`catalogs/observations/views.yaml` (`views.strategic`) already declares. The honest correction is on
the catalog side: `strategic` does not belong in `game.current_screen`'s vocabulary alongside
`city_screen`/`diplomacy`/`congress`; it belongs to the camera's `mode`.

## `prompt.religion_selection` — unmappable

Founding a religion is a **notification**, not a modal prompt. Activating it fires
`LuaEvents.NotificationPanel_OpenReligionPanel()` (`base/assets/ui/panels/notificationpanel.lua:1322`,
handler `OnChooseReligionActivate`), which `base/assets/ui/religionscreen.lua` binds to `OnShowScreen`
→ `Open()` (lines 1426-1455, registrations at 1606-1609) — the **same** `ReligionScreen` context the
launch bar opens for browsing (`base/assets/ui/launchbar.lua:142`) and the pantheon chooser hands off
to (`base/assets/ui/choosers/pantheonchooser.lua:137`).

So `IsHidden()==false` on `/InGame/ReligionScreen` means "the religion screen is open", never "a
blocking founding prompt is up". Mapping it would make the probe assert a blocking prompt every time
the agent merely opened the religion overview — a false `has_blocking_prompt`, which is the one
thing FR-049 exists to prevent. (Contrast `prompt.pantheon_selection` → `PantheonChooser`, which is
a dedicated single-purpose context and is correctly mapped.)

## `prompt.diplomatic_approach` — unmappable

An AI-initiated approach arrives as `Events.DiplomacyStatement` → `OnDiplomacyStatement`
(`base/assets/ui/diplomacyactionview.lua:2741`), which sets `ms_ActiveSessionID` and shows the
**same `DiplomacyActionView` context** in `CONVERSATION_MODE` or `CINEMA_MODE` that the
player-initiated screen uses in `OVERVIEW_MODE`/`DEAL_MODE`. `DiplomacyActionView` is already mapped
to the `diplomacy` screen id.

What separates "the AI is asking me something" from "I opened the diplomacy screen" is
`ms_ActiveSessionID` and the view mode — private Lua state of that context, invisible to
`ContextPtr:IsHidden()`. `LeaderScene` is the 3-D leader backdrop, shown for both cases, so it does
not distinguish them either.

## `prompt.congress_vote` — unmappable

`WorldCongressPopup` (`dlc/expansion2/ui/replacements/ingame.xml:121`) is **one context for every
stage** of a congress: proposals, the vote itself (`OnVoteResolution` line 983, `OnVoteProposal`
line 1452 in `dlc/expansion2/ui/additions/worldcongresspopup.lua`), and results
(`OnWorldCongressResults` line 2347). The stage lives in `m_CurrentStage` / `m_CurrentPhase` inside
that context. `WorldCongressIntro` and `WorldCongressBetweenTurns` are the intro and between-turns
banners, not the vote.

`congress` already maps to `WorldCongressPopup`. Mapping `prompt.congress_vote` to the same state
would both fabricate a distinction the client does not expose and shadow `congress` (the probe
prefers a `prompt.*` id when several ids name one open state), trading one attestable id for one
fabricated one.

## `prompt.city_state_quest` — unmappable, and the catalog claim is wrong

There is **no city-state quest popup in the shipped UI at all**. Quests arrive as notifications
(`NotificationTypes.CITYSTATE_QUEST_COMPLETED`, `base/assets/ui/panels/notificationpanel.lua:134`,
:253) and are read in the `CityStates` partial screen
(`base/assets/ui/partialscreens/citystates.lua`, `<LuaContext ID="CityStates" .../>` at
`base/assets/ui/ingame.xml:50`), which is a browsable panel, not a blocking modal. No entry in
`spikes/r5-raw/00_states.txt` corresponds to a city-state quest prompt.

Nothing accepts or declines a quest in Civ VI — quests are objectives you may or may not pursue.
So `prompts.city_state_quest` ("accept or decline it in that prompt") describes an interaction the
game does not have, and the catalog claim itself should be retired rather than mapped.

## What the live lane should see

Nothing here is live-verified. The probe's behaviour for each unmapped id is: open that screen and
the probe answers the id it *is* mapped to (`diplomacy`, `congress`) or `unknown`
(`ReligionScreen`), never the prompt id — which is the correct, non-guessing outcome under FR-049.
The newly mapped `prompt.great_work_created` → `GreatWorkShowcase` still needs a live observation
(see `lua/ingame/screens.lua`).
