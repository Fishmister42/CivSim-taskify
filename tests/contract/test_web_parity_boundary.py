"""The parity boundary, checked from the import graph and the registry.

`tests/contract/test_read_only_boundary.py` proves this feature can never
*write* to the store. This is its counterpart in the other direction, and it
exists because Principle I's asymmetry is easy to get backwards:

- the **playing agent** may only ever receive what a human player could obtain
  through the game's standard UI;
- this **web interface** may legitimately show the user and the directing
  Claude Code session *more* than the agent may receive -- run configuration,
  model identity, cost, latency, save lineage, the screening decision about a
  withheld capture. FR-013 makes that explicit, and Principle VI requires it.

So the risk here is not that the interface shows too much. It is that a read
path in this process becomes a **back channel into the agent's context** --
that something assembled for the user's screen finds its way into the prompt
that decides a game action. plan.md claims that is impossible "by construction,
not by care": *"This is a separate process with no code path back into 002's
agent-context assembly; nothing rendered here is ever consumed by the model call
that decides game actions."* A claim of that shape needs something asserting it,
which is what this file is.

Three structural facts carry the claim, and each is checked below:

1. **No module in `civsim_web` imports `civsim_harness`.** The agent's context
   assembly lives there. No import means no call means no channel.
2. **No module in `civsim_web` holds an outbound HTTP client.** This process
   serves; it never calls out. An outbound client is the one mechanism by which
   a separate process could push something into another one, so its absence is
   what makes "separate process" a boundary rather than a deployment detail --
   and it is also what keeps this feature from calling 002's operator surface,
   which plan.md Constraints forbids outright.
3. **No harness telemetry is declared `in_game`.** quickstart.md Scenario 5's
   third expectation, checked against the shipped registry: telemetry visible
   here must stay *marked* as telemetry, never laundered into looking like a
   parity-cleared observation (FR-013, panel-registry rule P2).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from civsim_web.registry.loader import default_panels_dir, load_panel_registry

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "civsim_web"


def _modules() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_package_is_being_scanned():
    """Guard against this suite silently passing over an empty directory."""
    assert len(_modules()) >= 15


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_no_module_imports_the_harness(module):
    """No code path from this process back into the agent's context assembly.

    `civsim_harness` is where the prompt that decides a game action is built.
    This package restates the `MatchStore` Protocol rather than importing 002's
    (see `store_client/port.py`) precisely so this assertion can hold.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    offenders = sorted(
        name for name in _imported_names(tree) if name.split(".")[0] == "civsim_harness"
    )
    assert not offenders, (
        f"{module.relative_to(PACKAGE_ROOT)} imports {offenders} -- this feature "
        f"must have no code path into 002's agent-context assembly (plan.md "
        f"Constitution Check, Principle I)"
    )


#: Modules that could originate an outbound request. `uvicorn` is absent from
#: this list on purpose: it serves, it does not call.
_OUTBOUND_CLIENTS = {"httpx", "requests", "aiohttp", "urllib3", "urllib.request", "http.client"}


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_no_module_holds_an_outbound_http_client(module):
    """This process serves and never calls out.

    Two requirements land on the same check. Principle I: an outbound client is
    the mechanism by which something rendered here could be pushed into the
    agent's process. plan.md Constraints: *"Nothing in this codebase calls an
    operator-surface endpoint"* -- 002 commands, this feature presents, and the
    two never meet.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    offenders = sorted(_imported_names(tree) & _OUTBOUND_CLIENTS)
    assert not offenders, (
        f"{module.relative_to(PACKAGE_ROOT)} imports {offenders}. This process "
        f"serves; it never calls out (plan.md Constraints -- the two surfaces "
        f"never merge)"
    )


#: The five categories FR-013 names as out-of-game telemetry, mapped to the 002
#: entities that carry them.
_TELEMETRY_ENTITIES = {
    "ModelCall": "model identity, cost, latency, retries",
    "ModelConfig": "model identity",
    "RunConfiguration": "run configuration",
    "SavePoint": "save lineage",
    "RunEvent": "harness errors",
}


def test_no_harness_telemetry_is_declared_in_game():
    """quickstart Scenario 5 / FR-013, against the shipped registry.

    Telemetry stays marked as telemetry. A panel that read `ModelCall.cost`
    under `category: in_game` would have to invent an in-client action for it
    (rule P1 requires one), which is exactly the laundering rule P2 exists to
    prevent -- this asserts the registry as shipped has not done it.
    """
    registry = load_panel_registry(default_panels_dir())
    offenders: list[str] = []
    for declaration in registry.declarations:
        if declaration.category != "in_game":
            continue
        for source in declaration.parsed_source_fields:
            if source.entity in _TELEMETRY_ENTITIES:
                offenders.append(
                    f"{declaration.panel_id} declares {source} "
                    f"({_TELEMETRY_ENTITIES[source.entity]}) as in_game"
                )
    assert not offenders, offenders


def test_every_in_game_panel_names_an_in_client_action():
    """SC-005's release-blocking audit, restated at the response boundary.

    The loader refuses an `in_game` panel with no `parity_basis` (rule P1), so
    this cannot fail while the registry loads -- which is the point. It is here
    so the audit is findable from the parity suite as well as from the registry
    suite, since SC-005 is worded as a release gate rather than a load rule.
    """
    registry = load_panel_registry(default_panels_dir())
    in_game = [d for d in registry.declarations if d.category == "in_game"]
    assert in_game, "the registry declares no in-game panels at all"
    for declaration in in_game:
        assert (declaration.parity_basis or "").strip(), declaration.panel_id


def test_telemetry_panels_carry_no_fabricated_parity_basis():
    """Rule P2, as shipped: a telemetry panel has `parity_basis: null`."""
    registry = load_panel_registry(default_panels_dir())
    for declaration in registry.declarations:
        if declaration.category == "out_of_game_telemetry":
            assert declaration.parity_basis is None, declaration.panel_id
