"""Run a .lua spike file in one or more named Lua states."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, "/home/matt/CivSolver/specs/002-civ-playing-harness/spikes/r5-raw")
from nexus_probe import Probe  # noqa: E402


def main() -> None:
    lua = pathlib.Path(sys.argv[1]).read_text()
    targets = sys.argv[2:] or ["InGame"]
    p = Probe(app="civsim-run-lua")
    try:
        states = p.handshake()
        print(f"=== {len(states)} states at this phase ===")
        for target in targets:
            if target not in states:
                print(f"!! state {target!r} absent at this phase\n")
                continue
            print(f"\n######## [{target}] idx={states[target]} ########")
            out, raw = p.exec_lua(states[target], lua, wait=12.0)
            print(out or f"(no sentinel output)\nRAW:\n{raw[:3000]}")
    finally:
        p.close()


if __name__ == "__main__":
    main()
