"""The live Windows demo: the harness's own NexusClient driving a real Civ VI client.

**Decisions here are scripted, not model-generated.** No OpenRouter key exists on
this machine and no model call is made anywhere in this file. What is being
demonstrated is the *harness's transport and execution path* against a live
client -- not an agent playing Civilization.

One deviation from stock `civsim_harness` is applied, and it is the finding of
this spike: `NexusClient._handshake()` sends `APP:` and then, without reading
the reply, sends `LSQ:` and reads the next frame -- so it parses the *APP:
response* as the state table. Against this client that is fatal:

    payload: "Civ6\\0Sid Meier's Civilization 6\\0C:\\...\\Binaries\\Debug"
    -> NexusError: odd number of NUL-separated fields

`_PatchedNexusClient` below reads the `APP:` reply before sending `LSQ:` and
changes nothing else. `nexus/client.py` is outside this agent's lane, so the
fix is demonstrated here rather than applied there.
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


OUT = Path(__file__).resolve().parent
transcript: list[str] = []


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    transcript.append(line)


class _PatchedNexusClient(NexusClient):
    """Stock `NexusClient` with the two live-client read-discipline defects fixed.

    DEFECT 1 -- `_handshake` never reads the `APP:` reply. It sends `APP:`, then
    sends `LSQ:` and reads the next frame, which is the *APP:* response, and
    parses that identification string as a state table. Fatal on this client.

    DEFECT 2 -- `_query_states` reads exactly one frame and raises if its tag is
    not `TAG_HANDSHAKE`. The client interleaves asynchronous **log** frames
    (`tag=-1`, e.g. ``O\\0StagingRoom: RefreshStatus()...``) into the same
    stream, so any log line emitted between the `LSQ:` send and its reply
    aborts the read with "got a different tag". Observed live on the first
    `refresh_state_indices()` call.

    Both are the same underlying mistake -- "the next frame is my answer" -- and
    both are in `nexus/client.py`, outside this agent's lane, so they are fixed
    here for the demo and reported rather than edited in place.
    """

    async def _handshake(self) -> StateIndices:
        await self._send_raw(TAG_HANDSHAKE, f"APP:{self._app_name}")
        hello = await self._read_handshake_frame()
        say(f"APP: reply (tag={hello.tag}) -> {hello.payload!r}")
        self._app_hello = hello.payload
        return await self._query_states()

    async def _read_handshake_frame(self):
        """Read until a TAG_HANDSHAKE frame; route anything else to telemetry."""
        while True:
            frame = await self._read_frame()
            if frame.tag == TAG_HANDSHAKE:
                return frame
            say(f"  (skipping async frame tag={frame.tag}: {frame.payload[:80]!r})")

    async def _query_states(self) -> StateIndices:
        from civsim_harness.nexus.client import _parse_state_list

        await self._send_raw(TAG_HANDSHAKE, "LSQ:")
        frame = await self._read_handshake_frame()
        by_name = _parse_state_list(frame.payload)
        return StateIndices(
            by_name=by_name,
            game_core_tuner=by_name.get("GameCore_Tuner"),
            in_game=by_name.get("InGame"),
        )


async def run() -> int:
    host = WindowsHostPlatform()
    dirs = host.resolve_game_directories()
    say(f"saves dir  : {dirs.saves_dir}")
    say(f"AppOptions : {dirs.app_options_path} (exists={dirs.app_options_path.exists()})")

    client = _PatchedNexusClient(app_name="civsim_harness", connect_timeout_s=6.0)
    indices = await client.connect()
    say(f"CONNECTED via harness NexusClient; {len(indices.by_name)} Lua states")
    say(f"  has_game_states={indices.has_game_states}")

    results: dict = {
        "app_hello": getattr(client, "_app_hello", None),
        "states_at_connect": dict(sorted(indices.by_name.items(), key=lambda kv: kv[1])),
    }

    async def lua(state: str, body: str, timeout: float = 20.0):
        idx = client.state_indices.by_name.get(state)
        if idx is None:
            say(f"  (state {state!r} not present)")
            return None
        try:
            return await client.execute_command(state_index=idx, lua_body=body, timeout_s=timeout)
        except Exception as exc:  # noqa: BLE001
            say(f"  lua in {state} FAILED: {type(exc).__name__}: {exc}")
            return {"__error__": f"{type(exc).__name__}: {exc}"}

    from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json

    # 1. A real round trip: prove the far side executed our Lua.
    probe = LUA_JSON_PRELUDE + lua_print_json(
        {"lua_version": "_VERSION", "echo": '"civsim-windows-live"'}
    )
    res = await lua("MainMenu", probe)
    say(f"ROUND TRIP in MainMenu -> {json.dumps(res)}")
    results["roundtrip"] = res

    # 2. What the front end actually exposes -- the load path (T217) needs this.
    surface = LUA_JSON_PRELUDE + lua_print_json({
        "Network": 'type(Network)',
        "Network_LoadGame": 'type(Network) == "table" and type(Network.LoadGame) or "n/a"',
        "Network_HostGame": 'type(Network) == "table" and type(Network.HostGame) or "n/a"',
        "SaveLocations": 'type(SaveLocations)',
        "SaveTypes": 'type(SaveTypes)',
        "ServerType": 'type(ServerType)',
        "GameConfiguration": 'type(GameConfiguration)',
        "UIManager": 'type(UIManager)',
    })
    res = await lua("MainMenu", surface)
    say(f"FRONT-END SURFACE -> {json.dumps(res)}")
    results["frontend_surface"] = res

    # 3. Load the most recent autosave -- owner ruling C's "start with a save
    #    loaded", reached through Lua rather than a command line.
    saves = sorted(
        (dirs.saves_dir / "auto").glob("*.Civ6Save"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    results["autosaves_found"] = [p.name for p in saves[:5]]
    if saves:
        stem = saves[0].stem
        say(f"attempting Network.LoadGame on {stem!r}")
        load = LUA_JSON_PRELUDE + (
            "local f = {}; "
            f'f.Name = "{stem}"; '
            'f.Location = SaveLocations.LOCAL_STORAGE; '
            'f.Type = SaveTypes.SINGLE_PLAYER; '
            'f.GameType = SaveTypes.SINGLE_PLAYER; '
            'f.IsAutosave = true; f.IsQuicksave = false; '
            "local ok, err = pcall(function() "
            "return Network.LoadGame(f, ServerType.SERVER_TYPE_NONE) end); "
        ) + lua_print_json({"ok": "tostring(ok)", "err": "tostring(err)"})
        res = await lua("MainMenu", load, timeout=30.0)
        say(f"LOAD issued -> {json.dumps(res)}")
        results["load_result"] = res

        # Assert on the FAR side: did GameCore_Tuner / InGame actually appear?
        for attempt in range(40):
            await asyncio.sleep(3.0)
            try:
                fresh = await client.refresh_state_indices()
            except Exception as exc:  # noqa: BLE001
                say(f"  refresh failed: {type(exc).__name__}: {exc}")
                break
            if fresh.has_game_states:
                say(f"IN GAME: GameCore_Tuner={fresh.game_core_tuner} InGame={fresh.in_game}")
                results["in_game_states"] = {
                    "GameCore_Tuner": fresh.game_core_tuner, "InGame": fresh.in_game
                }
                break
            if attempt % 4 == 0:
                say(f"  waiting for a loaded game... ({len(fresh.by_name)} states)")
        else:
            say("never reached a loaded game")

    # 4. If in game: the R5 question -- does Network.SaveGame write a file?
    if client.state_indices and client.state_indices.has_game_states:
        turn = await lua("InGame", LUA_JSON_PRELUDE + lua_print_json(
            {"turn": "Game.GetCurrentGameTurn()", "player": "Game.GetLocalPlayer()"}))
        say(f"GAME STATE -> {json.dumps(turn)}")
        results["game_state"] = turn

        name = f"civsim__winspike__t{int(time.time()) % 100000:05d}"
        save = LUA_JSON_PRELUDE + (
            "local g = {}; "
            f'g.Name = "{name}"; '
            "g.Location = SaveLocations.LOCAL_STORAGE; "
            "g.Type = SaveTypes.SINGLE_PLAYER; "
            "g.IsAutosave = false; g.IsQuicksave = false; "
            "local ok, err = pcall(function() return Network.SaveGame(g) end); "
        ) + lua_print_json({"ok": "tostring(ok)", "err": "tostring(err)"})
        res = await lua("InGame", save, timeout=30.0)
        say(f"SAVE issued -> {json.dumps(res)}")

        target = dirs.saves_dir / f"{name}.Civ6Save"
        stable = None
        for _ in range(50):
            if target.exists():
                a = target.stat().st_size
                time.sleep(0.5)
                if target.stat().st_size == a and a > 0:
                    stable = a
                    break
            time.sleep(0.4)
        say(f"SAVE FILE {target.name}: exists={target.exists()} stable_size={stable}")
        results["save"] = {"name": name, "path": str(target),
                           "exists": target.exists(), "stable_size": stable,
                           "lua": res}

    await client.close()
    results["transcript"] = transcript
    (OUT / "demo_live_results.json").write_text(json.dumps(results, indent=2, default=str))
    say("wrote demo_live_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
