"""LIVE STAGE 6 close-out: take a NAMED save through the production saves.save_game capability,
then confirm the board state the block ends on.

A save issued into a popup state can report `issued: true` and never land, so this only prints
what it did -- the caller stats the file on disk and checks it is size-stable.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path("/home/matt/CivSolver-live")
sys.path.insert(0, str(REPO / "src"))

from civsim_harness.capability.executor import CapabilityExecutor  # noqa: E402
from civsim_harness.capability.loader import load_catalog  # noqa: E402
from civsim_harness.capability.registry import CapabilityRegistry  # noqa: E402
from civsim_harness.models.catalog import LuaContext  # noqa: E402
from civsim_harness.models.common import DeclarationId  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402

SAVE_NAME = sys.argv[1] if len(sys.argv) > 1 else "civsim__stage6__close"


async def main() -> int:
    catalog = load_catalog(REPO / "catalogs")
    registry = CapabilityRegistry(catalog=catalog)
    client = NexusClient(connect_timeout_s=10.0)
    await client.connect()
    executor = CapabilityExecutor(
        registry=registry,
        execute_command=lambda i, b: client.execute_command(state_index=i, lua_body=b),
        session=client,
        lua_root=REPO,
    )

    async def obs(did: str):
        return (await executor.execute(DeclarationId(did), context=LuaContext("InGame"))).value

    print("SCREEN BEFORE:", json.dumps(await obs("game.screen_state"), sort_keys=True))
    print("TURN BEFORE:", json.dumps(await obs("game.turn_state"), sort_keys=True))
    result = await executor.execute(
        DeclarationId("saves.save_game"), context=LuaContext("InGame"), arguments=(SAVE_NAME,)
    )
    print("SAVE DISPATCH:", json.dumps(result.value, sort_keys=True, default=str))
    # Later command, never a same-command readback.
    await asyncio.sleep(3.0)
    print("SCREEN AFTER:", json.dumps(await obs("game.screen_state"), sort_keys=True))
    print("TURN AFTER:", json.dumps(await obs("game.turn_state"), sort_keys=True))
    print("CITIES AFTER:", json.dumps(await obs("cities.state"), default=str)[:400])
    await client.close()
    print("tuner connection closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
