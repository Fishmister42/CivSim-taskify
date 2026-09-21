"""T213 live: dispatch EVERY catalog observation declaration through the production executor
against the live client, validate each result against its declared output schema, and record
what answered -- so a run's observation set is chosen from measurement, not from hope.

    python3 -m tests.live.probe_observation_bodies [out.json]

Uses the production `load_catalog` -> `CapabilityRegistry` -> `CapabilityExecutor` chain and the
same `_validate_output` the production `ObservationReader` applies, on the run's own
`NexusClient`. A body that errors in Lua, returns the wrong shape, or cannot resolve its state is
reported as such and the probe continues -- nothing here stops at the first failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from civsim_harness.capability.executor import CapabilityExecutor  # noqa: E402
from civsim_harness.capability.loader import load_catalog  # noqa: E402
from civsim_harness.capability.registry import CapabilityRegistry  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402
from civsim_harness.observe.assemble import _validate_output  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else None


def _summarise(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, default=str, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def main() -> int:
    catalog = load_catalog(REPO / "catalogs")
    registry = CapabilityRegistry(catalog=catalog)
    client = NexusClient()
    indices = await client.connect()
    print(f"connected; states: GameCore_Tuner={indices.game_core_tuner} InGame={indices.in_game}")
    executor = CapabilityExecutor(
        registry=registry,
        execute_command=lambda i, b: client.execute_command(state_index=i, lua_body=b),
        session=client,
        lua_root=REPO,
    )
    report: dict[str, Any] = {
        "probed_at": datetime.now(UTC).isoformat(),
        "catalog_version": str(getattr(catalog, "version", "")),
        "results": [],
    }
    declarations = [
        d for d in catalog.declarations.values() if str(d.kind.value) == "observation"
    ]
    ok = 0
    for declaration in sorted(declarations, key=lambda d: str(d.declaration_id)):
        entry: dict[str, Any] = {
            "declaration_id": str(declaration.declaration_id),
            "context": str(declaration.context.value),
            "capability_id": str(declaration.capability_id),
        }
        t0 = time.perf_counter()
        try:
            result = await executor.execute(declaration.declaration_id, context=declaration.context)
            _validate_output(declaration, result.value)
        except Exception as exc:  # noqa: BLE001 -- every failure shape is a finding
            entry["status"] = "ERR"
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        else:
            entry["status"] = "OK"
            entry["value"] = result.value
            ok += 1
        entry["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        report["results"].append(entry)
        flag = "OK " if entry["status"] == "OK" else "ERR"
        detail = _summarise(entry.get("value")) if flag == "OK " else entry["error"][:200]
        print(f"{flag} {entry['declaration_id']:<28} [{entry['context']:<14}] "
              f"{entry['elapsed_ms']:>5}ms  {detail}")
    await client.close()
    report["ok"] = ok
    report["total"] = len(declarations)
    print(f"\n{ok}/{len(declarations)} observation declarations answered with a schema-valid value")
    if OUT is not None:
        OUT.write_text(json.dumps(report, indent=2, default=str))
        print(f"wrote {OUT}")
    return 0 if ok == len(declarations) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
