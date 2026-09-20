"""T204 hardening items 1 and 2, plus the leader-selection seam -- host-config preflight checks
(`run/preparation.py`).

1. :func:`~civsim_harness.run.preparation.debug_menu_preflight` -- reads ``EnableDebugMenu`` from
   ``AppOptions.txt`` and *records* it. Never refuses a run over it: a live-client spike
   (``specs/002-civ-playing-harness/spikes/principle-i-debugmenu-linux.md``) found the tuner's own
   callable surface byte-identical with the debug menu on and off across three separate
   comparisons, so there is no parity basis for treating this as a gate.
2. :func:`~civsim_harness.run.preparation.turn_timer_preflight` -- verifies, via an injected
   reader, that this run is not using a turn timer. A live, measured finding
   (``GameConfiguration.GetTurnTimerType()`` returning ``TURNTIMER_STANDARD`` in an ordinary
   single-player game, turns advancing 1 -> 6 with zero input once one end-turn was issued) makes
   this a hard preflight gate: verified-none proceeds, verified-active refuses the run outright
   (FR-011, FR-014, FR-015/SC-022 would otherwise all be silently corrupted), and undeterminable
   proceeds but records an explicit *unverified* precondition rather than silently assuming safe.
3. :func:`~civsim_harness.run.preparation.apply_and_verify_leader_selection` (the sync,
   injectable, unit-testable gate) and :class:`~civsim_harness.run.preparation.
   LuaLeaderSelectionApplier` (the concrete, live, VERIFIED binding -- Linux client, ``HostGame``
   Lua state, 2026-09-20 capture: ``PlayerConfigurations[0]:SetLeaderTypeName``/
   ``:SetCivilizationTypeName``, plain strings, read back through ``PlayerConfigurations`` and
   never the Create Game UI). The ``CivSim DEFAULT`` preset does not carry a civilization/leader
   selection at all (``spikes/civsim-default-preset-linux.md``: every player slot reads back
   ``civ=nil leader=nil``), so preparation must set it and read it back before turn 1; a write
   that cannot be verified must fail the run, never proceed unconfirmed (FR-002, V2, V3).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.config.run_config import load_run_configuration_file
from civsim_harness.errors import PreflightError
from civsim_harness.host.port import GameDirectories
from civsim_harness.models.config import RunConfiguration
from civsim_harness.nexus.client import NexusClient
from civsim_harness.run.preparation import (
    DebugMenuPreflightResult,
    DebugMenuState,
    LeaderSelectionOutcome,
    LeaderSelectionWriteResult,
    LeaderSelectionWriteStatus,
    LuaLeaderSelectionApplier,
    TurnTimerPreconditionState,
    TurnTimerReading,
    TurnTimerReadStatus,
    apply_and_verify_leader_selection,
    debug_menu_preflight,
    turn_timer_preflight,
)
from fakes.fake_host import FakeHostPlatform
from fakes.fake_nexus import FakeNexusServer, menu_only_state_table

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "configs" / "turn50-validation.yaml"

# --------------------------------------------------------------------------
# Hardening item 1 -- EnableDebugMenu: present (on/off), absent, unreadable
# --------------------------------------------------------------------------


def _host_with_app_options(
    tmp_path: Path, *, contents: str | None
) -> tuple[FakeHostPlatform, Path]:
    """A `FakeHostPlatform` whose `resolve_game_directories()` points at a real, on-disk
    `AppOptions.txt` under *tmp_path* -- `contents=None` leaves the file (and its directory)
    entirely absent, exactly the "fresh install" case `debug_menu_preflight`'s own docstring
    names.
    """
    saves_dir = tmp_path / "Saves" / "Single"
    app_options_path = tmp_path / "AppOptions.txt"
    if contents is not None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        app_options_path.write_text(contents, encoding="utf-8")
    host = FakeHostPlatform()
    host.set_directories(GameDirectories(saves_dir=saves_dir, app_options_path=app_options_path))
    return host, app_options_path


def test_debug_menu_preflight_present_enabled(tmp_path: Path) -> None:
    host, path = _host_with_app_options(
        tmp_path, contents="EnableTuner 1\nEnableDebugMenu 1    # [Debug]\n"
    )
    result = debug_menu_preflight(host)
    assert result == DebugMenuPreflightResult(
        state=DebugMenuState.ENABLED, app_options_path=path
    )


def test_debug_menu_preflight_present_disabled(tmp_path: Path) -> None:
    host, path = _host_with_app_options(
        tmp_path, contents="EnableTuner 1        # [Debug]\nEnableDebugMenu 0    # [Debug]\n"
    )
    result = debug_menu_preflight(host)
    assert result == DebugMenuPreflightResult(
        state=DebugMenuState.DISABLED, app_options_path=path
    )


def test_debug_menu_preflight_absent_file_is_unknown_not_a_crash(tmp_path: Path) -> None:
    """The client rewrites `AppOptions.txt` on exit and its directory need not pre-exist on a
    fresh install (the same accommodation `host._shared.read_disk_space` already needed) -- a
    missing file must never raise out of preflight."""
    missing_root = tmp_path / "fresh-install-never-launched"
    host, path = _host_with_app_options(missing_root, contents=None)
    assert not path.exists()

    result = debug_menu_preflight(host)

    assert result.state is DebugMenuState.UNKNOWN
    assert result.app_options_path == path
    assert result.detail is not None
    assert "does not exist" in result.detail


def test_debug_menu_preflight_present_but_no_entry_is_unknown(tmp_path: Path) -> None:
    host, path = _host_with_app_options(tmp_path, contents="EnableTuner 1\nPlayIntroVideo 0\n")
    result = debug_menu_preflight(host)
    assert result.state is DebugMenuState.UNKNOWN
    assert result.detail is not None
    assert "no EnableDebugMenu entry" in result.detail


def test_debug_menu_preflight_unreadable_file_is_unknown_not_a_crash(tmp_path: Path) -> None:
    """A file that exists but cannot be read as text (simulated here with a directory in its
    place, which raises an `OSError` on every platform when opened for reading) must be reported
    the same way as a missing file -- `UNKNOWN`, never raised."""
    root = tmp_path / "unreadable-host"
    root.mkdir()
    app_options_path = root / "AppOptions.txt"
    app_options_path.mkdir()  # a directory where a file is expected -- unreadable as text
    host = FakeHostPlatform()
    host.set_directories(
        GameDirectories(saves_dir=root / "Saves" / "Single", app_options_path=app_options_path)
    )

    result = debug_menu_preflight(host)

    assert result.state is DebugMenuState.UNKNOWN
    assert result.app_options_path == app_options_path
    assert result.detail is not None
    assert "could not be read" in result.detail


def test_debug_menu_preflight_never_raises_regardless_of_value() -> None:
    """Structural guard: whatever `AppOptions.txt` says, `debug_menu_preflight` only ever
    *reports* -- it must never refuse a run over this setting (no parity basis for a gate, per the
    live spike's own evidence). Walks the actual AST (not the raw source text, which would
    false-positive on the word "raise" appearing in the function's own docstring) for a `raise`
    statement anywhere in the function body."""
    import ast
    import inspect
    import textwrap

    from civsim_harness.run import preparation

    source = textwrap.dedent(inspect.getsource(preparation.debug_menu_preflight))
    tree = ast.parse(source)
    raises = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]
    assert raises == []


def test_debug_menu_preflight_uses_the_same_app_options_path_as_enable_tuner(
    tmp_path: Path,
) -> None:
    """`EnableDebugMenu` is read from the exact path `resolve_game_directories()` resolves --
    the same file `EnableTuner` already lives in (research R1) -- never a path this function
    invents on its own."""
    host, path = _host_with_app_options(tmp_path, contents="EnableDebugMenu 0\n")
    result = debug_menu_preflight(host)
    assert result.app_options_path == host.resolve_game_directories().app_options_path
    assert result.app_options_path == path


# --------------------------------------------------------------------------
# Hardening item 2 -- turn-timer preflight: verified-off, verified-on, undeterminable
# --------------------------------------------------------------------------


def test_turn_timer_preflight_verified_none_proceeds() -> None:
    reading = TurnTimerReading(
        status=TurnTimerReadStatus.DETERMINED,
        turn_timer_type="TURNTIMER_NONE",
        turn_timer_hash=-1525060181,
    )
    result = turn_timer_preflight(read_turn_timer=lambda: reading)
    assert result.state is TurnTimerPreconditionState.VERIFIED_NONE
    assert result.turn_timer_type == "TURNTIMER_NONE"
    assert result.turn_timer_hash == -1525060181


def test_turn_timer_preflight_accepts_the_other_confirmed_no_timer_name() -> None:
    """`TURNTIMER_NONE` and `NO_TURNTIMER` are distinct, build-dependent hashes on the probed
    build -- both must be accepted since which name a given build actually uses is not assumed
    here (the live finding's own instruction: "accept either if you cannot tell")."""
    reading = TurnTimerReading(
        status=TurnTimerReadStatus.DETERMINED,
        turn_timer_type="NO_TURNTIMER",
        turn_timer_hash=-1206781825,
    )
    result = turn_timer_preflight(read_turn_timer=lambda: reading)
    assert result.state is TurnTimerPreconditionState.VERIFIED_NONE
    assert result.turn_timer_type == "NO_TURNTIMER"


def test_turn_timer_preflight_verified_active_refuses_the_run() -> None:
    """FR-011/FR-014/FR-015/SC-022: a run started with a turn timer active would produce a
    clean-looking but meaningless dataset -- this must fail the run, not merely record it."""
    reading = TurnTimerReading(
        status=TurnTimerReadStatus.DETERMINED,
        turn_timer_type="TURNTIMER_STANDARD",
        turn_timer_hash=2133509568,
    )
    with pytest.raises(PreflightError) as excinfo:
        turn_timer_preflight(read_turn_timer=lambda: reading)
    assert excinfo.value.detail["turn_timer_type"] == "TURNTIMER_STANDARD"
    assert excinfo.value.detail["turn_timer_hash"] == 2133509568


@pytest.mark.parametrize(
    "turn_timer_type,turn_timer_hash",
    [
        ("TURNTIMER_STANDARD", 2133509568),
        ("TURNTIMER_DYNAMIC", 698670180),
        ("TURNTIMER_FIXED", 1033907547),
    ],
)
def test_turn_timer_preflight_refuses_every_active_timer_type(
    turn_timer_type: str, turn_timer_hash: int
) -> None:
    reading = TurnTimerReading(
        status=TurnTimerReadStatus.DETERMINED,
        turn_timer_type=turn_timer_type,
        turn_timer_hash=turn_timer_hash,
    )
    with pytest.raises(PreflightError):
        turn_timer_preflight(read_turn_timer=lambda: reading)


def test_turn_timer_preflight_undeterminable_records_unverified_not_silently_pass() -> None:
    """The reader could not resolve an answer at all -- this must **not** raise, and must **not**
    default to treating the precondition as satisfied (that is the exact assumption the retracted
    auto-end-turn finding showed is dangerous); it must be recorded as explicitly unverified."""
    reading = TurnTimerReading(
        status=TurnTimerReadStatus.UNDETERMINABLE,
        reason="GameConfiguration is not available before a game is loaded",
    )
    result = turn_timer_preflight(read_turn_timer=lambda: reading)
    assert result.state is TurnTimerPreconditionState.UNVERIFIED
    assert result.turn_timer_type is None
    assert result.turn_timer_hash is None
    assert result.reason == "GameConfiguration is not available before a game is loaded"


def test_turn_timer_reading_requires_a_reason_when_undeterminable() -> None:
    with pytest.raises(ValueError):
        TurnTimerReading(status=TurnTimerReadStatus.UNDETERMINABLE, reason=None)


def test_turn_timer_reading_requires_a_type_when_determined() -> None:
    with pytest.raises(ValueError):
        TurnTimerReading(status=TurnTimerReadStatus.DETERMINED, turn_timer_type=None)


def test_turn_timer_preflight_never_defaults_to_assuming_no_timer() -> None:
    """Structural guard against the exact failure mode this hardening item exists to prevent: no
    code path in `turn_timer_preflight` may construct a `VERIFIED_NONE` result without the
    injected reader itself having reported `DETERMINED` with a no-timer name. Walks the actual
    AST (not the raw source text, which would false-positive on "VERIFIED_NONE" appearing in the
    function's own docstring) for every `TurnTimerPreconditionState.VERIFIED_NONE` attribute
    access in the function body -- there must be exactly one, and it must sit inside the `if`
    branch that checks `reading.turn_timer_type in _NO_TIMER_NAMES`.
    """
    import ast
    import inspect
    import textwrap

    from civsim_harness.run import preparation

    source = textwrap.dedent(inspect.getsource(preparation.turn_timer_preflight))
    tree = ast.parse(source)
    verified_none_refs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "VERIFIED_NONE"
    ]
    assert len(verified_none_refs) == 1

    # And that one reference is reached only from inside an `if` statement whose test mentions
    # `_NO_TIMER_NAMES` -- i.e. it is not a bare/default construction reachable unconditionally.
    guarding_ifs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and "_NO_TIMER_NAMES" in ast.dump(node.test)
    ]
    assert len(guarding_ifs) == 1
    guard = guarding_ifs[0]
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "VERIFIED_NONE" for n in ast.walk(guard)
    )


# --------------------------------------------------------------------------
# Leader-selection seam, part 1 -- apply_and_verify_leader_selection: the
# sync, injectable, unit-testable gate (any apply_leader_selection/
# read_setting pair can drive it, including a plain test double here)
# --------------------------------------------------------------------------


def _leader_config() -> RunConfiguration:
    """`turn50-validation.yaml`'s own config, restated with the seed set's civilization/leader
    (`CIVILIZATION_PERSIA`/`LEADER_CYRUS`) -- civilization/leader are not frozen on
    `RunConfiguration` itself (only on `SeedSet`), so `model_copy` is a legitimate way to get a
    config carrying a specific pair for these tests without hand-building a whole
    `RunConfiguration`."""
    config = load_run_configuration_file(CONFIG_PATH)
    return config.model_copy(
        update={"civilization": "CIVILIZATION_PERSIA", "leader": "LEADER_CYRUS"}
    )


def test_leader_selection_verified_when_write_succeeds_and_readback_agrees() -> None:
    config = _leader_config()
    applied: dict[str, str] = {}

    def apply_leader_selection(civilization: str, leader: str) -> LeaderSelectionWriteResult:
        applied["civilization"] = civilization
        applied["leader"] = leader
        return LeaderSelectionWriteResult(status=LeaderSelectionWriteStatus.APPLIED)

    result = apply_and_verify_leader_selection(
        config,
        apply_leader_selection=apply_leader_selection,
        read_setting=applied.__getitem__,
    )

    assert result.outcome is LeaderSelectionOutcome.VERIFIED
    assert result.matched is True
    assert result.mismatches == ()
    assert applied == {"civilization": "CIVILIZATION_PERSIA", "leader": "LEADER_CYRUS"}


def test_leader_selection_mismatch_after_readback_fails_before_turn_1_with_mismatch_recorded() -> (
    None
):
    """FR-002/V2: a write that reports success but reads back differently must fail -- never
    silently accepted -- with the mismatch recorded so a caller can build the same
    `preparation_mismatch` event `verify_configuration`'s own mismatches already build (see
    `tests/integration/test_preparation.py`'s worked caller example)."""
    config = _leader_config()

    def apply_leader_selection(civilization: str, leader: str) -> LeaderSelectionWriteResult:
        return LeaderSelectionWriteResult(status=LeaderSelectionWriteStatus.APPLIED)

    # The client reports back a different civilization/leader than what was configured -- e.g.
    # the write silently landed on the wrong slot.
    readback = {"civilization": "CIVILIZATION_GREECE", "leader": "LEADER_PERICLES"}

    result = apply_and_verify_leader_selection(
        config, apply_leader_selection=apply_leader_selection, read_setting=readback.__getitem__
    )

    assert result.outcome is LeaderSelectionOutcome.MISMATCH
    assert result.matched is False  # fails before turn 1 -- never proceeds
    mismatched_fields = {m.field for m in result.mismatches}
    assert mismatched_fields == {"civilization", "leader"}
    recorded = {m.field: (m.expected, m.actual) for m in result.mismatches}
    assert recorded["civilization"] == ("CIVILIZATION_PERSIA", "CIVILIZATION_GREECE")
    assert recorded["leader"] == ("LEADER_CYRUS", "LEADER_PERICLES")


def test_leader_selection_partial_mismatch_names_only_the_differing_field() -> None:
    config = _leader_config()

    def apply_leader_selection(civilization: str, leader: str) -> LeaderSelectionWriteResult:
        return LeaderSelectionWriteResult(status=LeaderSelectionWriteStatus.APPLIED)

    readback = {"civilization": "CIVILIZATION_PERSIA", "leader": "LEADER_ALEXANDER"}

    result = apply_and_verify_leader_selection(
        config, apply_leader_selection=apply_leader_selection, read_setting=readback.__getitem__
    )

    assert result.outcome is LeaderSelectionOutcome.MISMATCH
    mismatched_fields = {m.field for m in result.mismatches}
    assert mismatched_fields == {"leader"}


def test_leader_selection_seam_unbound_fails_rather_than_silently_proceeding() -> None:
    """A caller standing in for "the real write is not wired up yet" reports `UNAVAILABLE` --
    this seam must fail closed on that, never silently treat an un-applied write as though it
    had succeeded."""
    config = _leader_config()

    def apply_leader_selection(civilization: str, leader: str) -> LeaderSelectionWriteResult:
        return LeaderSelectionWriteResult(
            status=LeaderSelectionWriteStatus.UNAVAILABLE,
            reason="leader-selection writer not wired up yet",
        )

    def read_setting(name: str) -> str:
        raise AssertionError("read_setting must not be called when the write is unavailable")

    result = apply_and_verify_leader_selection(
        config, apply_leader_selection=apply_leader_selection, read_setting=read_setting
    )

    assert result.outcome is LeaderSelectionOutcome.UNAVAILABLE
    assert result.matched is False
    mismatched_fields = {m.field for m in result.mismatches}
    assert mismatched_fields == {"civilization", "leader"}
    for mismatch in result.mismatches:
        assert "unavailable" in str(mismatch.actual)
        assert "not wired up yet" in str(mismatch.actual)


def test_leader_selection_write_result_requires_a_reason_when_unavailable() -> None:
    with pytest.raises(ValueError):
        LeaderSelectionWriteResult(status=LeaderSelectionWriteStatus.UNAVAILABLE, reason=None)


def test_leader_selection_never_verified_without_a_readback_confirming_both_fields() -> None:
    """Structural guard, mirroring `test_turn_timer_preflight_never_defaults_to_assuming_no_timer`
    above: `_compare_leader_selection` -- the one place either the sync gate or the live
    `LuaLeaderSelectionApplier` binding constructs `LeaderSelectionOutcome.VERIFIED` -- must reach
    it only by falling through its own `if mismatches:` check, never unconditionally."""
    import ast
    import inspect
    import textwrap

    from civsim_harness.run import preparation

    source = textwrap.dedent(inspect.getsource(preparation._compare_leader_selection))
    tree = ast.parse(source)
    verified_refs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "VERIFIED"
    ]
    assert len(verified_refs) == 1

    # And every other exit from this function returns MISMATCH -- there is no bare/default
    # branch that could produce VERIFIED without the `if mismatches:` check having run.
    return_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Return)]
    assert len(return_nodes) == 2


# --------------------------------------------------------------------------
# Leader-selection seam, part 2 -- LuaLeaderSelectionApplier: the concrete,
# live, VERIFIED binding. Exercised against a real, unmodified NexusClient
# over FakeNexusServer's real wire codec -- not a hand-rolled stand-in for
# the transport, matching tests/unit/test_save_game.py's own convention for
# LuaSaveCapability.
# --------------------------------------------------------------------------

_HOSTGAME_STATE_INDEX = 7


def _create_game_state_table() -> dict[str, int]:
    """A minimal Create Game screen state table (live capture: 31 states total, built from
    HostGame/MainMenu/StagingRoom/Lobby/Mods) -- only HostGame itself is needed for these
    tests."""
    return {"HostGame": _HOSTGAME_STATE_INDEX, "Main State": 0}


def _lua_executor(client: NexusClient) -> Callable[[int, str], Awaitable[Any]]:
    async def _execute(state_index: int, lua_body: str) -> Any:
        return await client.execute_command(state_index=state_index, lua_body=lua_body)

    return _execute


async def test_lua_leader_selection_applier_verified_via_playerconfigurations_readback() -> None:
    server = FakeNexusServer(state_table=_create_game_state_table())
    server.queue_response(
        {"issued": True, "error": ""},
        match="SetLeaderTypeName",
        state_index=_HOSTGAME_STATE_INDEX,
    )
    server.queue_response(
        {"civilization": "CIVILIZATION_PERSIA", "leader": "LEADER_CYRUS"},
        match="GetCivilizationTypeName",
        state_index=_HOSTGAME_STATE_INDEX,
    )
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        await client.connect()  # HostGame exists even before a game is loaded (live capture)
        applier = LuaLeaderSelectionApplier(_lua_executor(client), host_game_state_index=client)

        result = await applier.apply_and_verify("CIVILIZATION_PERSIA", "LEADER_CYRUS")
    finally:
        await client.close()
        await server.stop()

    assert result.outcome is LeaderSelectionOutcome.VERIFIED
    assert result.matched is True
    assert result.mismatches == ()
    assert len(server.received) == 2
    assert server.received[0].state_index == _HOSTGAME_STATE_INDEX
    assert server.received[1].state_index == _HOSTGAME_STATE_INDEX
    # Plain strings, verified live -- never DB.MakeHash.
    assert '"LEADER_CYRUS"' in server.received[0].lua_body
    assert '"CIVILIZATION_PERSIA"' in server.received[0].lua_body
    assert "DB.MakeHash" not in server.received[0].lua_body
    assert "PlayerConfigurations[0]" in server.received[0].lua_body
    # The verification is the PlayerConfigurations read-back, never a UI capture.
    assert "PlayerConfigurations[0]" in server.received[1].lua_body


async def test_lua_leader_selection_applier_mismatch_when_readback_disagrees() -> None:
    server = FakeNexusServer(state_table=_create_game_state_table())
    server.queue_response({"issued": True, "error": ""}, match="SetLeaderTypeName")
    server.queue_response(
        {"civilization": "CIVILIZATION_GREECE", "leader": "LEADER_PERICLES"},
        match="GetCivilizationTypeName",
    )
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        await client.connect()
        applier = LuaLeaderSelectionApplier(_lua_executor(client), host_game_state_index=client)

        result = await applier.apply_and_verify("CIVILIZATION_PERSIA", "LEADER_CYRUS")
    finally:
        await client.close()
        await server.stop()

    assert result.outcome is LeaderSelectionOutcome.MISMATCH
    assert result.matched is False
    mismatched_fields = {m.field for m in result.mismatches}
    assert mismatched_fields == {"civilization", "leader"}


async def test_lua_leader_selection_applier_unavailable_when_write_reports_failure() -> None:
    server = FakeNexusServer(state_table=_create_game_state_table())
    server.queue_response(
        {"issued": False, "error": "attempt to call a nil value"}, match="SetLeaderTypeName"
    )
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        await client.connect()
        applier = LuaLeaderSelectionApplier(_lua_executor(client), host_game_state_index=client)

        result = await applier.apply_and_verify("CIVILIZATION_PERSIA", "LEADER_CYRUS")
    finally:
        await client.close()
        await server.stop()

    assert result.outcome is LeaderSelectionOutcome.UNAVAILABLE
    assert result.matched is False
    assert len(server.received) == 1  # never attempted the read-back after a failed write
    for mismatch in result.mismatches:
        assert "unavailable" in str(mismatch.actual)


async def test_lua_leader_selection_applier_unavailable_when_hostgame_state_is_absent() -> None:
    """Before the Create Game screen (e.g. still at the main menu), `HostGame` is not in the
    state table at all -- this must fail closed as UNAVAILABLE, never raise out of
    `apply_and_verify`, and never silently proceed."""
    server = FakeNexusServer(state_table=menu_only_state_table())
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        await client.connect()
        applier = LuaLeaderSelectionApplier(_lua_executor(client), host_game_state_index=client)

        result = await applier.apply_and_verify("CIVILIZATION_PERSIA", "LEADER_CYRUS")
    finally:
        await client.close()
        await server.stop()

    assert result.outcome is LeaderSelectionOutcome.UNAVAILABLE
    assert len(server.received) == 0  # never even sent -- the index could not be resolved
    for mismatch in result.mismatches:
        assert "HostGame" in str(mismatch.actual)


def test_lua_leader_selection_applier_constructor_rejects_a_bare_int_state_index() -> None:
    """The same removed footgun `saves.save_game.LuaSaveCapability` already guards against: a
    bare int captured once at construction can go silently stale after a reconnect or phase
    transition -- construction must fail immediately and loudly instead."""

    async def _execute(state_index: int, lua_body: str) -> Any:  # pragma: no cover
        raise AssertionError("must never be called")

    with pytest.raises(TypeError):
        LuaLeaderSelectionApplier(_execute, host_game_state_index=_HOSTGAME_STATE_INDEX)  # type: ignore[arg-type]
