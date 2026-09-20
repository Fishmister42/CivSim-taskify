"""Unit tests for the FireTuner save capability (T078) and its filesystem
verification counterpart (T079, exercised together here since they are the
two halves of one turn-start quicksave).

Covers, per the T077 spike's own outcome (Outcome A: a verified FireTuner
Lua save path -- specs/002-civ-playing-harness/spikes/r5-save-path.md):

- :class:`~civsim_harness.saves.save_game.LuaSaveCapability` issues the
  verified ``Network.SaveGame`` call in the ``InGame`` context, using a real
  (unmodified) ``NexusClient`` against ``tests/fakes/fake_nexus.py``'s
  ``FakeNexusServer`` -- not a hand-rolled stand-in for the transport.
- The spike's own warning -- ``Network.SaveGame`` returns ``true``
  immediately while the file lands asynchronously -- means a save the Lua
  call *issued* successfully must still fail the turn if the ``.Civ6Save``
  never actually appears on disk (``saves/verify.py``'s job, never
  ``save_game.py``'s).
- ``saves/verify.py``'s existing size-stability rule (T079) rejects a file
  still being written.
- The T077 spike's fresh-install finding -- the save directory did not
  exist until the first save created it -- means a directory that simply
  does not exist yet must never itself be treated as a distinguished
  failure, in ``saves/verify.py`` or in ``host/_shared.py``'s disk-space
  read (the ``host/_shared.py`` fix this task made for exactly that trap).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from civsim_harness.errors import NexusError
from civsim_harness.host._shared import read_disk_space
from civsim_harness.host.port import GameDirectories
from civsim_harness.nexus.client import NexusClient
from civsim_harness.saves.save_game import LuaSaveCapability
from civsim_harness.saves.verify import (
    SAVE_FILE_SUFFIX,
    SaveVerificationError,
    resolve_saves_dir,
    verify_save,
)
from fakes.fake_host import FakeHostPlatform
from fakes.fake_nexus import (
    STATE_INDEX_GAME_CORE_TUNER,
    STATE_INDEX_IN_GAME,
    FakeNexusServer,
    loaded_game_state_table,
)

_SAVE_NAME = "civsim__abc123__t0007"


def _executor(client: NexusClient) -> Callable[[int, str], Awaitable[Any]]:
    async def _execute(state_index: int, lua_body: str) -> Any:
        return await client.execute_command(state_index=state_index, lua_body=lua_body)

    return _execute


# --------------------------------------------------------------------------
# LuaSaveCapability: the correct call, in the correct context
# --------------------------------------------------------------------------


async def test_lua_save_capability_issues_the_verified_call_in_the_in_game_context() -> None:
    server = FakeNexusServer()
    server.queue_response(
        {"issued": True, "save_name": _SAVE_NAME, "error": ""},
        match="Network.SaveGame",
        state_index=STATE_INDEX_IN_GAME,
    )
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        await client.connect()
        await client.resolve_game_states()
        # The client itself is a valid resolver source (it exposes
        # `.state_indices`) -- LuaSaveCapability asks it for the current
        # InGame index on every call rather than caching one.
        capability = LuaSaveCapability(_executor(client), in_game_state_index=client)

        await capability.save_game(_SAVE_NAME)
    finally:
        await client.close()
        await server.stop()

    assert len(server.received) == 1
    received = server.received[0]
    # The correct context: InGame, never GameCore_Tuner (research R3; the
    # T077 spike directly confirmed `Network` does not exist at all in
    # GameCore_Tuner).
    assert received.state_index == STATE_INDEX_IN_GAME
    assert received.state_index != STATE_INDEX_GAME_CORE_TUNER
    # The correct call: the verified shape from the spike, not a guess.
    assert "Network.SaveGame" in received.lua_body
    assert "SaveLocations.LOCAL_STORAGE" in received.lua_body
    assert "SaveTypes.SINGLE_PLAYER" in received.lua_body
    assert f'"{_SAVE_NAME}"' in received.lua_body


async def test_capability_built_before_a_phase_change_uses_the_new_index_afterwards() -> None:
    # Live-confirmed hazard (this module's docstring): the same state name
    # can sit at a different index after a phase transition or reconnect --
    # LoadGameMenu is 18 at Create Game but 112 once in game. A
    # LuaSaveCapability constructed early must never keep replaying the
    # index that was true at construction time.
    server = FakeNexusServer()
    server.queue_response({"issued": True, "save_name": _SAVE_NAME, "error": ""})
    server.queue_response({"issued": True, "save_name": _SAVE_NAME, "error": ""})
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        await client.connect()
        await client.resolve_game_states()
        # Built while InGame sits at the original index -- and never
        # rebuilt afterwards.
        capability = LuaSaveCapability(_executor(client), in_game_state_index=client)

        await capability.save_game(_SAVE_NAME)

        # A phase transition moves InGame to a different index; the run
        # sequence would call refresh_state_indices() at exactly this kind
        # of known boundary.
        new_in_game_index = STATE_INDEX_IN_GAME + 100
        server.set_state_table(
            loaded_game_state_table(
                game_core_tuner=STATE_INDEX_GAME_CORE_TUNER + 100,
                in_game=new_in_game_index,
            )
        )
        await client.refresh_state_indices()

        await capability.save_game(_SAVE_NAME)
    finally:
        await client.close()
        await server.stop()

    assert len(server.received) == 2
    # The old index the capability first saw -- never replayed after the
    # phase change.
    assert server.received[0].state_index == STATE_INDEX_IN_GAME
    # The new index, resolved fresh on the second call rather than the
    # stale one captured at construction.
    assert server.received[1].state_index == new_in_game_index
    assert server.received[1].state_index != STATE_INDEX_IN_GAME


def test_constructing_with_a_raw_int_state_index_is_rejected() -> None:
    # The removed footgun: a bare int captured once at construction can go
    # silently stale after a reconnect or phase transition. There is no
    # backward-compatible path for it -- construction must fail immediately
    # and loudly instead.
    async def _execute(state_index: int, lua_body: str) -> Any:  # pragma: no cover
        raise AssertionError("must never be called")

    with pytest.raises(TypeError):
        LuaSaveCapability(_execute, in_game_state_index=STATE_INDEX_IN_GAME)  # type: ignore[arg-type]


async def test_lua_save_capability_raises_when_the_lua_call_itself_errors() -> None:
    # The Lua-side pcall failing (e.g. a future client removing/renaming
    # Network.SaveGame) is a real failure this module must surface, never
    # swallow.
    server = FakeNexusServer()
    server.queue_response(
        {"issued": False, "save_name": _SAVE_NAME, "error": "attempt to call a nil value"},
        match="Network.SaveGame",
    )
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        await client.connect()
        await client.resolve_game_states()
        capability = LuaSaveCapability(_executor(client), in_game_state_index=client)

        with pytest.raises(NexusError):
            await capability.save_game(_SAVE_NAME)
    finally:
        await client.close()
        await server.stop()


# --------------------------------------------------------------------------
# Issuance is not verification: a save that never lands on disk fails the
# turn, even though the Lua call reported success (the spike's own warning:
# `Network.SaveGame` returns `true` immediately, asynchronously afterward).
# --------------------------------------------------------------------------


async def test_a_save_that_never_appears_on_disk_fails_verification_despite_being_issued(
    tmp_path: Path,
) -> None:
    server = FakeNexusServer()
    server.queue_response({"issued": True, "save_name": _SAVE_NAME, "error": ""})
    await server.start()
    client = NexusClient(host="127.0.0.1", port=server.port)
    try:
        await client.connect()
        await client.resolve_game_states()
        capability = LuaSaveCapability(_executor(client), in_game_state_index=client)
        # The Lua call is issued and reports success -- but the fake
        # transport never touches a real filesystem, exactly like the real
        # game between the call returning and the file actually landing.
        await capability.save_game(_SAVE_NAME)
    finally:
        await client.close()
        await server.stop()

    host = FakeHostPlatform()
    host.set_directories(
        GameDirectories(
            saves_dir=tmp_path / "Saves" / "Single", app_options_path=tmp_path / "AppOptions.txt"
        )
    )

    # The turn must fail rather than record this quicksave as taken (FR-007):
    # "issued" is never treated as proof of "on disk".
    with pytest.raises(SaveVerificationError):
        verify_save(host, _SAVE_NAME, sleep=lambda _seconds: None)


# --------------------------------------------------------------------------
# T079's existing size-stability rule, kept exactly as it is
# --------------------------------------------------------------------------


def test_size_unstable_file_is_not_accepted_yet(tmp_path: Path) -> None:
    saves_dir = tmp_path / "Saves" / "Single"
    saves_dir.mkdir(parents=True)
    save_path = saves_dir / f"{_SAVE_NAME}{SAVE_FILE_SUFFIX}"
    save_path.write_bytes(b"x" * 100)

    host = FakeHostPlatform()
    host.set_directories(
        GameDirectories(saves_dir=saves_dir, app_options_path=tmp_path / "AppOptions.txt")
    )

    def _grow_between_reads(_seconds: float) -> None:
        # Simulate the game still writing the file between the two reads.
        save_path.write_bytes(b"x" * 200)

    with pytest.raises(SaveVerificationError, match="not yet stable"):
        verify_save(host, _SAVE_NAME, sleep=_grow_between_reads)


def test_stable_file_is_accepted(tmp_path: Path) -> None:
    saves_dir = tmp_path / "Saves" / "Single"
    saves_dir.mkdir(parents=True)
    save_path = saves_dir / f"{_SAVE_NAME}{SAVE_FILE_SUFFIX}"
    save_path.write_bytes(b"x" * 681096)

    host = FakeHostPlatform()
    host.set_directories(
        GameDirectories(saves_dir=saves_dir, app_options_path=tmp_path / "AppOptions.txt")
    )

    verified = verify_save(host, _SAVE_NAME, sleep=lambda _seconds: None)

    assert verified.path == save_path
    assert verified.size_bytes == 681096


# --------------------------------------------------------------------------
# The fresh-install trap the T077 spike found: a save directory that does
# not exist *yet* must never itself be a distinguished failure.
# --------------------------------------------------------------------------


def test_resolving_the_save_directory_never_requires_it_to_pre_exist(tmp_path: Path) -> None:
    host = FakeHostPlatform()
    saves_dir = tmp_path / "Saves" / "Single"
    assert not saves_dir.exists()
    host.set_directories(
        GameDirectories(saves_dir=saves_dir, app_options_path=tmp_path / "AppOptions.txt")
    )

    # Resolution itself must never raise merely because the directory has
    # not been created yet (research R5, T077 spike: this is exactly what
    # happens on a fresh install before the first save).
    resolved = resolve_saves_dir(host)
    assert resolved == saves_dir


def test_missing_save_directory_produces_the_same_verification_failure_as_a_missing_file(
    tmp_path: Path,
) -> None:
    host = FakeHostPlatform()
    saves_dir = tmp_path / "Saves" / "Single"  # never created
    host.set_directories(
        GameDirectories(saves_dir=saves_dir, app_options_path=tmp_path / "AppOptions.txt")
    )

    # A genuinely missing quicksave still (correctly) fails -- the point is
    # only that "the directory itself never existed" is not a special case
    # that fails differently or earlier than "the file itself is absent".
    with pytest.raises(SaveVerificationError):
        verify_save(host, _SAVE_NAME, sleep=lambda _seconds: None)


def test_read_disk_space_resolves_even_when_the_leaf_directory_does_not_exist(
    tmp_path: Path,
) -> None:
    # host/_shared.py's read_disk_space fix: shutil.disk_usage requires an
    # existing path (raises FileNotFoundError on Windows for a merely
    # missing leaf directory), which would otherwise make a preflight
    # headroom check false-negative on exactly the fresh-install case the
    # T077 spike found. `tmp_path` exists; its `Saves/Single` leaf does not.
    missing = tmp_path / "Saves" / "Single"
    assert not missing.exists()

    space = read_disk_space(missing)

    # The originally requested path is preserved in the result even though
    # an existing ancestor was used internally to compute the usage.
    assert space.path == missing
    assert space.total_bytes >= space.free_bytes >= 0
