"""R6/T099 on Windows: window-scoped capture hygiene against a live, in-game client.

Requirements (T099, FR-025, FR-030, Scenario 3):
  - a console placed over the client is absent from the captured frame
  - unrelated windows over the client are absent
  - the WGC capture border does not appear (`IsBorderRequired = false`)

Method mirrors the Linux spike, with one deliberate omission: it does **not**
take the root-scoped comparison capture that spike used to demonstrate the
leak. That demonstration already exists
(`r6-evidence/root-scoped-same-region-LEAKS.png`), and repeating it here would
mean this agent taking a full-screen grab of the owner's desktop -- exactly
what Principle I forbids. Absence of the occluders is instead made meaningful
by proving, independently, that they were stacked **above** the client at
capture time.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import win32gui
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import wgc_shot  # noqa: E402

from civsim_harness.host.windows.adapter import WindowsHostPlatform  # noqa: E402

OUT = HERE
EVID = HERE.parent / "demo-evidence"
lines: list[str] = []

OCCLUDERS = [
    ("HARNESS CONSOLE -- THIS TEXT MUST NOT REACH THE AGENT", "#FF00FF", (400, 350, 1000, 300)),
    ("UNRELATED WINDOW -- ALSO MUST NOT APPEAR", "#00FF00", (400, 760, 1000, 260)),
]


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    lines.append(line)


def spawn_occluders() -> None:
    def run() -> None:
        root = tk.Tk()
        root.withdraw()
        for text, colour, (x, y, w, h) in OCCLUDERS:
            top = tk.Toplevel(root)
            top.overrideredirect(True)
            top.geometry(f"{w}x{h}+{x}+{y}")
            top.configure(bg=colour)
            top.attributes("-topmost", True)
            tk.Label(top, text=text, bg=colour, fg="#000000",
                     font=("Consolas", 18, "bold"), wraplength=w - 40).pack(expand=True)
        root.mainloop()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(1.5)


def zorder(game_hwnd: int) -> dict:
    order: list[int] = []
    h = win32gui.GetWindow(win32gui.GetDesktopWindow(), 5)
    while h:
        if win32gui.IsWindowVisible(h):
            order.append(h)
        h = win32gui.GetWindow(h, 2)
    gpos = order.index(game_hwnd) if game_hwnd in order else None
    occ = []
    for h in order:
        if win32gui.GetClassName(h) == "TkTopLevel":
            occ.append({"hwnd": h, "zpos": order.index(h),
                        "rect": win32gui.GetWindowRect(h),
                        "above_game": gpos is not None and order.index(h) < gpos})
    return {"game_zpos": gpos, "occluders": occ,
            "all_above_game": bool(occ) and all(o["above_game"] for o in occ)}


def main() -> int:
    host = WindowsHostPlatform()
    proc = host.locate_game_process()
    if proc is None:
        say("FAIL: no client process")
        return 1
    window = host.find_game_window(proc)
    if window is None:
        say("FAIL: no client window")
        return 1
    win32gui.SetForegroundWindow(window.handle)
    time.sleep(1.0)
    say(f"client window hwnd={window.handle} rect={window.rect}")

    # Clean baseline first, before anything is put over the window.
    base = wgc_shot.shoot(window.handle, EVID / "r6-baseline-clean.png", timeout_s=8)
    say(f"baseline capture: {base}")

    spawn_occluders()
    time.sleep(1.0)
    z = zorder(window.handle)
    say(f"z-order: game zpos={z['game_zpos']} occluders={z['occluders']}")
    if not z["all_above_game"]:
        say("FAIL: occluders are NOT stacked above the client -- the test would be vacuous")
        return 1

    shot = EVID / "r6-occluded-window-scoped.png"
    facts = wgc_shot.shoot(window.handle, shot, timeout_s=8)
    say(f"occluded capture: {facts}")

    img = Image.open(shot).convert("RGB")
    checks = []
    for text, colour, (x, y, w, h) in OCCLUDERS:
        rx, ry = x - window.rect.left, y - window.rect.top
        crop = img.crop((rx, ry, rx + w, ry + h))
        crop.save(EVID / f"r6-crop-{colour.lstrip('#')}.png")
        want = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))
        px = list(crop.getdata())
        hits = sum(1 for p in px if all(abs(p[i] - want[i]) < 30 for i in range(3)))
        frac = hits / max(1, len(px))
        leaked = frac > 0.01
        checks.append({"occluder": text, "colour": colour, "crop_rect": [rx, ry, w, h],
                       "matching_pixel_fraction": round(frac, 8), "leaked": leaked})
        say(f"occluder {colour}: matching fraction {frac:.8f} -> "
            f"{'LEAKED (FAIL)' if leaked else 'ABSENT (pass)'}")

    # The in-frame contaminant: an FPS overlay composited into the client's own
    # frame by a third party. Window scoping cannot exclude it, exactly as the
    # Linux spike found with Steam's counter.
    corner = img.crop((0, 0, 260, 60)).resize((780, 180), Image.NEAREST)
    corner.save(EVID / "r6-inframe-fps-overlay.png")
    say("saved top-left corner crop (in-frame FPS overlay evidence)")

    result = {
        "window": {"hwnd": window.handle, "rect": window.rect.__dict__},
        "baseline": base, "occluded": facts, "zorder": z, "checks": checks,
        "border_required_requested_false": True,
        "any_leak": any(c["leaked"] for c in checks),
        "transcript": lines,
    }
    (OUT / "r6_hygiene_results.json").write_text(json.dumps(result, indent=2, default=str))
    say(f"VERDICT: {'FAIL - leak detected' if result['any_leak'] else 'PASS - no occluder leaked'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
