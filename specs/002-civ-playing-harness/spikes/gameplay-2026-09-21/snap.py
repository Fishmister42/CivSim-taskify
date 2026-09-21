"""Capture the game window through the production capture_window() path and save a <=960px JPEG.

    uv run python .../snap.py <out.jpg>
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))
from PIL import Image  # noqa: E402

from civsim_harness.host.factory import get_host_platform  # noqa: E402
from civsim_harness.host.port import CaptureStatus, GameProcess  # noqa: E402

out = Path(sys.argv[1])
host = get_host_platform()
pids = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
window = host.find_game_window(GameProcess(pid=int(pids[0]), name="Civ6"))
res = host.capture_window(window)
if res.status is not CaptureStatus.ok or res.frame is None:
    raise SystemExit(f"capture failed: {res.status} {getattr(res, 'reason', '')}")
f = res.frame
img = Image.frombytes("RGBA", (f.width, f.height), f.image_bytes)
b, g, r, _ = img.split()
rgb = Image.merge("RGB", (r, g, b))
w = 960
rgb = rgb.resize((w, int(rgb.height * w / rgb.width)), Image.LANCZOS)
out.parent.mkdir(parents=True, exist_ok=True)
rgb.save(out, "JPEG", quality=80, optimize=True)
print(f"saved {out} {out.stat().st_size // 1024} KB from {f.width}x{f.height}")
