"""T251 -- `mod_set` identity is the lower-cased id, and a version the client cannot report is
recorded, never fabricated and never a mismatch.

Measured on the Linux validation host (2026-09-20, Civ VI 1.0.12.9, 22 mods active), through the
production getter: `Modding.GetActiveMods()` entries carry `Id`/`Name`/`Handle`/... and **no**
`Version`; ids come back in load order and in MIXED case (workshop mods lower-case, official
content upper-case, in the same list); the version is only reachable through
`Modding.GetModProperty(handle, "Version")` and is nil for every official pack. Before this, the
getter printed the string `"nil"` for every mod and compared it against a pin -- no run could
pass V2 on a live client with any mods at all.
"""

from __future__ import annotations

from civsim_harness.models.common import ModRef
from civsim_harness.run.preparation import (
    _SETTING_GETTERS,
    canonical_mod_set,
    reconcile_mod_set_versions,
)

_MPH = "619ac86e-d99d-4bf3-b8f0-8c5b8c402567"
_BBG = "cb84075d-5007-4207-b662-c35a5f7be260"
_RISE_AND_FALL = "1b28771a-c749-434b-9053-d1380c553de9"


def test_getter_reads_the_version_through_get_mod_property_by_handle_and_lower_cases_ids() -> None:
    """The entry itself has no Version field (measured); the handle-keyed property does."""
    lua = _SETTING_GETTERS["mod_set"].expression

    assert 'Modding.GetModProperty(m.Handle, "Version")' in lua
    assert "string.lower(tostring(m.Id))" in lua
    # A nil version is omitted, never stringified: "nil" as a version was the original defect.
    assert "tostring(m.Version)" not in lua
    assert _SETTING_GETTERS["mod_set"].verified is True


def test_canonical_form_ignores_order_and_id_case_and_treats_missing_version_as_none() -> None:
    live = [
        {"id": _BBG.upper(), "version": "70500"},
        {"id": _RISE_AND_FALL.upper()},  # no version key: the getter omits nil
        {"id": _MPH, "version": "179"},
    ]
    configured = [
        ModRef(id=_MPH, version="179"),
        ModRef(id=_RISE_AND_FALL, version=None),
        ModRef(id=_BBG, version="70500"),
    ]

    assert canonical_mod_set(live) == canonical_mod_set(configured)
    assert canonical_mod_set(live) == [
        {"id": _RISE_AND_FALL, "version": None},
        {"id": _MPH, "version": "179"},
        {"id": _BBG, "version": "70500"},
    ]


def test_a_pinned_version_the_client_cannot_report_is_recorded_not_mismatched() -> None:
    live = [{"id": _RISE_AND_FALL.upper()}, {"id": _MPH, "version": "179"}]
    configured = [ModRef(id=_RISE_AND_FALL, version="1.0"), ModRef(id=_MPH, version="179")]

    comparable, unverified = reconcile_mod_set_versions(live, configured)

    assert comparable == canonical_mod_set(configured)
    assert unverified == [f"mod_set[{_RISE_AND_FALL}].version"]


def test_a_reported_version_is_compared_never_substituted() -> None:
    live = [{"id": _MPH, "version": "179"}]
    configured = [ModRef(id=_MPH, version="180")]

    comparable, unverified = reconcile_mod_set_versions(live, configured)

    assert comparable != canonical_mod_set(configured)
    assert comparable == [{"id": _MPH, "version": "179"}]
    assert unverified == []


def test_a_mod_the_client_does_not_list_still_mismatches() -> None:
    live = [{"id": _MPH, "version": "179"}]
    configured = [ModRef(id=_MPH, version="179"), ModRef(id=_BBG, version="70500")]

    comparable, unverified = reconcile_mod_set_versions(live, configured)

    assert comparable != canonical_mod_set(configured)
    assert unverified == []


def test_an_unpinned_version_agrees_with_an_unreported_one_without_a_record() -> None:
    live = [{"id": _RISE_AND_FALL.upper()}]
    configured = [ModRef(id=_RISE_AND_FALL)]

    comparable, unverified = reconcile_mod_set_versions(live, configured)

    assert comparable == canonical_mod_set(configured)
    assert unverified == []
