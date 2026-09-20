"""The demo: the harness ends a real turn in a real Civilization VI client.

**Scripted, not model-generated.** There is no OpenRouter key on this machine and
no model call anywhere in this file. The decision "end the turn" is hard-coded.
What is demonstrated is the harness's transport, Lua execution, and capture
path against a live client -- not an agent playing Civilization.

Assertion is on the far side: the turn counter read back from the game must
actually advance. Frames are captured **window-scoped** throughout.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "r5-raw-windows"))
sys.path.insert(0, str(HERE.parent / "r6-raw-windows"))

import wgc_shot  # noqa: E402
from r5_save_spike import LiveNexusClient  # noqa: E402

from civsim_harness.host.windows.adapter import WindowsHostPlatform  # noqa: E402
from civsim_harness.nexus.sentinels import LUA_JSON_PRELUDE, lua_print_json  # noqa: E402

FRAMES = HERE / "frames"
log: list[str] = []


def say(m: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    log.append(line)


def burst(hwnd: int, stop: threading.Event) -> list[Path]:
    FRAMES.mkdir(exist_ok=True)
    for old in FRAMES.glob("*.png"):
        old.unlink()
    shots: list[Path] = []
    i = 0
    while not stop.is_set() and i < 40:
        p = FRAMES / f"f{i:03d}.png"
        try:
            wgc_shot.shoot(hwnd, p, timeout_s=3.0)
            shots.append(p)
        except Exception:  # noqa: BLE001 - a dropped frame is not a failure
            pass
        i += 1
        time.sleep(0.6)
    return shots


async def main() -> int:
    host = WindowsHostPlatform()
    proc = host.locate_game_process()
    window = host.find_game_window(proc) if proc else None
    if window is None:
        say("no client window")
        return 1

    client = LiveNexusClient(app_name="civsim_harness", connect_timeout_s=8.0)
    await client.connect()
    idx = await client.resolve_game_states()
    say(f"connected; InGame={idx.in_game}")

    async def lua(body: str, t: float = 25.0):
        return await client.execute_command(
            state_index=idx.in_game, lua_body=body, timeout_s=t
        )

    read_turn = LUA_JSON_PRELUDE + lua_print_json({
        "turn": "Game.GetCurrentGameTurn()",
        "player": "Game.GetLocalPlayer()",
    })
    before = await lua(read_turn)
    say(f"TURN BEFORE -> {json.dumps(before)}")

    stop = threading.Event()
    shots: list[Path] = []
    t = threading.Thread(target=lambda: shots.extend(burst(window.handle, stop)), daemon=True)
    t.start()
    await asyncio.sleep(2)

    end_turn = LUA_JSON_PRELUDE + (
        "local ok, err = pcall(function() "
        "return UI.RequestAction(ActionTypes.ACTION_ENDTURN) end); "
    ) + lua_print_json({"ok": "tostring(ok)", "err": "tostring(err)"})
    say("issuing UI.RequestAction(ACTION_ENDTURN)")
    res = await lua(end_turn, t=30)
    say(f"END TURN -> {json.dumps(res)}")

    advanced = False
    after = before
    for _ in range(25):
        await asyncio.sleep(2)
        try:
            after = await lua(read_turn, t=20)
        except Exception as exc:  # noqa: BLE001
            say(f"  read: {type(exc).__name__}")
            continue
        if isinstance(after, dict) and isinstance(before, dict) \
                and after.get("turn") != before.get("turn"):
            advanced = True
            say(f"TURN ADVANCED {before.get('turn')} -> {after.get('turn')}")
            break
    if not advanced:
        say(f"turn did not advance (still {after})")

    await asyncio.sleep(2)
    stop.set()
    t.join(timeout=20)
    say(f"captured {len(shots)} window-scoped frames")

    if shots:
        from PIL import Image

        imgs = [Image.open(p).convert("RGB").resize((960, 540), Image.LANCZOS) for p in shots]
        gif = HERE / "harness-ends-a-turn.gif"
        imgs[0].save(gif, save_all=True, append_images=imgs[1:],
                     duration=500, loop=0, optimize=True)
        say(f"wrote {gif.name} ({gif.stat().st_size // 1024} KB, {len(imgs)} frames)")
        for keep, n in ((0, "key-01-before"), (len(shots) // 2, "key-02-during"),
                        (len(shots) - 1, "key-03-after")):
            Image.open(shots[keep]).convert("RGB").resize((1280, 720), Image.LANCZOS).save(
                HERE / f"{n}.png")
        say("wrote 3 key frames")

    await client.close()
    (HERE / "end_turn_demo_results.json").write_text(json.dumps(
        {"turn_before": before, "end_turn": res, "turn_after": after,
         "advanced": advanced, "frames": len(shots),
         "model_calls": 0, "decisions": "scripted, not model-generated",
         "log": log}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
