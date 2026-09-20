"""Reachability: every wired interface has a production caller (T235).

This repo's defining defect pattern is the complete, well-tested module with
**no caller anywhere in `src/`**: the fabricated V2 verification, the dead Lua
``return``s, the duplicated parity filter (T231), the dead accounting module
(T232), the unwired detection layer (T233), and the nexus fake that spoke the
protocol the code expected rather than the one the client speaks (T234 -- 1539
green tests over a transport that could not drive any real client). Each time,
the tests were green because a fake shared the defect's assumption. The
adopted structural counter (hypervisor log, Run 4: the Linux peer's proposal
plus the Scribe's refinement) is **two rules, both required**:

1. **Reachability** -- every method of the `HostPlatform` port, every
   resilience/detection collaborator, the parity filter, provider accounting,
   the nexus client's public surface, and the composition root's build
   products must have at least one caller in `src/civsim_harness/` outside
   its own defining module. Enforced here, mechanically, from the AST.
2. **Negative controls** -- tests proving each check CAN fail, by feeding the
   same checker a synthetic module tree containing a known violation and
   asserting it is flagged. A check that cannot be shown to fail is the
   fake-shares-the-assumption problem recursed one level up (that is exactly
   how the string-literal doctor defect survived: it *had* a caller, and
   nothing proved the check on it could fire). The bottom half of this file
   is those controls.

**Why an AST scan and not a text scan:** the same reason
``tests/contract/test_read_only_boundary.py`` and the T231 check in
``tests/contract/test_parity_redteam.py`` give -- half the modules in scope
*name* these symbols in prose to document what they must not do, and a text
match would flag exactly the files whose job is to be explicit about the
boundary. An import or an ``__init__.py`` re-export contributes **nothing**
here: ``ast.ImportFrom`` produces no ``Name`` load and ``__all__`` strings
are constants, so the T233 opening symptom ("outside their own modules they
appear only in `resilience/__init__.py`'s re-export list") reads as
unreached, which is the point.

**Honest limits, stated rather than hidden:** matching is by name, not by
resolved type, so a same-named call on an unrelated object (``sqlite3
.connect`` vs ``NexusClient.connect``) can satisfy a target. That residual
risk only ever errs toward a false PASS on a name that some *other* object
genuinely uses, never toward blocking a merge; the family this test hunts --
zero callers under a name nothing else uses -- cannot hide behind it, and the
negative controls prove the tripwire fires on precisely that family. Scope is
``src/civsim_harness/`` alone: a reference from ``src/civsim_web`` (a separate
deliverable with its own read-only port) does not make a harness interface
reachable for the harness's own runs, and test/spike callers never count --
the demo's ``send_input`` caller lives in ``specs/.../spikes/r5-raw-windows/``
and left the production layer exactly as dead as the peer's audit found it.

The allowlist below is the deliberate, documented home for the honestly
uncalled. Every entry must cite a task ID (or carry the ``UNRESOLVED -
hypervisor review`` marker for findings awaiting a ruling); an empty or
uncited justification fails, and an entry whose target has since gained a
caller fails as stale, so the list can only ratchet down.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS_ROOT = _REPO_ROOT / "src" / "civsim_harness"

# --------------------------------------------------------------------------
# The checker: an AST reference scan plus a per-target reached/unreached rule.
# Everything below is driven through these same functions by both the real
# tree and the synthetic negative-control trees -- one checker, so the
# controls exercise the code the enforcement actually runs.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleRefs:
    """Every name a module *reaches for*, bucketed by how it reaches.

    Definitions do not appear here at all -- a ``FunctionDef`` named
    ``send_input`` in an adapter is an implementation, not a caller -- and
    neither do imports or ``__all__`` strings (see the module docstring).
    """

    name_loads: frozenset[str]
    name_calls: frozenset[str]
    attr_loads: frozenset[str]
    attr_calls: frozenset[str]
    keywords: frozenset[str]


def _scan_module(path: Path) -> ModuleRefs:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name_loads: set[str] = set()
    name_calls: set[str] = set()
    attr_loads: set[str] = set()
    attr_calls: set[str] = set()
    keywords: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                name_calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                attr_calls.add(node.func.attr)
            for keyword in node.keywords:
                if keyword.arg is not None:
                    keywords.add(keyword.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name_loads.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            attr_loads.add(node.attr)
    return ModuleRefs(
        name_loads=frozenset(name_loads),
        name_calls=frozenset(name_calls),
        attr_loads=frozenset(attr_loads),
        attr_calls=frozenset(attr_calls),
        keywords=frozenset(keywords),
    )


def _scan_tree(root: Path) -> dict[str, ModuleRefs]:
    """Scan every ``*.py`` under *root*, keyed by posix-style relative path."""
    return {
        path.relative_to(root).as_posix(): _scan_module(path)
        for path in sorted(root.rglob("*.py"))
    }


@dataclass(frozen=True)
class Target:
    """One interface the harness is supposed to wire, and how a caller looks.

    ``kind`` picks the reference bucket a caller must appear in:

    - ``"method"`` -- an attribute call ``obj.name(...)`` (port methods).
    - ``"called"`` -- any call, bare or attribute (functions, constructors).
    - ``"read"`` -- an attribute access (properties like ``state_indices``).
    - ``"referenced"`` -- any load at all. The loosest rule, reserved for
      symbols whose production wiring is legitimately not a call: `NexusClient`
      is wired as the composition root's *default factory value*, and protocol
      classes like `OperationKind` are consumed via member access.
    - ``"seam"`` -- a dependency-seam field. Two-sided: some module **other
      than the defining one** must supply it as a keyword (the composition
      root filling the seam), and some module must read it as an attribute
      (the runner consuming it). Either side missing is a dead seam.

    ``also_excluded`` lists path prefixes whose references never count -- the
    port's platform adapters, so an implementation calling itself cannot
    vouch for its own reachability (the production-side twin of the
    fake-shares-the-assumption shape).
    """

    name: str
    kind: str
    defined_in: str
    also_excluded: tuple[str, ...] = ()


def _counts_as_caller(target: Target, module: str) -> bool:
    if module == target.defined_in:
        return False
    return not any(module.startswith(prefix) for prefix in target.also_excluded)


def _refs_for_kind(kind: str, refs: ModuleRefs) -> frozenset[str]:
    if kind == "method":
        return refs.attr_calls
    if kind == "called":
        return refs.name_calls | refs.attr_calls
    if kind == "read":
        return refs.attr_loads | refs.attr_calls
    if kind == "referenced":
        return (
            refs.name_loads | refs.name_calls | refs.attr_loads | refs.attr_calls | refs.keywords
        )
    raise ValueError(f"unknown target kind: {kind!r}")


def _is_reached(target: Target, refs_by_module: dict[str, ModuleRefs]) -> bool:
    if target.kind == "seam":
        supplied = any(
            target.name in refs.keywords
            for module, refs in refs_by_module.items()
            if _counts_as_caller(target, module)
        )
        consumed = any(
            target.name in (refs.attr_loads | refs.attr_calls)
            for refs in refs_by_module.values()
        )
        return supplied and consumed
    return any(
        target.name in _refs_for_kind(target.kind, refs)
        for module, refs in refs_by_module.items()
        if _counts_as_caller(target, module)
    )


def _reachability_problems(
    targets: list[Target],
    refs_by_module: dict[str, ModuleRefs],
    allowlist: dict[str, str],
) -> list[str]:
    """Every way the wiring claim can be false, as one flat list of findings.

    Three failure shapes, so the allowlist can only ratchet down: an unreached
    target nobody justified, a justified target that has since gained a caller
    (the entry is stale -- delete it, the tool's coverage just grew), and an
    allowlist entry naming nothing in the roster (a typo, or a target someone
    deleted without deleting its excuse).
    """
    problems: list[str] = []
    target_names = {target.name for target in targets}
    for target in targets:
        reached = _is_reached(target, refs_by_module)
        if not reached and target.name not in allowlist:
            problems.append(
                f"UNREACHED: {target.kind} target {target.name!r} (defined in "
                f"{target.defined_in}) has no production caller outside its defining "
                f"module -- wire it, or allowlist it with a justification citing its task"
            )
        elif reached and target.name in allowlist:
            problems.append(
                f"STALE ALLOWLIST ENTRY: {target.name!r} now has a production caller; "
                f"delete its allowlist entry so the check guards it from here on"
            )
    for name in allowlist:
        if name not in target_names:
            problems.append(
                f"UNKNOWN ALLOWLIST ENTRY: {name!r} names no target in the roster"
            )
    return problems


_UNRESOLVED_MARKER = "UNRESOLVED - hypervisor review"
_TASK_CITATION = re.compile(r"\bT\d{3}\b")


def _allowlist_problems(allowlist: dict[str, str]) -> list[str]:
    """An allowlist entry is only as good as its citation.

    T232 is the model: "record_model_call remains uncalled by design" is in
    the ledger, so the entry can point at it. An entry with no justification,
    or prose citing neither a task ID nor the UNRESOLVED marker, is an
    unaudited exemption -- exactly the quiet hole this file exists to close --
    and fails the suite by itself.
    """
    problems: list[str] = []
    for name, justification in allowlist.items():
        if not justification.strip():
            problems.append(f"{name!r}: empty justification -- every entry must say why")
        elif (
            _UNRESOLVED_MARKER not in justification
            and not _TASK_CITATION.search(justification)
        ):
            problems.append(
                f"{name!r}: justification cites no task ID and carries no "
                f"{_UNRESOLVED_MARKER!r} marker: {justification!r}"
            )
    return problems


# --------------------------------------------------------------------------
# The roster. Port methods, the nexus surface, and the runner's dependency
# seam are DERIVED from the production AST rather than restated here, so a
# method added to any of those surfaces is guarded the moment it exists --
# a hand-copied list would go quietly stale, which is this family's move.
# The named collaborators are the specific interfaces where the pattern
# already bit once (T225, T227, T231-T234) plus the composition root's
# build product itself.
# --------------------------------------------------------------------------


def _class_body(path: Path, class_name: str) -> list[ast.stmt]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node.body
    raise AssertionError(f"class {class_name} not found at module top level in {path}")


def _public_methods_and_properties(path: Path, class_name: str) -> tuple[list[str], list[str]]:
    methods: list[str] = []
    properties: list[str] = []
    for stmt in _class_body(path, class_name):
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            if stmt.name.startswith("_"):
                continue
            is_property = any(
                isinstance(decorator, ast.Name) and decorator.id == "property"
                for decorator in stmt.decorator_list
            )
            (properties if is_property else methods).append(stmt.name)
    return methods, properties


def _dataclass_field_names(path: Path, class_name: str) -> list[str]:
    return [
        stmt.target.id
        for stmt in _class_body(path, class_name)
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]


#: The port's platform implementations: a self-call inside one of these is an
#: adapter talking to itself, never evidence the harness wired the port.
_ADAPTER_PREFIXES = ("host/windows/", "host/macos/", "host/linux/")

_NAMED_COLLABORATORS: tuple[Target, ...] = (
    # The parity boundary (T225, T231): the one structural filter and the
    # per-step red-team guard in front of the provider.
    Target("filter_to_entries", "called", "parity/filter.py"),
    Target("resolve_observable", "called", "parity/filter.py"),
    Target("enforce_parity_boundary", "called", "parity/forbidden.py"),
    Target("assert_no_literal_leaks", "called", "parity/forbidden.py"),
    # Provider accounting (T232): one definition of "a completed call becomes
    # a ModelCall", with the P2 image-count re-check on the loop's own path.
    Target("build_model_call", "called", "provider/accounting.py"),
    Target("record_model_call", "called", "provider/accounting.py"),
    Target("ModelCallSink", "referenced", "provider/accounting.py"),
    # The detection/recovery layer (T233): research R12's signals, which were
    # complete, tested, and constructed nowhere until this loop.
    Target("DetectionAggregator", "called", "resilience/detector.py"),
    Target("check_process_liveness", "called", "resilience/detector.py"),
    Target("ScreenIdentityProbe", "referenced", "resilience/detector.py"),
    Target("HeartbeatMonitor", "called", "resilience/heartbeat_monitor.py"),
    Target("ProcessLivenessMonitor", "called", "resilience/liveness.py"),
    Target("run_bounded", "called", "resilience/operation_bounds.py"),
    Target("OperationKind", "referenced", "resilience/operation_bounds.py"),
    Target("OperationTimedOut", "referenced", "resilience/operation_bounds.py"),
    Target("RecoveryEngine", "called", "resilience/recovery.py"),
    Target("DetectionWatch", "called", "run/detection.py"),
    # The composition root's build product, and the client every run speaks
    # through. `NexusClient` is "referenced" because its production wiring is
    # the default *factory value* in build_runner_dependencies / doctor, not
    # a literal constructor call -- demanding a call would flag real wiring.
    Target("build_runner_dependencies", "called", "run/composition.py"),
    Target("NexusClient", "referenced", "nexus/client.py"),
    # The Linux adapter's R6 capture preflight -- outside the six-method port,
    # named by the hypervisor log as one of the seven this check exists for.
    Target("capture_preconditions", "method", "host/linux/adapter.py", _ADAPTER_PREFIXES),
    # The synthetic-input value type: adapters only *annotate* with it, so a
    # production caller must construct one somewhere portable.
    Target("InputEvent", "called", "host/port.py", _ADAPTER_PREFIXES),
)


def _harness_targets() -> list[Target]:
    targets: list[Target] = list(_NAMED_COLLABORATORS)
    port_methods, _ = _public_methods_and_properties(
        _HARNESS_ROOT / "host" / "port.py", "HostPlatform"
    )
    targets.extend(
        Target(name, "method", "host/port.py", _ADAPTER_PREFIXES) for name in port_methods
    )
    nexus_methods, nexus_properties = _public_methods_and_properties(
        _HARNESS_ROOT / "nexus" / "client.py", "NexusClient"
    )
    targets.extend(Target(name, "method", "nexus/client.py") for name in nexus_methods)
    targets.extend(Target(name, "read", "nexus/client.py") for name in nexus_properties)
    seam_fields = _dataclass_field_names(
        _HARNESS_ROOT / "run" / "runner.py", "RunnerDependencies"
    )
    targets.extend(Target(name, "seam", "run/runner.py") for name in seam_fields)
    names = [target.name for target in targets]
    assert len(set(names)) == len(names), (
        "target names must be unique -- the allowlist is keyed by name, and a collision "
        f"would let one justification silently cover two interfaces: {sorted(names)}"
    )
    return targets


# --------------------------------------------------------------------------
# The allowlist: the honestly-uncalled, each with its citation. Adding a name
# here without a task ID (or the UNRESOLVED marker for findings awaiting a
# hypervisor ruling) fails the suite -- see _allowlist_problems.
# --------------------------------------------------------------------------

ALLOWLIST: dict[str, str] = {
    "record_model_call": (
        "T232: uncalled by design -- the decision loop builds via build_model_call and "
        "writes the ModelCall itself; a build-and-write helper would double-write the "
        "success path (T232 LANDED note)"
    ),
    "ModelCallSink": (
        "T232: the store-facing seam record_model_call takes; uncalled by design for the "
        "same recorded reason as record_model_call"
    ),
    "ScreenIdentityProbe": (
        "T233: deliberately not wired -- run/decision_loop.py already polls "
        "game.screen_state and raises UnknownScreenEncountered (FR-049), and a second "
        "producer of unknown_screen would put two paths on one timeline entry"
    ),
    "assert_no_literal_leaks": (
        "T225: production literal scanning runs inside enforce_parity_boundary; this "
        "helper is the load-bearing test tier's own entry point (T128, consumed by "
        "tests/unit/test_telemetry_exclusion.py) with no second production role"
    ),
    "send_input": (
        "UNRESOLVED - hypervisor review: no caller anywhere in src/ on any platform; the "
        "demo's 'first production caller' (hypervisor log, Run 5) lives in "
        "specs/002-civ-playing-harness/spikes/r5-raw-windows/{bringup,advance_to_frontend}"
        ".py, outside the package, and T217's Network.LoadGame resolution removed the "
        "planned production consumer (option B: 'no bespoke driver, ever')"
    ),
    "InputEvent": (
        "UNRESOLVED - hypervisor review: constructed nowhere in src/ (the three adapters "
        "only annotate with it); same evidence and same open question as send_input -- "
        "wire a portable caller or retire the synthetic-input layer by ruling"
    ),
    "capture_preconditions": (
        "UNRESOLVED - hypervisor review: the Linux adapter's R6 capture preflight has "
        "callers only in tests/live and spikes; validation-results.md already flags it "
        "('no production caller -- this one is ours') and no Phase 11 entry covers it"
    ),
}


# --------------------------------------------------------------------------
# Enforcement against the real tree.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def harness_refs() -> dict[str, ModuleRefs]:
    return _scan_tree(_HARNESS_ROOT)


def test_the_harness_tree_is_being_scanned(harness_refs: dict[str, ModuleRefs]) -> None:
    """Guard against this suite silently passing over an empty or moved tree."""
    assert len(harness_refs) >= 50, (
        f"only {len(harness_refs)} modules found under {_HARNESS_ROOT} -- the scan root "
        "is wrong or the package moved, and every reachability verdict below is vacuous"
    )


def test_the_derived_rosters_found_the_surfaces_they_claim_to_guard() -> None:
    """The checker-that-cannot-fail problem, one level down: if the AST derivation
    quietly returned nothing, every target would be trivially absent and the suite
    would still be green. Pin the surfaces it must find.
    """
    port_methods, _ = _public_methods_and_properties(
        _HARNESS_ROOT / "host" / "port.py", "HostPlatform"
    )
    assert {
        "locate_game_process",
        "find_game_window",
        "capture_window",
        "resolve_game_directories",
        "send_input",
        "free_disk_space",
    } <= set(port_methods), f"HostPlatform derivation lost methods: {sorted(port_methods)}"

    nexus_methods, nexus_properties = _public_methods_and_properties(
        _HARNESS_ROOT / "nexus" / "client.py", "NexusClient"
    )
    assert {"connect", "execute_command", "refresh_state_indices"} <= set(nexus_methods)
    assert {"state_indices", "is_connected"} <= set(nexus_properties)

    seam_fields = _dataclass_field_names(_HARNESS_ROOT / "run" / "runner.py", "RunnerDependencies")
    assert {"store", "prepare_run", "build_turn_dependencies", "evaluate_stop_facts"} <= set(
        seam_fields
    )


def test_every_wired_interface_has_a_production_caller_or_a_cited_exemption(
    harness_refs: dict[str, ModuleRefs],
) -> None:
    """The reachability rule itself, over the full roster.

    A failure here means one of three things, and the message says which: an
    interface exists that no production code reaches (the T225/T231/T232/T233
    shape -- wire it or justify it), an allowlist entry went stale because the
    wiring landed (delete the entry), or an entry names nothing (fix the typo).
    Never silence this by loosening a Target's kind: "referenced" is reserved
    for the symbols whose wiring is legitimately not a call, and each one says
    why at its declaration.
    """
    problems = _reachability_problems(_harness_targets(), harness_refs, ALLOWLIST)
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("name", sorted(ALLOWLIST))
def test_every_allowlist_entry_carries_a_cited_justification(name: str) -> None:
    """Per-entry, so the failing name is in the test id, not buried in a diff."""
    assert _allowlist_problems({name: ALLOWLIST[name]}) == []


# --------------------------------------------------------------------------
# Negative controls: proof each check CAN fail. Every control feeds the same
# _scan_tree/_reachability_problems/_allowlist_problems the real enforcement
# runs, on a synthetic tree carrying one known violation, and asserts the
# violation is flagged -- and that its correctly-wired twin is not.
# --------------------------------------------------------------------------


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")


def test_negative_control_an_uncalled_port_method_is_flagged(tmp_path: Path) -> None:
    """The core tripwire fires: `frob` has an implementation and no caller, and
    the checker says so; `ping`, identical but for its one call site, passes.
    """
    _write_tree(
        tmp_path,
        {
            "port.py": """\
                class Widget:
                    def ping(self) -> None: ...

                    def frob(self) -> None: ...
                """,
            "app.py": """\
                def use(widget):
                    return widget.ping()
                """,
        },
    )
    targets = [Target("ping", "method", "port.py"), Target("frob", "method", "port.py")]
    problems = _reachability_problems(targets, _scan_tree(tmp_path), allowlist={})
    assert len(problems) == 1 and "frob" in problems[0], problems
    assert not any("'ping'" in problem for problem in problems)


def test_negative_control_an_import_or_reexport_alone_is_not_a_caller(tmp_path: Path) -> None:
    """T233's exact opening symptom: outside its own module the class appears
    only in an ``__init__.py`` re-export (plus an unused import elsewhere). If
    either counted as a caller, the very defect this file was written against
    would have passed its own guard.
    """
    _write_tree(
        tmp_path,
        {
            "pkg/__init__.py": """\
                from pkg.detector import Aggregator

                __all__ = ["Aggregator"]
                """,
            "pkg/detector.py": """\
                class Aggregator:
                    def check_once(self) -> None: ...
                """,
            "main.py": """\
                from pkg import Aggregator
                """,
        },
    )
    targets = [Target("Aggregator", "called", "pkg/detector.py")]
    problems = _reachability_problems(targets, _scan_tree(tmp_path), allowlist={})
    assert len(problems) == 1 and "Aggregator" in problems[0], problems


def test_negative_control_a_call_in_the_defining_module_does_not_count(tmp_path: Path) -> None:
    """A module invoking its own deliverable is self-confirmation, not wiring --
    the AST analogue of the fake written in the same sitting as the code.
    """
    _write_tree(
        tmp_path,
        {
            "helpers.py": """\
                def helper() -> int:
                    return 1


                def caller() -> int:
                    return helper()
                """,
        },
    )
    targets = [Target("helper", "called", "helpers.py")]
    problems = _reachability_problems(targets, _scan_tree(tmp_path), allowlist={})
    assert len(problems) == 1 and "helper" in problems[0], problems


def test_negative_control_an_adapters_self_call_does_not_satisfy_the_port(
    tmp_path: Path,
) -> None:
    """`also_excluded` is load-bearing: an implementation calling itself must not
    vouch for the port's reachability, or three platform adapters full of
    internal dispatch would mask a portless method forever (the send_input
    shape, where only spike scripts ever called the layer).
    """
    _write_tree(
        tmp_path,
        {
            "port.py": """\
                class Port:
                    def frob(self) -> None: ...
                """,
            "adapters/linux.py": """\
                class LinuxPort:
                    def frob(self) -> None:
                        return self.frob()
                """,
        },
    )
    targets = [Target("frob", "method", "port.py", also_excluded=("adapters/",))]
    problems = _reachability_problems(targets, _scan_tree(tmp_path), allowlist={})
    assert len(problems) == 1 and "frob" in problems[0], problems


def test_negative_control_a_dead_seam_field_is_flagged_from_either_side(
    tmp_path: Path,
) -> None:
    """The seam rule is two-sided and both sides can fail: `unread` is supplied
    but never consumed (composition builds what nothing reads -- the dead
    accounting shape), `orphan` is consumed but never supplied (the runner
    declares a seam the composition never fills -- the prepare_branch-before-
    T226 shape). `widget_factory`, wired on both sides, passes.
    """
    _write_tree(
        tmp_path,
        {
            "runner.py": """\
                from dataclasses import dataclass


                @dataclass(frozen=True)
                class Deps:
                    widget_factory: object
                    unread: object
                    orphan: object = None


                class Runner:
                    def __init__(self, deps: Deps) -> None:
                        self._deps = deps

                    def tick(self) -> object:
                        return self._deps.widget_factory()

                    def status(self) -> object:
                        return self._deps.orphan
                """,
            "composition.py": """\
                from runner import Deps


                def build() -> Deps:
                    return Deps(widget_factory=list, unread=set)
                """,
        },
    )
    targets = [
        Target("widget_factory", "seam", "runner.py"),
        Target("unread", "seam", "runner.py"),
        Target("orphan", "seam", "runner.py"),
    ]
    problems = _reachability_problems(targets, _scan_tree(tmp_path), allowlist={})
    assert len(problems) == 2, problems
    assert any("unread" in problem for problem in problems), problems
    assert any("orphan" in problem for problem in problems), problems
    assert not any("widget_factory" in problem for problem in problems), problems


def test_negative_control_a_stale_allowlist_entry_is_flagged(tmp_path: Path) -> None:
    """The ratchet: once a target gains a real caller, its exemption must go.
    Without this, the allowlist would grow monotonically and quietly exempt
    regressions -- an allowlist that only accretes is no allowlist at all.
    """
    _write_tree(
        tmp_path,
        {
            "port.py": """\
                class Widget:
                    def frob(self) -> None: ...
                """,
            "app.py": """\
                def use(widget):
                    return widget.frob()
                """,
        },
    )
    targets = [Target("frob", "method", "port.py")]
    allowlist = {"frob": "T999: kept while frob awaited its caller"}
    problems = _reachability_problems(targets, _scan_tree(tmp_path), allowlist=allowlist)
    assert len(problems) == 1 and "STALE" in problems[0] and "frob" in problems[0], problems


def test_negative_control_an_allowlist_entry_naming_no_target_is_flagged() -> None:
    """A typo'd or orphaned entry is an exemption pointing at nothing."""
    problems = _reachability_problems([], {}, allowlist={"gone_symbol": "T999: was real once"})
    assert len(problems) == 1 and "gone_symbol" in problems[0], problems


@pytest.mark.parametrize(
    "justification",
    ["", "   ", "seems fine to leave this one"],
    ids=["empty", "whitespace", "prose-without-citation"],
)
def test_negative_control_an_unjustified_allowlist_entry_is_rejected(
    justification: str,
) -> None:
    """The brief's own rule, proven falsifiable: an entry with no justification --
    or prose citing neither a task ID nor the UNRESOLVED marker -- fails on
    its own, before anyone reads a diff.
    """
    assert _allowlist_problems({"frob": justification}) != []


def test_negative_control_properly_cited_justifications_are_accepted() -> None:
    """The positive twin, so the rejection above is shown to be selective."""
    assert _allowlist_problems({"frob": "T232: uncalled by design (see ledger)"}) == []
    assert (
        _allowlist_problems({"frob": "UNRESOLVED - hypervisor review: found 2026-09-20"}) == []
    )
