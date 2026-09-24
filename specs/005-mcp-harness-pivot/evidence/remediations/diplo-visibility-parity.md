# R3a — what Civilization VI itself shows at a known diplomatic visibility level

**Date**: 2026-09-24
**Purpose**: unblock R3. The audit established that `get_diplomacy`'s gating is *inconsistent* —
agendas gated on diplomatic visibility, exact military strength and total city count not. It did
**not** establish where Civ VI's own line falls, and R3 cannot be written correctly without that.
Patching on a guess would encode my assumption as the boundary, which is the failure this audit
exists to catch.

**Method**: the Cyrus run had met nobody at turn 52, so the comparison was run on the save the
original finding came from — the John Curtin / Australia game (`MCPAUDIT`, a copy of
`civsim-gameplay-2026-09-22-end2`), loaded at turn 69. The Cyrus game was checkpointed to `KEEPER`
first, so nothing was lost. Subject: **France (Catherine de Medici)** — chosen because it is the
cleanest case in the data.

---

## Side by side

### What the tool told the agent

From `get_diplomacy_raw.txt`, captured at turn 69, verbatim:

```
France (Catherine de Medici (Black Queen)) — UNFRIENDLY (-8) [player 4]
    Cities: 6 (all in fog)
    Military: 447 vs our 651 (0.7x)
    Access: they have delegation
    -8 Respects strong espionage opponents
    Agenda: Black Queen — Gains as many Spies and as much diplomatic access as possible...
```

**`Cities: 6 (all in fog)`.** The tool states France has exactly six cities and, in the same breath,
that the player has seen **none** of them. It is hard to construct a clearer statement of the leak:
the count is disclosed by the tool's own admission that its constituents are not visible.

### What the game showed, same civ, same save

`r3a-04-france-intel.png` — Civilization VI's own **Intel Report: Overview** for Catherine:

| Row | Value |
|---|---|
| Gossip | No New Items |
| **Access Level** | **Limited** |
| Government | Classical Republic |
| Our Relationship | Unfriendly, No Grievances |
| Agendas | Black Queen · **1 Hidden agenda** |
| Relationships | *(section)* |

**No city count. No military strength.** Neither figure appears anywhere on the panel at
`Access Level: Limited`.

---

## Verdict

**At `Access Level: Limited`, Civilization VI discloses neither a rival's city count nor their
military strength. `get_diplomacy` discloses both.** Confirmed by direct observation, not inference.

Two further things the comparison settles:

1. **The candidate already knows how to do this correctly.** The same panel shows
   *"1 Hidden agenda"* and the tool correspondingly emits
   `Agenda: [Hidden] — Requires Secret diplomatic visibility (spy or alliance)`. The agenda gate is
   right. The handle it uses — `pDiplo:GetVisibilityOn(i)` — sits four lines above the ungated
   `MILITARY|` print in the same query. This is not a missing capability; it is a boundary applied
   in one place and not the neighbouring two.
2. **The per-city detail was already gated correctly** (`+ 3 in fog`, `all in fog`), which is what
   makes the ungated *total* stand out as an oversight rather than a policy.

## What this did NOT establish — and what R3 does about it

**Only the `Limited` level was observed.** I did not verify what Civ VI shows at Open, Secret or
Top Secret access, and an attempt to page through the Intel Report's other five tabs failed — the
clicks landed on the map behind the panel and closed it (`r3a-05-*.png` are map views, not intel
tabs). So the exact threshold at which the game itself begins showing military strength or city
counts is **unverified**.

R3 therefore errs toward withholding, which is the direction Principle I requires: the constitution
says block rather than ship with a caveat. Both fields are withheld below the same threshold the
candidate's own agenda gate already uses, and the residual uncertainty is recorded here rather than
hidden in the code. If a later session establishes that Open access reveals military strength in
the game's own UI, the threshold can be relaxed on that evidence.

## Artifacts

- `get_diplomacy_raw.txt` — the tool's full output at turn 69
- `r3a-04-france-intel.png` — **the decisive frame**: Intel Report: Overview, Access Level Limited
- `r3a-03-france-intel.png` — hover tooltip confirming the subject is Catherine de Medici / French Empire
- `r3a-01-hud.png`, `r3a-02-hud.png` — HUD state before opening the panel
- `r3a-05-tab4/5/6.png` — failed tab navigation, kept as the record of what was *not* checked
