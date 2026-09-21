"""Reproduce one model call from a stored decision step and print the RAW reply content.

    uv run python .../repro_decision_call.py <run_id_prefix> <turn> <step> [--no-schema]

Rebuilds the DecisionRequest exactly as the runner does (assemble_context with the catalog's
actions and the stored observation), sends it through the production OpenRouterProvider, and
prints what the model returned before parsing. Diagnostic only; costs one call.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))

from civsim_harness.agent import decisions as dec  # noqa: E402
from civsim_harness.agent.context import assemble_context  # noqa: E402
from civsim_harness.capability.loader import load_catalog  # noqa: E402
from civsim_harness.models.common import ModelRef  # noqa: E402
from civsim_harness.models.turn import Observation  # noqa: E402
from civsim_harness.provider import openrouter as orp  # noqa: E402

prefix, turn, step = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
use_schema = "--no-schema" not in sys.argv
c = sqlite3.connect(f"file:{REPO / 'civsim-match-store.db'}?mode=ro", uri=True)
run_id = c.execute("select run_id from runs where run_id like ?", (prefix + "%",)).fetchone()[0]
row = c.execute(
    "select ds.bundle_json from decision_steps ds join turn_cycles tc on tc.turn_cycle_id=ds.turn_cycle_id "
    "where tc.run_id=? and tc.turn_number=? and ds.step_index=?", (run_id, turn, step)).fetchone()
bundle = json.loads(row[0])
observation = Observation.model_validate(bundle["observation"])
catalog = load_catalog(REPO / "catalogs")
actions = list(catalog.declarations.values())
request = assemble_context(
    observation=observation, guidance=None, model=ModelRef(provider="openrouter", model="anthropic/claude-sonnet-5"),
    step_index=step, response_schema=dec.RESPONSE_SCHEMA if use_schema else {}, actions=actions,
)
print("=== observation text (tail) ===")
print(request.observation[-1200:])
captured = {}
orig = orp.parse_decision
def spy(content):
    captured["raw"] = content
    return orig(content)
orp.parse_decision = spy
provider = orp.OpenRouterProvider()
method = provider.complete
resp = method(request)
print("=== RAW CONTENT ===")
print(captured.get("raw"))
print("=== parsed ===")
print(resp.decision)
print("outcome", resp.outcome, "cost", resp.cost)
