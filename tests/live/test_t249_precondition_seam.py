"""T249 live re-verification: does the new port seam behave correctly on a real host?

`HostPlatform.check_capture_preconditions()` now delegates to this node's
live-verified `capture_preconditions()`. The seam note asks the Linux peer to
confirm the delegation behaves through the port, which is the half a unit test
against a fake cannot reach: the fake agrees with whatever the adapter believes,
so only a real compositor and a real client can say whether the verdict matches
what capture actually does.

The check that matters is **agreement**: the seam must pass exactly when
`capture_window()` succeeds, and say so for the right reason.

    python3 -m tests.live.test_t249_precondition_seam
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from civsim_harness.host.detect import LinuxSessionType  # noqa: E402
from civsim_harness.host.linux.adapter import LinuxHostPlatform  # noqa: E402
from civsim_harness.host.port import CaptureStatus, GameProcess  # noqa: E402


def main() -> int:
    adapter = LinuxHostPlatform(session_type=LinuxSessionType.x11)

    print("[1] the underlying probe this node verified live")
    raw = adapter.capture_preconditions()
    print(f"    capture_preconditions()        -> {raw}")

    print("\n[2] the new port seam that delegates to it")
    seam = adapter.check_capture_preconditions()
    print(f"    check_capture_preconditions()  -> {seam}")

    print("\n[3] does the verdict agree with what capture actually does?")
    out = subprocess.run(["pgrep", "-x", "Civ6"], capture_output=True, text=True).stdout.split()
    if not out:
        print("    !! Civ VI not running -- cannot compare the verdict against a real capture")
        return 1
    window = adapter.find_game_window(GameProcess(pid=int(out[0]), name="Civ6"))
    if window is None:
        print("    !! no window resolved")
        return 1

    result = adapter.capture_window(window)
    captured_ok = result.status is CaptureStatus.ok
    print(f"    capture_window()               -> {result.status.name} "
          f"({result.reason or 'no reason given'})")

    passed = getattr(seam, "passed", None)
    if passed is None:  # tolerate a differently-named field rather than guessing
        passed = getattr(seam, "ok", None)
    print(f"\n    seam says preconditions pass   = {passed}")
    print(f"    capture actually succeeded     = {captured_ok}")

    if passed is None:
        print("    ?? could not read a pass/fail field off the result; printed above for inspection")
        return 1
    if bool(passed) != captured_ok:
        print("    ❌ DISAGREE -- the seam would gate capture incorrectly on this host")
        return 1
    print("    ✅ AGREE -- the seam's verdict matches real capture behaviour on this host")

    # -- negative control -------------------------------------------------------------------
    # One agreeing case is weak: a check hard-wired to pass would agree here too. So ask it
    # about a configuration where capture is known NOT to work. A precondition check that
    # cannot return False is decoration.
    print("\n[4] negative control -- can this seam ever return a FAILING verdict?")
    wayland = LinuxHostPlatform(session_type=LinuxSessionType.wayland)
    neg = wayland.check_capture_preconditions()
    print(f"    wayland (no portal path implemented) -> passed={neg.passed}")
    print(f"      reason: {neg.reason}")
    if neg.passed:
        print("    ❌ the seam passes even where capture cannot work -- it is decoration")
        return 1
    print("    ✅ the seam can fail, and fails for the right reason")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
