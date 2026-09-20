"""Cold-launch timing, with and without the intro video.

Two signals, because they are NOT the same moment and the difference matters
for the FR-045 recovery budget:
  t_port : TCP 4318 accepts a connection      (process is up)
  t_menu : LSQ returns the main-menu state set (client is actually drivable)
"""
from __future__ import annotations

import re
import socket
import subprocess
import time
from pathlib import Path

APPOPTS = Path.home() / ".local/share/aspyr-media/Sid Meier's Civilization VI/AppOptions.txt"


def set_intro(value: int) -> None:
    txt = APPOPTS.read_text()
    txt = re.sub(r"^PlayIntroVideo \d+$", f"PlayIntroVideo {value}", txt, flags=re.M)
    APPOPTS.write_text(txt)


def kill_game() -> None:
    subprocess.run("pkill -9 -x Civ6", shell=True)
    for _ in range(30):
        if subprocess.run("pgrep -x Civ6", shell=True, capture_output=True).returncode != 0:
            break
        time.sleep(1)
    time.sleep(3)


def port_open() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 4318), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def menu_ready() -> bool:
    """Main menu = tuner answers LSQ with the small pre-game state set."""
    try:
        from nexus_probe import Probe
        p = Probe(app="civsim-timing")
        st = p.handshake()
        p.close()
        return len(st) > 0
    except Exception:
        return False


def measure(label: str, intro: int) -> dict:
    kill_game()
    set_intro(intro)
    print(f"\n=== {label} (PlayIntroVideo {intro}) ===")
    t0 = time.time()
    subprocess.run("setsid steam steam://rungameid/289070 >/dev/null 2>&1 &", shell=True)
    t_port = t_menu = None
    while time.time() - t0 < 300:
        if t_port is None and port_open():
            t_port = round(time.time() - t0, 1)
            print(f"  t_port = {t_port}s")
        elif t_port is not None and menu_ready():
            t_menu = round(time.time() - t0, 1)
            print(f"  t_menu = {t_menu}s")
            break
        time.sleep(1)
    return {"label": label, "intro": intro, "t_port": t_port, "t_menu": t_menu}


results = [measure("intro DISABLED", 0), measure("intro ENABLED", 1)]
set_intro(0)  # leave the host in the fast configuration
print("\n=== SUMMARY ===")
for r in results:
    print(f"  {r['label']:<18} PlayIntroVideo={r['intro']}  t_port={r['t_port']}s  t_menu={r['t_menu']}s")
print("\n(AppOptions left with PlayIntroVideo 0)")
Path("launch_timing_results.txt").write_text(
    "\n".join(f"{r['label']}: PlayIntroVideo={r['intro']} t_port={r['t_port']}s t_menu={r['t_menu']}s" for r in results)
)
