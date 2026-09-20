"""R5/T077 on Windows: does a FireTuner Lua save path exist, and does it write a file?

Runs against a live, in-game client through the harness's own `NexusClient`,
with the three live-client read-discipline defects patched locally (see
`LiveNexusClient`). Those patches are the spike's other finding and belong to
`nexus/client.py`, which is outside this agent's lane.

Verification is filesystem-based and asserts on what the **far side** produced:
`Network.SaveGame` returns immediately, so the return value proves nothing. The
`.Civ6Save` must appear on disk with a size stable across two reads.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from civsim_harness.host.windows.adapter import WindowsHostPlatform
from civsim_harness.nexus.client import NexusClient, StateIndices
from civsim_harness.nexus.codec import TAG_HANDSHAKE
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json

OUT = Path(__file__).resolve().parent
lines: list[str] = []

#: Observed live: every `print()` line comes back on tag -1, as
#: ``O\0<StateName>: <the printed text>``. Neither the tag nor the prefix is
#: mentioned in contracts/nexus-protocol.md.
PRINT_TAG = -1


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    lines.append(line)


def _strip_print_prefix(payload: str) -> str:
    """``O\\0InGame: ---BEGIN:x---`` -> ``---BEGIN:x---``."""
    if payload.startswith("O\x00"):
        payload = payload[2:]
        head, sep, tail = payload.partition(": ")
        if sep and "\x00" not in head:
            return tail
        return payload
    return payload


class LiveNexusClient(NexusClient):
    """`NexusClient` corrected against an actual Civilization VI client.

    Three defects, all the same mistake -- "the next frame is my answer":

    1. `_handshake` sends `APP:` and never reads its reply, then treats that
       reply as the `LSQ:` response. Fatal: the identification payload
       ``Civ6\\0Sid Meier's Civilization 6\\0<path>`` has an odd field count.
    2. `_query_states` raises on any frame that is not `TAG_HANDSHAKE`, but the
       client interleaves asynchronous log frames into the same stream.
    3. `_await_result` feeds only `TAG_COMMAND` (3) payloads to the sentinel
       correlator. The client returns an **empty** tag-3 frame as a bare
       acknowledgement and sends the actual printed output on **tag -1**, so
       the correlator never sees the nonce and every command times out --
       even though the Lua ran.
    """

    async def _handshake(self) -> StateIndices:
        await self._send_raw(TAG_HANDSHAKE, f"APP:{self._app_name}")
        hello = await self._read_tagged(TAG_HANDSHAKE)
        say(f"APP: reply -> {hello.payload[:90]!r}")
        return await self._query_states()

    async def _read_tagged(self, tag: int):
        while True:
            frame = await self._read_frame()
            if frame.tag == tag:
                return frame

    async def _query_states(self) -> StateIndices:
        from civsim_harness.nexus.client import _parse_state_list

        await self._send_raw(TAG_HANDSHAKE, "LSQ:")
        frame = await self._read_tagged(TAG_HANDSHAKE)
        by_name = _parse_state_list(frame.payload)
        return StateIndices(
            by_name=by_name,
            game_core_tuner=by_name.get("GameCore_Tuner"),
            in_game=by_name.get("InGame"),
        )

    async def _await_result(self, nonce: str) -> str:
        while True:
            result = self._correlator.take_result(nonce)
            if result is not None:
                return result
            frame = await self._read_frame()
            if frame.tag == PRINT_TAG:
                self._correlator.feed(_strip_print_prefix(frame.payload) + "\n")


async def run() -> int:
    host = WindowsHostPlatform()
    dirs = host.resolve_game_directories()
    say(f"saves dir : {dirs.saves_dir}")

    client = LiveNexusClient(app_name="civsim_harness", connect_timeout_s=6.0)
    await client.connect()
    indices = await client.resolve_game_states()
    say(f"IN GAME: {len(indices.by_name)} states, "
        f"GameCore_Tuner={indices.game_core_tuner}, InGame={indices.in_game}")

    results: dict = {"states": len(indices.by_name),
                     "in_game_index": indices.in_game,
                     "game_core_tuner_index": indices.game_core_tuner}

    async def lua(state_index: int, body: str, timeout: float = 25.0):
        return await client.execute_command(
            state_index=state_index, lua_body=body, timeout_s=timeout
        )

    # 1. Read real game state through the harness's own transport.
    state = await lua(indices.in_game, LUA_JSON_PRELUDE + lua_print_json({
        "turn": "Game.GetCurrentGameTurn()",
        "local_player": "Game.GetLocalPlayer()",
        "lua_version": "_VERSION",
    }))
    say(f"GAME STATE -> {json.dumps(state)}")
    results["game_state"] = state

    # 2. Does the save API exist here?
    surface = await lua(indices.in_game, LUA_JSON_PRELUDE + lua_print_json({
        "Network": "type(Network)",
        "Network_SaveGame": 'type(Network) == "table" and type(Network.SaveGame) or "n/a"',
        "Network_LoadGame": 'type(Network) == "table" and type(Network.LoadGame) or "n/a"',
        "SaveLocations_LOCAL_STORAGE": "tostring(SaveLocations.LOCAL_STORAGE)",
        "SaveTypes_SINGLE_PLAYER": "tostring(SaveTypes.SINGLE_PLAYER)",
    }))
    say(f"SAVE SURFACE -> {json.dumps(surface)}")
    results["save_surface"] = surface

    # 3. Take four saves, exactly as the Linux spike did, and verify each on disk.
    saves = []
    for i in range(1, 5):
        name = f"civsim__winspike__t{i:04d}"
        body = LUA_JSON_PRELUDE + (
            "local g = {}; "
            f'g.Name = "{name}"; '
            "g.Location = SaveLocations.LOCAL_STORAGE; "
            "g.Type = SaveTypes.SINGLE_PLAYER; "
            "g.IsAutosave = false; g.IsQuicksave = false; "
            "local ok, err = pcall(function() return Network.SaveGame(g) end); "
        ) + lua_print_json({"pcall_ok": "tostring(ok)", "returned": "tostring(err)",
                            "turn": "Game.GetCurrentGameTurn()"})
        res = await lua(indices.in_game, body, timeout=30.0)
        target = dirs.saves_dir / f"{name}.Civ6Save"

        stable = None
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if target.exists():
                a = target.stat().st_size
                await asyncio.sleep(0.5)
                if target.exists() and target.stat().st_size == a and a > 0:
                    stable = a
                    break
            await asyncio.sleep(0.4)
        say(f"SAVE {name}: lua={json.dumps(res)} exists={target.exists()} stable_size={stable}")
        saves.append({"name": name, "lua": res, "path": str(target),
                      "exists": target.exists(), "stable_size": stable})
    results["saves"] = saves
    results["saves_written"] = sum(1 for s in saves if s["stable_size"])

    await client.close()
    results["transcript"] = lines
    (OUT / "r5_save_spike_results.json").write_text(json.dumps(results, indent=2, default=str))
    say(f"WROTE {results['saves_written']}/4 verified saves; results json written")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
