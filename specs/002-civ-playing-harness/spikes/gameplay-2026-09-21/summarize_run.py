"""Summarise one run from the match store, read-only, for the gameplay ledger.

    uv run python specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/summarize_run.py <run_id_prefix> [--md]

Everything printed is read from the store's own records (turn cycles, decision steps, model calls,
run events). Nothing is inferred from a recording.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
DB = REPO / "civsim-match-store.db"
prefix = sys.argv[1]
md = "--md" in sys.argv

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
run_id = c.execute("select run_id from runs where run_id like ? order by run_id", (prefix + "%",)).fetchone()
if not run_id:
    raise SystemExit(f"no run matching {prefix}")
run_id = run_id[0]
run = json.loads(c.execute("select run_json from runs where run_id=?", (run_id,)).fetchone()[0])

def val(entries, did):
    for e in entries:
        if e.get("declaration_id") == did:
            return e.get("value")
    return None

turns = c.execute(
    "select turn_cycle_id, turn_number, attempt_index, is_authoritative, turn_json from turn_cycles where run_id=? order by turn_number, attempt_index",
    (run_id,),
).fetchall()
events = [json.loads(r[0]) for r in c.execute("select event_json from run_events where run_id=? order by rowid", (run_id,))] if c.execute("pragma table_info(run_events)").fetchall() else []
ev_types = Counter(e.get("event_type") or e.get("kind") or e.get("type") for e in events)

calls = [json.loads(r[0]) for r in c.execute("select call_json from model_calls where run_id=?", (run_id,))]
cost = sum((k.get("cost") or {}).get("amount_usd") or 0 for k in calls)

actions: Counter = Counter()
outcomes: Counter = Counter()
screens: Counter = Counter()
lines: list[str] = []
game_turns: list[int] = []
for tcid, tn, att, auth, tj in turns:
    t = json.loads(tj)
    steps = c.execute("select step_index, bundle_json from decision_steps where turn_cycle_id=? order by step_index", (tcid,)).fetchall()
    gt = None
    yields = t.get("yields") or {}
    for si, bj in steps:
        b = json.loads(bj)
        ents = (b.get("observation") or {}).get("entries") or []
        ts = val(ents, "game.turn_state") or {}
        if gt is None and isinstance(ts, dict):
            gt = ts.get("turn_number") or ts.get("game_turn") or ts.get("turn")
        ss = val(ents, "game.screen_state") or {}
        scr = ss.get("screen") if isinstance(ss, dict) else None
        screens[scr] += 1
        d = b.get("decision") or {}
        ex = d.get("execution") or {}
        status = ex.get("status") or ex.get("outcome") or ex.get("result") or "?"
        reason = ex.get("reason") or ex.get("detail") or ex.get("refusal_reason") or ""
        aid = d.get("action_declaration_id")
        actions[aid] += 1
        outcomes[f"{aid}:{status}"] += 1
        mc = b.get("model_call") or {}
        lines.append(
            f"  t{tn}{'' if auth else '(non-auth)'} s{si} [{scr}] {aid} {json.dumps(d.get('parameters'))} -> {status}"
            + (f" ({str(reason)[:140]})" if reason and status not in ('applied',) else "")
            + (f"  [{(mc.get('outcome') or '')} {(mc.get('latency_ms') or 0)/1000:.0f}s]" if mc else "")
        )
    if gt is not None:
        game_turns.append(int(gt))
    lines.insert(len(lines) - len(steps), f" turn {tn} (attempt {att}, {'authoritative' if auth else 'abandoned'}): game turn {gt}, outcome {t.get('outcome')}, {len(steps)} steps, yields {json.dumps(yields)[:160]}")

print(f"run {run_id}")
print(f"  state {run.get('lifecycle_state')} stop {run.get('stop_resolution')} completeness {run.get('record_completeness_status')} tier {run.get('host_support_tier')}")
print(f"  game turns {min(game_turns) if game_turns else '?'} -> {max(game_turns) if game_turns else '?'} ; turns recorded {len(turns)} ; model calls {len(calls)} ; cost ${cost:.4f}")
print(f"  actions: {dict(actions)}")
print(f"  outcomes: {dict(outcomes)}")
print(f"  screens seen at steps: {dict(screens)}")
print(f"  run events: {dict(ev_types)}")
stalls = [e for e in events if any(k in json.dumps(e) for k in ("unknown_screen", "UnknownScreen", "paused", "refused", "failed", "no_progress"))]
for e in stalls[:12]:
    print("  event:", json.dumps(e, default=str)[:220])
print("\n".join(lines))
