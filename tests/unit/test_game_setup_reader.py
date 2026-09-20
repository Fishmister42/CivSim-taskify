"""T250 (closing T218): the game-setup read-back is phase-honest.

The peer's live evidence (`specs/002-civ-playing-harness/spikes/t218-RESULTS-setting-getters.md`)
is that two of `run/preparation.py`'s setting getters mean different things by game phase --
`Modding.GetActiveMods()` returns 0 entries at the front end vs the real 22 in-game, and the
major-opponent count decomposes 6-vs-16 across the same boundary -- and that
`map_settings.resources` has no getter in *either* phase. Three behaviours follow, each guarded
here at the seam where it lives (`LuaGameSetupReader`/`GameSetupSnapshot`):

1. An in-game-only field is **never read at the front end** -- the dispatched Lua must not even
   contain the call, because a front-end read on two differently-modded hosts records 0 on both
   and V2 falsely judges them COMPARABLE (Principle IV). The field is reported *phase-deferred*,
   in its own bucket, never conflated with "no getter has been authored".
2. The same field **is read in-game**, through the phase-correct derivation -- for
   `opponents.major_count` that is the spike's measured `Players` walk, never
   `GetAIPlayerCount()`, which in-game counts city-states, Free Cities, and Barbarians too.
3. `map_settings.resources` is **unobservable, not unread**: reported as a measured negative
   result in its own bucket, never dispatched, and -- for any caller that compares it anyway --
   still failing closed. (That it no longer kills a run is the composition root's decision,
   guarded by `tests/integration/test_end_to_end_wiring.py`.)

Every deliberately-not-read field still fails closed through `read_setting`: deferring or
recording is an explicit caller decision made by consulting the snapshot's own buckets, never a
default the snapshot volunteers.
"""

from __future__ import annotations

import asyncio
from typing import Any

from civsim_harness.run.preparation import (
    UNOBSERVABLE_SETTING_FIELDS,
    GameSetupSnapshot,
    LuaGameSetupReader,
    UnreadSetting,
)

_NO_GETTER_FIELD = "game_settings.experimental_toggle"

#: Substrings that identify the two in-game-only reads inside a dispatched Lua body. `IsMajor`
#: rather than the whole derivation: any `Players`-walking major count must touch it, so the
#: assertion survives cosmetic rewording of the expression while still failing if the read is
#: dropped or swapped back to `GetAIPlayerCount()`.
_MOD_SET_CALL = "Modding.GetActiveMods"
_MAJOR_COUNT_CALL = "IsMajor"


class _RecordingExecute:
    """The `execute(state_index, lua_body)` seam, recording every dispatched body."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.lua_bodies: list[str] = []

    async def __call__(self, state_index: int, lua_body: str) -> dict[str, Any]:
        self.lua_bodies.append(lua_body)
        return self.result


def _read(
    *, state_name: str, result: dict[str, Any], fields: tuple[str, ...]
) -> tuple[GameSetupSnapshot, str]:
    execute = _RecordingExecute(result)
    reader = LuaGameSetupReader(
        execute, state_index_source=lambda: 7, state_name=state_name
    )
    snapshot = asyncio.run(reader.read(fields))
    assert len(execute.lua_bodies) == 1, "the reader must read everything in one dispatch"
    return snapshot, execute.lua_bodies[0]


_REQUESTED = (
    "civilization",
    "mod_set",
    "opponents.major_count",
    "map_settings.resources",
    _NO_GETTER_FIELD,
)


def _front_end_snapshot() -> tuple[GameSetupSnapshot, str]:
    return _read(
        state_name="HostGame",
        result={"civilization": "CIVILIZATION_PERSIA", "turn_timer_type": "TURNTIMER_NONE"},
        fields=_REQUESTED,
    )


def test_in_game_only_fields_are_not_read_at_the_front_end() -> None:
    """The Lua a front-end (`HostGame`) snapshot dispatches contains neither in-game-only call --
    not "read and discarded", *not read* -- and both fields are reported phase-deferred."""
    snapshot, lua_body = _front_end_snapshot()

    assert _MOD_SET_CALL not in lua_body, (
        "a front-end snapshot dispatched Modding.GetActiveMods() -- the call whose front-end "
        "answer (0 mods, measured live with 22 active) is the T250 Principle IV risk"
    )
    assert _MAJOR_COUNT_CALL not in lua_body
    assert set(snapshot.phase_deferred) == {"mod_set", "opponents.major_count"}
    # The phase-stable field on the same request was read normally.
    assert snapshot.values["civilization"] == "CIVILIZATION_PERSIA"


def test_a_phase_deferred_field_still_fails_closed_if_compared_anyway() -> None:
    """`read_setting` on a deferred field returns an `UnreadSetting` naming the deferral --
    a caller that compares without consulting `phase_deferred` gets a mismatch, never a pass."""
    snapshot, _lua_body = _front_end_snapshot()

    for field_name in ("mod_set", "opponents.major_count"):
        value = snapshot.read_setting(field_name)
        assert isinstance(value, UnreadSetting)
        assert "in-game only" in value.reason
        assert value != [] and value != 0  # compares unequal to plausible front-end answers


def test_a_deferred_field_is_never_conflated_with_a_no_getter_field() -> None:
    """T250's distinct-marker requirement at the snapshot level: the no-getter tail lands in
    `unread` with the no-read-path reason, deferred fields land in `phase_deferred` with a
    different reason, and the buckets are disjoint."""
    snapshot, _lua_body = _front_end_snapshot()

    assert _NO_GETTER_FIELD in snapshot.unread
    assert _NO_GETTER_FIELD not in snapshot.phase_deferred
    assert snapshot.unread[_NO_GETTER_FIELD] == (
        "no read path is registered for this configured field"
    )
    deferred_reasons = set(snapshot.phase_deferred.values())
    assert deferred_reasons and all(
        reason != snapshot.unread[_NO_GETTER_FIELD] for reason in deferred_reasons
    )


def test_in_game_snapshot_reads_the_in_game_only_fields_for_real() -> None:
    """At the `InGame` state -- the phase both getters are honest in -- the reads are dispatched
    and their answers land in `values`, with nothing deferred."""
    snapshot, lua_body = _read(
        state_name="InGame",
        result={
            "civilization": "CIVILIZATION_PERSIA",
            "mod_set": [{"id": "bbg", "version": "4.2.1"}],
            "opponents__major_count": 5,
            "turn_timer_type": "TURNTIMER_NONE",
        },
        fields=_REQUESTED,
    )

    assert _MOD_SET_CALL in lua_body
    assert _MAJOR_COUNT_CALL in lua_body
    assert snapshot.phase_deferred == {}
    assert snapshot.values["mod_set"] == [{"id": "bbg", "version": "4.2.1"}]
    assert snapshot.values["opponents.major_count"] == 5


def test_major_count_is_the_players_derivation_never_get_ai_player_count() -> None:
    """Revert guard for the getter swap: `GetAIPlayerCount()` in-game counts every non-human
    player (16 = 5 majors + 9 city-states + Free Cities + Barbarians, T218's per-player
    decomposition), so it must not appear in the dispatched Lua in any phase."""
    _snapshot, in_game_lua = _read(
        state_name="InGame",
        result={"turn_timer_type": "TURNTIMER_NONE"},
        fields=("opponents.major_count",),
    )
    assert "GetAIPlayerCount" not in in_game_lua
    assert "IsAlive" in in_game_lua and _MAJOR_COUNT_CALL in in_game_lua


def test_resources_is_unobservable_in_both_phases_and_fails_closed_if_compared() -> None:
    """`map_settings.resources` is a measured negative result (T218: all three candidate keys
    nil), not missing work: never dispatched, reported in its own `unobservable` bucket with the
    spike-backed reason, and still failing closed for any caller that compares it directly."""
    for state_name in ("HostGame", "InGame"):
        snapshot, lua_body = _read(
            state_name=state_name,
            result={"turn_timer_type": "TURNTIMER_NONE"},
            fields=("map_settings.resources",),
        )
        assert "RESOURCES" not in lua_body, f"resources was dispatched at {state_name}"
        assert "map_settings.resources" in snapshot.unobservable
        assert "map_settings.resources" not in snapshot.unread
        assert "map_settings.resources" not in snapshot.phase_deferred
        value = snapshot.read_setting("map_settings.resources")
        assert isinstance(value, UnreadSetting)
        assert "unobservable" in value.reason


def test_the_unobservable_registry_names_exactly_the_live_confirmed_field() -> None:
    """The registry is a set of measured facts, not a dumping ground: today it holds exactly the
    one field the peer's live session confirmed unreadable in either phase. Growing it requires
    the same standard of evidence -- this assertion is the reminder."""
    assert set(UNOBSERVABLE_SETTING_FIELDS) == {"map_settings.resources"}
    assert "T218" in UNOBSERVABLE_SETTING_FIELDS["map_settings.resources"]
