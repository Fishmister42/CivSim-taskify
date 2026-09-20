"""Live acceptance test for the production `LuaSaveLoader` (T217 → `saves/load_game.py`).

Its module docstring carries one honestly-open item:

    "a load may stop on the leader-intro screen ... This loader cannot click it"

The R7 spike measured that screen appearing on **every** load, not only on saves
taken before it was first dismissed -- so on this host the open item is not a
contingency, it is the normal path, and the loader should time out every time.

This drives the real loader against the real client to find out, with a SHORT
timeout so an honest failure costs seconds rather than the 300 s default. Then
it repeats the load with one `send_input(Escape)` supplied at the right moment,
to show what closes the gap.

    python3 -m tests.live.test_production_save_loader <save_name>
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from civsim_harness.host.detect import LinuxSessionType  # noqa: E402
from civsim_harness.host.linux.adapter import LinuxHostPlatform  # noqa: E402
from civsim_harness.host.port import (  # noqa: E402
    GameProcess, InputEvent, InputEventKind,
)
from civsim_harness.models.records import SavePoint  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.saves.load_game import LuaSaveLoader  # noqa: E402

SAVE_NAME = sys.argv[1] if len(sys.argv) > 1 else "civsim__spike__t0001"
SHORT_TIMEOUT_S = 150.0


def make_save_point() -> SavePoint:
    from datetime import UTC, datetime
    return SavePoint(
        save_point_id="sp-live-probe",
        run_id="run-live-probe",
        turn_number=1,
        save_name=SAVE_NAME,
        taken_at=datetime.now(UTC),
        verified=True,
        retention_status="retained",
    )


def civ_window(adapter):
    out = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
    if not out:
        return None
    return adapter.find_game_window(GameProcess(pid=int(out[0]), name="Civ6"))


async def attempt(label: str, *, dismiss: bool) -> bool:
    """One load attempt. With dismiss=True, send Escape once the port goes quiet."""
    adapter = LinuxHostPlatform(session_type=LinuxSessionType.x11)
    client = NexusClient()
    await client.connect()
    loader = LuaSaveLoader(
        client,
        host=adapter,
        load_timeout_s=SHORT_TIMEOUT_S,
        exit_to_menu_timeout_s=SHORT_TIMEOUT_S,
        verify_timeout_s=SHORT_TIMEOUT_S,
    )

    dismisser: asyncio.Task | None = None
    if dismiss:
        async def press_escape_until_the_port_returns() -> None:
            """Retry Escape until the tuner rebinds.

            A one-shot press at a fixed delay does NOT work, measured: the port
            closes the instant the load is issued, but the intro screen only
            appears once the load finishes, tens of seconds later. And the intro
            screen cannot be observed *through* the tuner, because the tuner is
            exactly what it is holding closed. So the only available signal is
            the port coming back -- which means dismissal has to be a retry loop
            keyed on that, not a timed single press.
            """
            sent = 0
            while True:
                await asyncio.sleep(5.0)
                probe = subprocess.run(["ss", "-ltn"], capture_output=True, text=True).stdout
                if "4318" in probe:
                    if sent:
                        print(f"    [dismisser] port back after {sent} Escape press(es)")
                    return
                window = civ_window(adapter)
                if window is not None:
                    subprocess.run(
                        ["xdotool", "windowactivate", str(window.handle)], check=False
                    )
                    await asyncio.sleep(0.5)
                result = adapter.send_input(
                    [InputEvent(kind=InputEventKind.key_press, key="Escape")]
                )
                sent += 1
                print(f"    [dismisser] Escape #{sent} -> {result.status.name}")
        dismisser = asyncio.create_task(press_escape_until_the_port_returns())

    print(f"\n=== {label} ===")
    t0 = time.perf_counter()
    try:
        await loader.load(make_save_point())
        dt = time.perf_counter() - t0
        print(f"  RESULT: load() SUCCEEDED in {dt:.1f}s")
        return True
    except Exception as exc:  # noqa: BLE001 - reporting whatever the loader raises
        dt = time.perf_counter() - t0
        print(f"  RESULT: load() FAILED after {dt:.1f}s")
        print(f"    {type(exc).__name__}: {exc}")
        detail = getattr(exc, "detail", None)
        if detail:
            print(f"    detail: {detail}")
        return False
    finally:
        if dismisser is not None and not dismisser.done():
            dismisser.cancel()
        try:
            await client.close()
        except Exception:
            pass


async def recover_to_a_connectable_client() -> None:
    """Attempt 1 strands the client on the intro screen with the port closed, so the
    next attempt cannot even connect. Press Escape until the tuner comes back --
    itself another measurement of the same finding."""
    adapter = LinuxHostPlatform(session_type=LinuxSessionType.x11)
    for _ in range(12):
        if "4318" in subprocess.run(["ss", "-ltn"], capture_output=True, text=True).stdout:
            print("  [recover] tuner is up")
            return
        window = civ_window(adapter)
        if window is not None:
            subprocess.run(["xdotool", "windowactivate", str(window.handle)], check=False)
            await asyncio.sleep(1.0)
            adapter.send_input([InputEvent(kind=InputEventKind.key_press, key="Escape")])
            print("  [recover] sent Escape")
        await asyncio.sleep(5.0)
    print("  [recover] gave up; tuner still closed")


async def main() -> int:
    print(f"save={SAVE_NAME!r}  short timeout={SHORT_TIMEOUT_S:g}s (default is 300s)")
    # A previous run may have left the client stranded on the intro screen with the
    # port closed -- which is itself the finding, but it stops us connecting at all.
    await recover_to_a_connectable_client()
    without = await attempt("ATTEMPT 1 - production loader, exactly as shipped", dismiss=False)
    print("\n(the client is now sitting on whatever screen attempt 1 left it on)")
    await recover_to_a_connectable_client()
    with_dismiss = await attempt(
        "ATTEMPT 2 - same loader + one send_input(Escape)", dismiss=True
    )

    print("\n" + "=" * 68)
    print(f"  as shipped              : {'PASS' if without else 'FAIL'}")
    print(f"  with one Escape         : {'PASS' if with_dismiss else 'FAIL'}")
    if not without and with_dismiss:
        print("  => the gap is exactly the missing keystroke")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
