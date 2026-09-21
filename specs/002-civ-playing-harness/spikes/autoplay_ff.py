"""OPERATOR SCRIPTING for the autoplay fast-forward spike (2026-09-21), not production.

Runs one Lua expression that returns a table in a named Lua state on the live client and prints
the result as JSON, through the harness's own NexusClient (single tuner connection, closed after).

    uv run python specs/002-civ-playing-harness/spikes/autoplay_ff.py --list
    uv run python specs/002-civ-playing-harness/spikes/autoplay_ff.py InGame '(function() return { t = Game.GetCurrentGameTurn() } end)()'

Everything printed is read back from the client. Nothing here is counted as a demonstrated
harness capability; the spike write-up (autoplay-fast-forward-linux.md) records what was run.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402


async def main() -> int:
    state = sys.argv[1]
    client = NexusClient(connect_timeout_s=8.0)
    indices = await client.connect()
    try:
        if state == "--list":
            print(json.dumps(sorted(indices.by_name.keys())))
            return 0
        lua = sys.argv[2]
        out = await client.execute_command(
            state_index=indices.by_name[state],
            lua_body=LUA_JSON_PRELUDE + lua_print_json({"r": lua}),
        )
        print(json.dumps(out["r"]))
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
