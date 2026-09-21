"""Which content-gate technique fires on a real frame, with the numbers the gate computes.

    uv run python specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/content_gate_probe.py <png>...

Read-only. Reuses the production heuristics' own constants and PIL calls so the printed numbers
are exactly what `parity/screening.py` sees; the final line per frame is the production detector's
verdict for the Linux screening profile with no text tokens (the live configuration).
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageStat

from civsim_harness.host.port import CaptureFrame, WindowRect
from civsim_harness.parity import screening as s

PROFILE = s.load_screening_profiles()
LINUX = s.resolve_screening_profile(PROFILE, declared_profile_key="default", platform="linux")

for arg in sys.argv[1:]:
    path = Path(arg)
    image = Image.open(path).convert("RGB")
    w, h = image.size
    print(f"== {path.parent.name}/{path.name}  {w}x{h}")
    # border ring, per edge
    t = min(s._BORDER_THICKNESS_PX, w // 4, h // 4)
    inset = t * 3
    interior = ImageStat.Stat(image.crop((inset, inset, w - inset, h - inset))).mean
    edges = {
        "top": image.crop((0, 0, w, t)),
        "bottom": image.crop((0, h - t, w, h)),
        "left": image.crop((0, 0, t, h)),
        "right": image.crop((w - t, 0, w, h)),
    }
    for name, edge in edges.items():
        st = ImageStat.Stat(edge)
        sd = sum(st.stddev) / len(st.stddev)
        delta = sum(abs(a - b) for a, b in zip(st.mean, interior, strict=True)) / len(st.mean)
        flag = sd <= s._BORDER_UNIFORMITY_STDDEV_MAX and delta >= s._BORDER_INTERIOR_DELTA_MIN
        print(f"  border {name:6s} stddev={sd:6.1f} (max {s._BORDER_UNIFORMITY_STDDEV_MAX}) "
              f"delta={delta:6.1f} (min {s._BORDER_INTERIOR_DELTA_MIN}) -> {'SUSPECT' if flag else 'ok'}")
    # corners
    cw, ch = max(1, int(w * s._CORNER_FRACTION)), max(1, int(h * s._CORNER_FRACTION))
    whole = ImageStat.Stat(image)
    wv = sum(whole.var) / len(whole.var)
    corners = {
        "top-left": image.crop((0, 0, cw, ch)),
        "top-right": image.crop((w - cw, 0, w, ch)),
        "bottom-left": image.crop((0, h - ch, cw, h)),
        "bottom-right": image.crop((w - cw, h - ch, w, h)),
    }
    print(f"  whole-frame variance={wv:8.1f}; corner patch {cw}x{ch}; "
          f"thresholds abs>={s._CORNER_VARIANCE_ABS_MIN} and ratio>={s._CORNER_VARIANCE_RATIO_MIN}")
    for name, c in corners.items():
        st = ImageStat.Stat(c)
        cv = sum(st.var) / len(st.var)
        flag = cv >= s._CORNER_VARIANCE_ABS_MIN and cv >= wv * s._CORNER_VARIANCE_RATIO_MIN
        print(f"  corner {name:12s} variance={cv:8.1f} ratio={cv / wv if wv else float('inf'):5.2f} "
              f"-> {'SUSPECT' if flag else 'ok'}")
    frame = CaptureFrame(width=w, height=h, rect=WindowRect(left=0, top=0, width=w, height=h),
                         image_bytes=path.read_bytes(), image_format="PNG")
    matches = s.DEFAULT_CONTENT_DETECTOR.detect(frame, reject_categories=LINUX.reject,
                                                detected_text_tokens=frozenset())
    print(f"  production verdict (linux profile, no text tokens): "
          f"{'WITHHELD ' + str(sorted(matches)) if matches else 'CLEAN'}")
