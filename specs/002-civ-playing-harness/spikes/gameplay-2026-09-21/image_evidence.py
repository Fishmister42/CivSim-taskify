"""Did an image reach the agent? Read-only, per step, from the store (T260 live evidence).

    uv run python specs/002-civ-playing-harness/spikes/gameplay-2026-09-21/image_evidence.py <run_id_prefix>

Prints the run's recorded host_support_tier and, for every decision step: the observation's
capture ids, each capture's screening_status / shown_to_agent / blob_ref presence, the model
call's image_count, and every capture_failed / image_withheld event's recorded reason. Nothing
here is inferred: a "delivered" line requires screened_clean + shown_to_agent + image_count >= 1.
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

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
run_id = c.execute("select run_id from runs where run_id like ?", (prefix + "%",)).fetchone()[0]
run = json.loads(c.execute("select run_json from runs where run_id=?", (run_id,)).fetchone()[0])
print(f"run {run_id}: tier={run.get('host_support_tier')} capture_path={run.get('capture_path')}")

captures = {}
for (cj,) in c.execute("select capture_json from captures where run_id=?", (run_id,)):
    cap = json.loads(cj)
    captures[cap.get("capture_id")] = cap

events = [json.loads(r[0]) for r in c.execute("select event_json from run_events where run_id=? order by rowid", (run_id,))]
reasons: Counter = Counter()
for e in events:
    if e.get("event_type") in ("capture_failed", "image_withheld"):
        d = e.get("detail") or {}
        reasons[(e["event_type"], str(d.get("withheld_reason")), str(d.get("reason") or d.get("target_unavailable_reason") or ""))] += 1
print("withheld/failed event reasons:")
for (et, wr, r), n in reasons.most_common():
    print(f"  {n:3d}  {et} / {wr} / {r[:140]}")

delivered = 0
rows = c.execute(
    "select tc.turn_number, ds.step_index, ds.bundle_json from decision_steps ds join turn_cycles tc on tc.turn_cycle_id=ds.turn_cycle_id where tc.run_id=? order by tc.turn_number, ds.step_index",
    (run_id,),
).fetchall()
for tn, si, bj in rows:
    b = json.loads(bj)
    obs = b.get("observation") or {}
    mc = b.get("model_call") or {}
    cap_ids = obs.get("captures") or []
    caps = [captures.get(cid) or {"capture_id": cid, "missing": True} for cid in cap_ids]
    summary = ", ".join(
        f"{(cp.get('screening_status'))}/{'shown' if cp.get('shown_to_agent') else 'not-shown'}/{'blob' if cp.get('blob_ref') else 'no-blob'}"
        for cp in caps
    ) or "(no capture ids on the observation)"
    ic = mc.get("image_count")
    ok = any(cp.get("screening_status") == "screened_clean" and cp.get("shown_to_agent") for cp in caps) and (ic or 0) >= 1
    delivered += 1 if ok else 0
    print(f"  t{tn} s{si}: captures[{summary}] image_count={ic} {'DELIVERED' if ok else ''}")
print(f"steps with an image delivered: {delivered} of {len(rows)}")
