"""R5/T077 Windows spike, step 1: connect through the harness's own NexusClient.

Deliberately uses `civsim_harness.nexus.NexusClient` rather than a raw socket
(the Linux spike used a standalone client on purpose, to make its results
evidence about the *game*). Here the point is the opposite and is stated
plainly: connecting to a live tuner **through the harness's own transport** is
itself the thing under test, because nothing in this repository had ever done
it on Windows.

Run:  uv run python specs/002-civ-playing-harness/spikes/r5-raw-windows/probe_connect.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from civsim_harness.nexus.client import NexusClient
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json


async def main() -> int:
    client = NexusClient(app_name="civsim_harness")
    print("connecting to 127.0.0.1:4318 via civsim_harness.nexus.NexusClient ...")
    indices = await client.connect()
    print(f"CONNECTED. state table has {len(indices.by_name)} entries.")
    print(f"  has_game_states = {indices.has_game_states}")
    print(f"  GameCore_Tuner  = {indices.game_core_tuner}")
    print(f"  InGame          = {indices.in_game}")
    print("\n--- full state table (name -> index) ---")
    for name, index in sorted(indices.by_name.items(), key=lambda kv: kv[1]):
        print(f"  {index:>4}  {name}")

    # Prove the *far side* executed our Lua, not merely that our send
    # returned. The Linux peer's rule: assert on what the far side received.
    target = indices.by_name.get("InGame") or indices.by_name.get("Main State")
    if target is not None:
        body = LUA_JSON_PRELUDE + lua_print_json(
            {
                "echo": '"nexus-windows-live"',
                "lua_version": "_VERSION",
            }
        )
        print(f"\n--- executing a round-trip probe in state index {target} ---")
        try:
            result = await client.execute_command(state_index=target, lua_body=body, timeout_s=15)
            print("far side returned:", json.dumps(result))
        except Exception as exc:  # noqa: BLE001 - spike: report, never mask
            print(f"round-trip FAILED: {type(exc).__name__}: {exc}")

    await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
