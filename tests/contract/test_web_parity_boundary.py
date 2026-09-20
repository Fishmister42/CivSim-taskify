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
import re
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


# --------------------------------------------------------------------------
# US2 (T035/T038) -- the panel projection, checked against the registry
# --------------------------------------------------------------------------


def test_every_shipped_panel_can_be_placed_on_the_view_its_scope_names():
    """A registered panel with no place on its own view is invisible by accident.

    `viewmodels/panel.py` resolves a panel reference by locating each of 002's
    entities inside the enclosing view model, so a panel declaring an entity
    that map has no entry for would answer with an explanation and no content --
    which reads to a user exactly like a run with nothing recorded. The two must
    not be confusable (UP-005), so the drift is caught here, on the build,
    rather than discovered as an empty panel.

    This is the check that makes the projection honest as the registry grows:
    adding a panel over an already-placed entity needs no code change, and
    adding one over a *new* entity fails here until someone places it.
    """
    from civsim_web.viewmodels.panel import ENTITY_PATHS

    registry = load_panel_registry(default_panels_dir())
    offenders: list[str] = []
    for declaration in registry.declarations:
        placeable = ENTITY_PATHS[declaration.scope]
        for source in declaration.parsed_source_fields:
            if source.entity not in placeable:
                offenders.append(
                    f"{declaration.panel_id} ({declaration.scope}-scoped) declares "
                    f"{source}, and viewmodels/panel.py has no place for "
                    f"{source.entity!r} on a {declaration.scope} view"
                )
    assert not offenders, offenders


def test_the_panel_projection_never_reaches_past_the_declared_fields():
    """UP-001 at the panel endpoint: `source_fields` is the whole permission.

    Every value a panel response carries names the `Entity.field` declaration
    that permitted it, and that declaration must be one the panel actually
    declares. A projection that widened -- returning a neighbouring key because
    it happened to sit on the same node -- would be the panel endpoint reading a
    field the registry never cleared, which is the one thing the registry exists
    to prevent.
    """
    from civsim_web.viewmodels.panel import project_panel

    registry = load_panel_registry(default_panels_dir())
    panel = registry.get("run.intervention")
    assert panel is not None, "the fixture panel for this check is gone; pick another"

    data = {
        "intervention_info": {
            "run_id": "run-1",
            "lifecycle_status": "playing",
            "last_known_good_turn": 3,
            "last_known_good_save_id": "save-1",
            "last_known_good_save_name": "civsim__run-1__t0003",
        }
    }
    values, unplaceable = project_panel(panel, data, {"SavePoint": ("intervention_info",)})

    assert not unplaceable
    assert {value.source for value in values} <= set(panel.source_fields)
    # `run_id` and `lifecycle_status` sit on the same node and are declared by a
    # different panel (`run.header`). This one must not pick them up.
    assert not any(
        value.path.endswith((".run_id", ".lifecycle_status")) for value in values
    )


# --------------------------------------------------------------------------
# US4 (T057) -- the comparison boundary, checked from the type graph
#
# The same shape of argument as the three above, applied to the one requirement
# this feature states as a property of a *type* rather than of a response:
# FR-035 and SC-016 require every comparison and trend question to be answerable
# with every capture in every compared run withheld, and data-model.md SS11 says
# how: *"this entire model is constructed with `CaptureView` nowhere in its type
# -- not merely unused, but structurally absent, so a future addition to this
# view cannot accidentally introduce a capture dependency into a comparison
# question without changing the type."*
#
# A response-level scan (`tests/contract/test_web_read_api.py`) proves no
# capture reached one particular body. Only a type-level walk proves none can.
# --------------------------------------------------------------------------


def _reachable_models(root: type) -> set[type]:
    """Every Pydantic model reachable from `root` through its annotations."""
    from pydantic import BaseModel

    seen: set[type] = set()
    pending = [root]
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        for info in model.model_fields.values():
            for candidate in _annotation_types(info.annotation):
                if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                    pending.append(candidate)
    return seen


def _annotation_types(annotation: object) -> list[object]:
    """Flatten an annotation into the concrete types it can hold.

    Walks `tuple[X, ...]`, `list[X]`, `dict[K, V]`, `X | None`, and nestings of
    those, because a capture smuggled in as `dict[str, list[CaptureView]]` is
    exactly as much of a dependency as a bare field would be.
    """
    from typing import get_args

    found: list[object] = [annotation]
    for argument in get_args(annotation):
        found.extend(_annotation_types(argument))
    return found


def test_the_comparison_view_has_no_capture_anywhere_in_its_type():
    """Invariant V7, from the type graph rather than from one response.

    FR-035 / SC-016: the comparison view never depends on a capture. This walks
    every model reachable from `ComparisonView` through its field annotations
    and asserts `CaptureView` is not among them -- so adding one, at any depth,
    fails here rather than quietly making a trend question depend on whether a
    screenshot survived screening.
    """
    from civsim_web.viewmodels.capture import CaptureView
    from civsim_web.viewmodels.comparison import ComparisonView

    reachable = _reachable_models(ComparisonView)
    assert len(reachable) >= 5, "the walk found almost nothing -- it is not walking"
    assert CaptureView not in reachable, (
        "ComparisonView can reach CaptureView through its type. data-model.md "
        "SS11 requires captures be structurally absent from this model, so a "
        "comparison stays answerable with every capture withheld (FR-035, SC-016)"
    )
    offenders = sorted(
        model.__name__
        for model in reachable
        if "capture" in model.__name__.lower()
    )
    assert not offenders, offenders


def test_the_comparison_module_imports_nothing_capture_shaped():
    """The import graph half of the same claim.

    A model cannot reach a type its module never imports, so this is the cheaper
    check -- kept alongside the type walk because the two fail for different
    reasons and a reader of one failure wants the other's answer.
    """
    import ast

    module = PACKAGE_ROOT / "viewmodels" / "comparison.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    offenders = sorted(name for name in _imported_names(tree) if "capture" in name.lower())
    assert not offenders, f"viewmodels/comparison.py imports {offenders}"


def test_the_compare_route_reads_no_capture_operation():
    """`GET /compare` never calls a capture read (FR-035).

    Asserted against the route module's own AST: `get_capture`,
    `get_capture_blob`, and `capture_records_for_turn` are the three ways this
    codebase can reach a capture, and none of them may appear on the path that
    answers a comparison question.
    """
    import ast

    capture_reads = {"get_capture", "get_capture_blob", "capture_records_for_turn"}
    for name in ("compare.py", "catalog.py"):
        module = PACKAGE_ROOT / "routes" / name
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        offenders = sorted(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in capture_reads
        )
        assert not offenders, f"routes/{name} reaches for {offenders}"


def test_no_catalog_panel_launders_telemetry_into_an_in_game_basis():
    """Rule P2 against US4's own declarations specifically.

    `test_no_harness_telemetry_is_declared_in_game` above covers the whole
    registry; this narrows to `catalog.yaml` so a US4 regression names US4 in
    the failure rather than being reported as a registry-wide problem.
    """
    registry = load_panel_registry(default_panels_dir())
    catalog_panels = [d for d in registry.declarations if d.declared_in == "catalog.yaml"]
    assert catalog_panels, "catalog.yaml declares no panels"
    for declaration in catalog_panels:
        if declaration.category == "out_of_game_telemetry":
            assert declaration.parity_basis is None, declaration.panel_id
        else:
            assert (declaration.parity_basis or "").strip(), declaration.panel_id
            for source in declaration.parsed_source_fields:
                assert source.entity not in _TELEMETRY_ENTITIES, (
                    f"{declaration.panel_id} declares {source} as in_game"
                )


# ==========================================================================
# The trajectory renderer (T045/T056) -- source-level guards
# ==========================================================================

#: `static/trajectory.js` is the one place in this feature where a rule is
#: implemented twice: once in Python (`MetricSeriesView.segments`) for the
#: server-rendered chart both readers get, and once in JavaScript for the
#: figure the replay page inlines. This project has **no JavaScript test tier**
#: and deliberately so -- plan.md's Primary Dependencies decline a bundler and
#: an npm dependency tree, and adding a JS runner to assert one function would
#: introduce the second ecosystem research R2 rejected. So the behavioural guard
#: on the break rule lives where the load-bearing chart lives, on the server
#: (`tests/integration/test_replay.py::
#: test_a_gapped_turn_is_a_break_in_the_trajectory_not_a_join`), and these two
#: tests guard the copy against the edits most likely to reintroduce the join.
TRAJECTORY_JS = PACKAGE_ROOT / "static" / "trajectory.js"


def test_the_trajectory_script_breaks_the_line_rather_than_joining_across_a_gap():
    """A polyline is built from a *segment*, never from a raw `points` array.

    data-model.md SS10 permits a gapped turn to be omitted from `points` only
    because the gap stays visible as a break. The server omits the turn, so a
    renderer that walks `points` straight into one polyline draws a line across
    the hole -- the interpolation the same paragraph forbids, arrived at by not
    thinking about it.
    """
    source = TRAJECTORY_JS.read_text(encoding="utf-8")

    assert "function segmentsOf(" in source, (
        "the segmentation rule is gone; without it a series is drawn as one "
        "unbroken line through every recorded point"
    )
    # The fail-closed comparison itself: a new segment starts whenever two
    # consecutive points are not on consecutive turns.
    assert "current[current.length - 1].turn + 1" in source

    # Every polyline is built inside the segment loop.
    for match in re.finditer(r'el\("polyline"', source):
        window = source[max(0, match.start() - 600) : match.start()]
        assert "segments[i]" in window, (
            "a polyline is being built outside the segment loop -- that is the "
            "join across the gap"
        )


def test_the_trajectory_script_draws_from_series_never_from_runs():
    """Principle III, on the client side of the same quarantine.

    A quarantined run is listed in `runs` and absent from `series` by design
    (`viewmodels/comparison.py`). A script that iterated `runs` to decide what
    to draw would put a trend line back under a run whose turn-by-turn record
    has gaps -- reintroducing on the client exactly what the server's model
    validator refuses to construct.

    `runs` is read for one thing only: a run's *position* in it is its colour
    index, which is what keeps the legend honest.
    """
    source = TRAJECTORY_JS.read_text(encoding="utf-8")

    uses = re.findall(r"payload\.runs", source)
    assert uses, "the colour index is derived from `runs`; that use should exist"
    assert len(uses) == 1, (
        f"`payload.runs` is read in {len(uses)} places; it may be read only to "
        f"index the palette (see `colourIndexes`). Anything else risks drawing a "
        f"quarantined run."
    )
    assert "drawSeries(svg, byMetric[chosen][i]" in source, (
        "the drawn series come from `series`, not from `runs`"
    )
