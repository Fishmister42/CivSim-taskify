"""LIVE STAGE 6 (2026-09-22) block-09: sub-second dispatch->effect latency probe.

OPERATOR SCRIPTING, not production, and deliberately NOT counted as demonstrated capability.
It exists for one reason: the store samples an action's effect once per decision step, so every
latency number the store can yield is a CEILING, not the curve. Only a controlled dispatch with a
tight poll cadence can produce the distribution that `ACTION_CONFIRM_TIMEOUT_S` is supposed to
bound (run/decision_loop.py:237, sole call site :959).

Three rules this probe obeys, each from a measured fact:

  1. A same-command Lua readback returns the PRE-CALL value, so **every poll is its own tuner
     command** -- the dispatch command's own return value is never read as the effect.
  2. Nothing here dispatches `units.found_city`. Founding the capital is an agent decision that
     goes in the store with a run id; an operator dispatching it would spend the one demonstration
     this board offers and count as nothing. Only non-consuming, repeatable actions are probed.
  3. The probe reports RAW SAMPLES and the poll interval. It proposes no bound.

    uv run python <this file> OUT.json
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path("/home/matt/CivSolver-live")
sys.path.insert(0, str(REPO / "src"))

from civsim_harness.capability.executor import CapabilityExecutor  # noqa: E402
from civsim_harness.capability.loader import load_catalog  # noqa: E402
from civsim_harness.capability.registry import CapabilityRegistry  # noqa: E402
from civsim_harness.models.catalog import LuaContext  # noqa: E402
from civsim_harness.models.common import DeclarationId  # noqa: E402
from civsim_harness.nexus.client import NexusClient  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("latency-probe.json")

#: Sleep between consecutive polls. The EFFECTIVE sampling granularity is this plus one tuner
#: round trip, which is why the round-trip floor is measured first and reported alongside.
POLL_SLEEP_S = 0.10
#: Hard ceiling on any single poll loop, so the probe is bounded no matter what the client does.
POLL_DEADLINE_S = 20.0


class Probe:
    def __init__(self, client: NexusClient, executor: CapabilityExecutor) -> None:
        self.client = client
        self.executor = executor
        self.samples: list[dict[str, Any]] = []

    async def observe(self, declaration_id: str) -> Any:
        """One observation, as its own tuner command."""
        result = await self.executor.execute(
            DeclarationId(declaration_id), context=LuaContext("InGame")
        )
        return result.value

    async def dispatch_and_poll(
        self,
        *,
        action_class: str,
        declaration_id: str,
        arguments: tuple[Any, ...],
        observation_id: str,
        effect,
        note: str = "",
    ) -> dict[str, Any]:
        """Dispatch one action, then poll in SEPARATE commands until *effect* first holds."""
        sample: dict[str, Any] = {
            "action_class": action_class,
            "declaration_id": declaration_id,
            "arguments": list(arguments),
            "note": note,
            "poll_sleep_s": POLL_SLEEP_S,
        }
        t_send = time.perf_counter()
        try:
            dispatch = await self.executor.execute(
                DeclarationId(declaration_id), context=LuaContext("InGame"), arguments=arguments
            )
            dispatch_value = dispatch.value
        except Exception as exc:  # noqa: BLE001 -- every failure shape is a finding
            sample["dispatch_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.samples.append(sample)
            return sample
        t_ret = time.perf_counter()
        sample["dispatch_result"] = dispatch_value
        sample["dispatch_command_s"] = round(t_ret - t_send, 4)

        polls: list[dict[str, Any]] = []
        observed_at: float | None = None
        while (time.perf_counter() - t_ret) < POLL_DEADLINE_S:
            await asyncio.sleep(POLL_SLEEP_S)
            t_poll_send = time.perf_counter()
            try:
                value = await self.observe(observation_id)
                hit = bool(effect(value))
                err = None
            except Exception as exc:  # noqa: BLE001
                hit, err, value = False, f"{type(exc).__name__}: {str(exc)[:160]}", None
            t_poll_ret = time.perf_counter()
            polls.append(
                {
                    "i": len(polls),
                    "sent_at_s": round(t_poll_send - t_ret, 4),
                    "returned_at_s": round(t_poll_ret - t_ret, 4),
                    "command_s": round(t_poll_ret - t_poll_send, 4),
                    "effect_observed": hit,
                    "error": err,
                }
            )
            if hit:
                observed_at = t_poll_ret - t_ret
                break
        sample["polls"] = polls
        sample["poll_count"] = len(polls)
        if observed_at is None:
            sample["effect_first_observed_s"] = None
            sample["outcome"] = "NOT_OBSERVED_WITHIN_DEADLINE"
            sample["deadline_s"] = POLL_DEADLINE_S
        else:
            sample["effect_first_observed_s"] = round(observed_at, 4)
            sample["effect_first_observed_from_send_s"] = round(observed_at + (t_ret - t_send), 4)
            sample["outcome"] = "OBSERVED"
        self.samples.append(sample)
        flag = sample["outcome"]
        print(
            f"  {action_class:<16} {declaration_id:<18} dispatch={sample['dispatch_command_s']}s "
            f"first_observed={sample.get('effect_first_observed_s')}s "
            f"polls={len(polls)} {flag}"
        )
        return sample


def _units(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        for key in ("units", "items", "state"):
            if isinstance(value.get(key), list):
                return [u for u in value[key] if isinstance(u, dict)]
    if isinstance(value, list):
        return [u for u in value if isinstance(u, dict)]
    return []


def _mine(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [u for u in units if u.get("owner_is_local_player") in (True, None)]


def _by_id(units: list[dict[str, Any]], unit_id: Any) -> dict[str, Any] | None:
    for u in units:
        if u.get("unit_id") == unit_id:
            return u
    return None


def _plot_of(unit: dict[str, Any] | None) -> Any:
    if not unit:
        return None
    return unit.get("plot")


async def main() -> int:
    catalog = load_catalog(REPO / "catalogs")
    registry = CapabilityRegistry(catalog=catalog)
    client = NexusClient(connect_timeout_s=10.0)
    indices = await client.connect()
    print(f"connected; InGame={indices.in_game} GameCore_Tuner={indices.game_core_tuner}")
    executor = CapabilityExecutor(
        registry=registry,
        execute_command=lambda i, b: client.execute_command(state_index=i, lua_body=b),
        session=client,
        lua_root=REPO,
    )
    probe = Probe(client, executor)
    report: dict[str, Any] = {
        "probed_at": datetime.now(UTC).isoformat(),
        "poll_sleep_s": POLL_SLEEP_S,
        "poll_deadline_s": POLL_DEADLINE_S,
        "provenance": "worktree /home/matt/CivSolver-live at 287dee7 + uncommitted "
        "lua/ingame/screens.lua and catalogs/README.md (EndGameMenu watchlist mapping, "
        "content identical to main f14bad0); NOT a clean checkout",
        "note": "operator scripting; counted as nothing; units.found_city deliberately never "
        "dispatched here",
    }

    # ---- 0. Baseline board + the round-trip FLOOR of a bare observation command --------------
    turn_state = await probe.observe("game.turn_state")
    screen = await probe.observe("game.screen_state")
    units_value = await probe.observe("units.state")
    cities_value = await probe.observe("cities.state")
    report["baseline"] = {
        "game.turn_state": turn_state,
        "game.screen_state": screen,
        "units.state": units_value,
        "cities.state": cities_value,
    }
    print("baseline turn_state:", json.dumps(turn_state, default=str)[:300])
    print("baseline screen:", json.dumps(screen, default=str)[:300])
    print("baseline units:", json.dumps(units_value, default=str)[:900])
    print("baseline cities:", json.dumps(cities_value, default=str)[:300])

    floor: list[float] = []
    for _ in range(8):
        t0 = time.perf_counter()
        await probe.observe("units.state")
        floor.append(round(time.perf_counter() - t0, 4))
    report["observation_round_trip_floor_s"] = floor
    print("round-trip floor (units.state, 8 samples):", floor)

    units = _mine(_units(units_value))
    settler = next((u for u in units if "SETTLER" in str(u.get("unit_type", "")).upper()), None)
    other = next((u for u in units if u is not settler), None)
    report["settler"] = settler
    report["other_unit"] = other
    print("settler:", json.dumps(settler, default=str)[:400])
    print("other unit:", json.dumps(other, default=str)[:400])

    # ---- 1. CLASS: camera.move ---------------------------------------------------------------
    # Non-consuming and freely repeatable. verification_predicate: camera.target_plot == target.
    anchor = _plot_of(settler) or _plot_of(other)
    print("\nCLASS camera.move")
    if isinstance(anchor, dict) and "x" in anchor and "y" in anchor:
        ax, ay = int(anchor["x"]), int(anchor["y"])
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1), (2, 1), (1, 2)):
            target = {"x": ax + dx, "y": ay + dy}

            def effect(value: Any, _t=target) -> bool:
                cam = value if isinstance(value, dict) else {}
                tp = cam.get("target_plot") or cam.get("plot")
                if isinstance(tp, dict):
                    return int(tp.get("x", -1)) == _t["x"] and int(tp.get("y", -1)) == _t["y"]
                return False

            await probe.dispatch_and_poll(
                action_class="camera.move",
                declaration_id="camera.move",
                arguments=(target,),
                observation_id="camera.read_state",
                effect=effect,
                note="camera pan to a revealed plot near the starting units",
            )
    else:
        print("  skipped: no anchor plot available")

    # ---- 2. CLASS: units.select --------------------------------------------------------------
    # Alternating selection between the two starting units. Non-consuming; changes nothing on the
    # board but which unit the next order would act on.
    # ALTERNATING city <-> unit, so every dispatch is a genuine selection TRANSITION rather than
    # a re-select of something already selected (which would observe true on the first poll and
    # measure nothing).
    print("\nCLASS units.select / cities.select (alternating, real transitions)")
    city_ids = [
        c.get("city_id")
        for c in (cities_value.get("cities") or [])
        if isinstance(c, dict) and c.get("owner_is_local_player") is not False
    ]
    city_id = city_ids[0] if city_ids else None
    report["city_id"] = city_id
    unit_for_select = other or settler
    for _round in range(3):
        if city_id is not None:

            def city_effect(value: Any, _cid=city_id) -> bool:
                v = value if isinstance(value, dict) else {}
                if not v.get("has_selection"):
                    return False
                sel = v.get("selected_city_id", v.get("city_id"))
                return sel is None or sel == _cid

            await probe.dispatch_and_poll(
                action_class="cities.select",
                declaration_id="cities.select",
                arguments=(city_id,),
                observation_id="cities.selection",
                effect=city_effect,
                note=f"select city {city_id} (transition away from unit selection)",
            )
        if unit_for_select:
            uid = unit_for_select.get("unit_id")

            def effect(value: Any, _uid=uid) -> bool:
                u = _by_id(_units(value), _uid)
                return bool(u and u.get("is_selected"))

            await probe.dispatch_and_poll(
                action_class="units.select",
                declaration_id="units.select",
                arguments=(uid,),
                observation_id="units.state",
                effect=effect,
                note=f"select unit {uid} ({unit_for_select.get('unit_type')})",
            )

    # ---- 3. CLASS: units.move_to (the class the 9.63 s lower bound came from) -----------------
    # The NON-SETTLER unit only. The settler is never moved here: moving it is part of the
    # founding decision the harness gets to make.
    print("\nCLASS units.move_to (non-settler unit only)")
    for attempt in range(3):
        latest = await probe.observe("units.state")
        unit = _by_id(_mine(_units(latest)), other.get("unit_id")) if other else None
        if not unit:
            print("  no unit to move")
            break
        reach = unit.get("reachable_plots") or []
        here = _plot_of(unit)
        choices = [
            p
            for p in reach
            if isinstance(p, dict)
            and not (
                isinstance(here, dict)
                and p.get("x") == here.get("x")
                and p.get("y") == here.get("y")
            )
        ]
        if not choices:
            print(f"  attempt {attempt}: no reachable plot other than its own (movement spent?)")
            report.setdefault("move_skips", []).append(
                {"attempt": attempt, "unit": unit, "reason": "no_reachable_plot"}
            )
            break
        # Prefer an adjacent plot: a one-tile order arrives this turn, so the effect is observable.
        def _dist(p: dict[str, Any]) -> int:
            if not isinstance(here, dict):
                return 0
            return abs(int(p.get("x", 0)) - int(here.get("x", 0))) + abs(
                int(p.get("y", 0)) - int(here.get("y", 0))
            )

        target = sorted(choices, key=_dist)[0]

        # The order acts on the SELECTED unit, so select it first (not timed as a move sample).
        await probe.dispatch_and_poll(
            action_class="units.select",
            declaration_id="units.select",
            arguments=(unit.get("unit_id"),),
            observation_id="units.state",
            effect=lambda v, _u=unit.get("unit_id"): bool(
                (lambda x: x and x.get("is_selected"))(_by_id(_units(v), _u))
            ),
            note="reselect before move (also a units.select sample)",
        )

        def effect(value: Any, _t=target, _u=unit.get("unit_id")) -> bool:
            u = _by_id(_units(value), _u)
            p = _plot_of(u)
            return bool(
                isinstance(p, dict)
                and int(p.get("x", -1)) == int(_t["x"])
                and int(p.get("y", -1)) == int(_t["y"])
            )

        await probe.dispatch_and_poll(
            action_class="units.move_to",
            declaration_id="units.move_to",
            arguments=(target,),
            observation_id="units.state",
            effect=effect,
            note=f"move non-settler from {here} to adjacent {target}",
        )

    report["samples"] = probe.samples
    report["final_units_state"] = await probe.observe("units.state")
    report["final_turn_state"] = await probe.observe("game.turn_state")
    await client.close()

    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {OUT}")
    observed = [s for s in probe.samples if s.get("outcome") == "OBSERVED"]
    print(f"{len(observed)}/{len(probe.samples)} dispatches had their effect observed")
    for cls in sorted({s["action_class"] for s in probe.samples}):
        vals = [
            s["effect_first_observed_s"]
            for s in probe.samples
            if s["action_class"] == cls and s.get("effect_first_observed_s") is not None
        ]
        print(f"  {cls:<16} n={len(vals)} raw={vals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
