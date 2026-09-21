# T251 — `mod_set` identity on a live client: mixed-case ids, and a version that is not where the getter looked

2026-09-20 night, Linux validation host, Civilization VI 1.0.12.9 (Aspyr), 22 mods active
(BBG 7.5.0 / BBM 1.39.5 / MPH 1.7.9 + 19 official packs). Every reading below came through the
**production** read path — `run/preparation.py`'s `LuaGameSetupReader` over the production
`NexusClient` — not a spike client.

## Why this was measured

`tests/live/test_production_save_loader.py` re-verified T248, which meant a real run was next. Before
starting one, the V2 read-back (the setup snapshot the composition root compares against the run
configuration) was read on the live client to write a configuration that could pass. It could not
have: **`mod_set` was un-passable for any configuration on any modded host.**

## What the client actually reports

`Modding.GetActiveMods()` in `InGame`, first entry's fields (`pairs()`):

```
Allowance=true | Created=1 | Enabled=true | Handle=1 | Id=619ac86e-d99d-4bf3-b8f0-8c5b8c402567
| Name=Multiplayer Helper 1.7.9 | Official=false | Source=Mod | SourceFileName=MPH Core v1xx.modinfo
| SubscriptionId=3307282026 | Teaser=Esport oriented mod to help tournament and game statibility.
```

**There is no `Version` field.** The shipped getter read `tostring(m.Version)` and therefore printed
the string `"nil"` for all 22 mods — compared against any pin, that is a mismatch on every entry, so
V2 failed closed before turn 1 for every run configuration naming this host's mods (and `mod_set: []`
mismatches 22 ≠ 0). No run could have reached turn 1 on this host.

The version is reachable, keyed by the integer **`Handle`**, not the id string:

| call | result |
|---|---|
| `Modding.GetModProperty(m.Id, "Version")` | `bad argument #1 to 'lGetModProperty' (integer expected, got string)` |
| `Modding.GetModInfo(m.Id)` | same — integer expected |
| `Modding.GetModVersion(m.Id)` | function does not exist |
| **`Modding.GetModProperty(m.Handle, "Version")`** | **works** |

Read for all 22 through that call:

| id (as reported) | Name | Version |
|---|---|---|
| `619ac86e-d99d-4bf3-b8f0-8c5b8c402567` | Multiplayer Helper 1.7.9 | `179` |
| `c88cba8b-8311-4d35-90c3-51a4a5d66542` | Better Balanced Map 1.39.5 | `1.39.5` |
| `cb84075d-5007-4207-b662-c35a5f7be260` | Better Balanced Game 7.5.0 | `70500` |
| `9E881F4F-017F-29F5-D849-04537424BE3F` | `[ENDCOLOR]TopPanel Extension [COLOR:ResGoldLabelCS]Pro[ENDCOLOR]` | nil |
| `382a187f-c8ba-4094-a6a7-0d5315661f33` | Extended Policy Cards | nil |
| `E3F53C61-371C-440B-96CE-077D318B36C0` | LOC_AUSTRALIA_MOD_TITLE | nil |
| `02A8BDDE-67EA-4D38-9540-26E685E3156E` | LOC_AZTEC_MONTEZUMA_MOD_TITLE | nil |
| `8424840C-92EF-4426-A9B4-B4E0CB818049` | Babylon Pack | nil |
| `A1100FC4-70F2-4129-AC27-2A65A685ED08` | Byzantium and Gaul Pack | nil |
| `CE5876CD-6900-46D1-9C9C-8DBA1F28872E` | Catherine de Medici Persona Pack | nil |
| `1B394FE9-23DC-4868-8F0A-5220CB8FB427` | Ethiopia Pack | nil |
| `1B28771A-C749-434B-9053-D1380C553DE9` | LOC_EXPANSION1_MOD_TITLE | nil |
| `4873eb62-8ccc-4574-b784-dda455e74e68` | LOC_EXPANSION2_MOD_TITLE | nil |
| `9DE86512-DE1A-400D-8C0A-AB46EBBF76B9` | Maya and Gran Colombia Pack | nil |
| `1F367231-A040-4793-BDBB-088816853683` | LOC_INDONESIA_KHMER_MOD_TITLE | nil |
| `A3F42CD4-6C3E-4F5A-BC81-BE29E0C0B87C` | Vietnam and Kublai Khan Pack | nil |
| `E2749E9A-8056-45CD-901B-C368C8E83DEB` | LOC_MACEDONIA_PERSIA_MOD_TITLE | nil |
| `643EA320-8E1A-4CF1-A01C-00D88DDD131A` | LOC_NUBIA_MOD_TITLE | nil |
| `3809975F-263F-40A2-A747-8BFB171D821A` | LOC_POLAND_JADWIGA_MOD_TITLE | nil |
| `FFDF4E79-DEE2-47BB-919B-F5739106627A` | Portugal Pack | nil |
| `113D9459-0A3B-4FCB-A49C-483F40303575` | Teddy Roosevelt Persona Pack | nil |
| `2F6E858A-28EF-46B3-BEAC-B985E52E9BC1` | LOC_MOD_VIKINGSLANDMARKS_TITLE | nil |

Three more facts in that table, each of which would have failed V2 on its own:

1. **Ids are mixed-case in one list.** Workshop mods report lower-case GUIDs, official content
   upper-case. The seed set pins lower-case. A GUID's case is not identity.
2. **The list is in load order, not sorted.** V2 compared two Python lists with `!=`, so even a
   perfect pin in a different order was a mismatch.
3. **Official content has no version at all.** Not "unread" — the property is nil. A pin that
   demands a version string for Gathering Storm can never be verified on any host, and the seed
   set's `"v1"` on all 22 entries was a placeholder, not a measurement.

Also observed on the same snapshot, not chased tonight: `civsim_resolve(GameConfiguration
.GetTurnTimerType(), "TurnTimerTypes", "TurnTimerType")` returned **nil** for hash `-1525060181`
(the value the seed set records as `TURNTIMER_NONE`), so `turn_timer_preflight` saw an undeterminable
read and recorded `UNVERIFIED` rather than the verified name. The hash matches; the name lookup does
not resolve on this build. `turn_timer_preflight` does not refuse on it, so it is not a blocker —
but the "VERIFIED" note above `_TURN_TIMER_NAME_LUA` is stronger than what this snapshot shows.

## What landed (T251)

- **Getter** (`_SETTING_GETTERS["mod_set"]`): id lower-cased; version via
  `Modding.GetModProperty(m.Handle, "Version")` under `pcall`; a nil version is *omitted* from the
  entry, never printed as `"nil"`. Marked verified (this session).
- **`ModRef.version` is now `str | None`**: `None` means "pinned on id only", which is the only
  honest pin for official content. It is not a wildcard — a mod that *does* report a version is
  compared against it.
- **`canonical_mod_set`** (`run/preparation.py`): the one comparable shape — lower-cased ids,
  sorted by id, missing version ≡ `None`. `configured_fields` emits it; V3's `_mod_set_key` and the
  branch-document agreement key lower-case ids the same way.
- **`reconcile_mod_set_versions`**: for a live entry with no version where the configuration pins
  one, the pin is substituted so the *id* still compares, and `mod_set[<id>].version` is appended to
  the run's `v2_unobservable_fields` on the preparing → playing event — recorded, exactly like
  `map_settings.resources` (T250), never silently accepted. A reported version is compared as read;
  an unlisted mod still mismatches.
- **`configs/seedsets/civsim-default.yaml`**: the three workshop versions as measured, `null` for
  the 19 official packs, with the mapping id → pack name recorded beside each.
- Tests: `tests/unit/test_mod_set_identity.py` (six cases, each of the four failure modes above
  has a negative control); the seed-set contract test updated to the measured truth; published
  JSON schemas regenerated.

## Not done

- The reconciliation is exercised headlessly and the getter's Lua was measured by hand; the first
  real run through the composition root on this host is the end-to-end confirmation. That run is
  the next thing this session does.
- Windows/macOS: the `GetActiveMods()` shape is a client fact, not a platform one, but per R19 each
  host confirms its own.
