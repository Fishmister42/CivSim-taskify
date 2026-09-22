"""Gate inputs: a Principle I gate's evidence must actually be supplied in production.

Sibling to ``tests/contract/test_reachability.py`` -- same idiom (AST scan, roster
and allowlist as data, negative controls that prove the checker can fire), aimed at
the defect family that roster does *not* see.

**The family.** This repo's defining shape is "a correct thing that is not wired to
production, with nothing red to show for it". Reachability catches the version where
the symbol has *no* caller at all. Two variants slip past it, and between them they
account for the most serious findings of 2026-09-21/22:

1. **A safety gate whose evidence is never gathered.** ``observe/capture.py``'s
   ``capture_for_step`` accepts ``detected_text_tokens`` and ``expected_process``;
   the one production call site (``run/decision_loop.py``) passes neither. The
   symbol is reached -- the *decision* is vacuous. Every screening technique that
   depends on text tokens is dead in production while unit tests supply tokens and
   pass, and the source gate's process-identity check never runs. Worse, a test
   named ``test_source_gate_skips_process_check_when_none_supplied`` asserts a
   *clean* result for exactly the production shape, which encodes the defect as
   intended behaviour.
2. **A helper with no production caller while a naive reimplementation sits in the
   call path.** ``act/prompts.py::prompt_screen_for_declaration_id`` correctly
   honours a documented exception (``prompts.ai_diplomatic_approach`` answers the
   screen ``prompt.diplomatic_approach``) and had zero callers in ``src/``, while
   ``act/executor.py`` derived the same key inline with a prefix swap that is right
   for twelve of thirteen prompt declarations. The thirteenth wedged a whole board:
   the wrong key was rejected by the Lua guard, the prompt stayed unanswerable,
   ``game.has_blocking_prompt`` stayed true, ``turn.end_turn`` was refused, and no
   run on that board could advance a turn at all. Green suite throughout.

**WHAT THIS FILE DOES NOT DO, and why -- read this before widening it.** The
obvious generalisation is "flag every parameter with an empty default that tests
supply and production omits". That was built and measured read-only over
``src/civsim_harness`` + ``tests`` in three formulations, and **none of them caught
the ``capture_for_step`` shape above**:

- naive signature rule: 24 hits, the real defect absent. 9 of the 24 were
  ``build_runner_dependencies(provider=None, save_loader=None, run_lock=None, ...)``
  and 6 more ``run_doctor(env=None, host_info=None, ...)`` -- correct dependency
  injection, where tests override and production uses the real default.
- plus the defining-module exclusion ``test_reachability.py`` already encodes:
  32 hits, still absent.
- chain-aware, collapsing same-name forwarding chains: 43 hits, 20 of them
  multi-hop, still absent, and now dominated by legitimate telemetry threading
  (``turn_number=None`` / ``step_index=None`` through the resilience checks).

It misses **structurally**, not for want of tuning. The defect is not "a parameter
nobody passes" -- plenty of never-passed empty defaults are good design. It is *"a
parameter whose empty default makes a withhold decision silently vacuous"*, and
that signal is not in the signature. The shape also spans three hops, with tests
entering at hop 3 (``CaptureAttempt``) while production enters at hop 1
(``capture_for_step``): at hop 1 the "tests supply it" clause is false, and at hop 3
the "production supplies it" clause is satisfied by ``capture.py``'s own internal
forwarding. A signature-keyed check cannot see it from either end.

Shipping ~40 findings of which ~35 are legitimate seams would get allowlisted down
to nothing and become a check nobody reads -- a check that cannot fail in the way
that matters, which is this project's defining defect wearing the costume of the fix
for it. So this file is deliberately **narrow and semantic**: a hand-curated roster,
recorded as data, of the specific parameters that feed a withhold/reject decision,
and of the specific helpers whose whole value is a documented exception someone
could otherwise reimplement naively. Adding to either roster is a human judgement
about *meaning*, which is exactly the part no syntax scan supplies.

**Honest limits.** Matching is by name, not resolved type, so a same-named call on
an unrelated object can satisfy a target -- the same residual the reachability scan
documents, and it errs only toward a false PASS on a name something else genuinely
uses. A call site that forwards ``**kwargs`` is reported as unprovable rather than
assumed good. ``src/`` vs ``tests/`` is the production/test partition; spike and
demo callers never count. And this file asserts only that the evidence is *passed*,
not that it is *correct*: whether the tokens the gate receives are the right tokens
is the screening suite's question, not this one's.
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
# The checker. One implementation, driven by both the real tree and the
# synthetic negative-control trees, so the controls exercise the enforcement.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CallSite:
    """One call of some function, and what it actually handed over."""

    module: str
    lineno: int
    keywords: frozenset[str]
    empty_literal_keywords: frozenset[str]
    has_kwargs_splat: bool


#: Argument forms that hand a gate nothing at all. A gate given one of these has not
#: been given evidence -- it has been given the absence of evidence in a shape that
#: type-checks, which is the whole mechanism of the defect this file guards.
_EMPTY_CALLS = frozenset({"frozenset", "set", "dict", "list", "tuple"})


def _is_empty_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is None or node.value == "" or node.value == b""
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.Call) and not node.args and not node.keywords:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        return name in _EMPTY_CALLS
    return False


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _scan_call_sites(root: Path) -> dict[str, list[CallSite]]:
    """Every call in the tree under *root*, bucketed by the called name."""
    sites: dict[str, list[CallSite]] = {}
    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name is None:
                continue
            supplied = {kw.arg for kw in node.keywords if kw.arg is not None}
            empty = {
                kw.arg
                for kw in node.keywords
                if kw.arg is not None and _is_empty_literal(kw.value)
            }
            sites.setdefault(name, []).append(
                CallSite(
                    module=module,
                    lineno=node.lineno,
                    keywords=frozenset(supplied),
                    empty_literal_keywords=frozenset(empty),
                    has_kwargs_splat=any(kw.arg is None for kw in node.keywords),
                )
            )
    return sites


def _referencing_modules(root: Path) -> dict[str, frozenset[str]]:
    """Which modules load or call each name. Imports and ``__all__`` do not count.

    Same rule, and the same reason, as ``test_reachability.py``: an
    ``ast.ImportFrom`` produces no ``Name`` load and an ``__all__`` entry is a
    string constant, so a re-export cannot vouch for a helper being used.
    """
    by_name: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name is not None:
                    by_name.setdefault(name, set()).add(module)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                by_name.setdefault(node.id, set()).add(module)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                by_name.setdefault(node.attr, set()).add(module)
    return {name: frozenset(modules) for name, modules in by_name.items()}


# --------------------------------------------------------------------------
# ARM 1 -- gate inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GateInput:
    """One parameter whose absence makes a Principle I decision vacuous.

    ``entry_point`` is deliberately the **outermost** production function, not the
    innermost one that reads the value: the defect hides in the gap between where
    production enters the chain and where tests enter it, and only the outer end is
    on production's own path. ``decides`` and ``vacuous_means`` are required prose,
    so a future reader can tell a real gate input from an ordinary optional seam
    without re-deriving the judgement that put it here.
    """

    entry_point: str
    parameter: str
    defined_in: str
    decides: str
    vacuous_means: str


def _gate_input_problems(
    gate_inputs: list[GateInput],
    call_sites: dict[str, list[CallSite]],
    allowlist: dict[str, str],
) -> list[str]:
    """Every way "this gate is fed in production" can be false, as one flat list.

    Four failure shapes, so the allowlist can only ratchet down: a production call
    site that omits the evidence, one that passes a literal empty (the same thing
    said out loud), an entry point with *no* production call site at all (a gate
    nothing runs cannot be certifying anything), and a stale or unknown allowlist
    entry.
    """
    problems: list[str] = []
    keys = {f"{gate.entry_point}.{gate.parameter}" for gate in gate_inputs}
    for gate in gate_inputs:
        key = f"{gate.entry_point}.{gate.parameter}"
        production = [
            site
            for site in call_sites.get(gate.entry_point, [])
            if site.module != gate.defined_in
        ]
        findings: list[str] = []
        if not production:
            findings.append(
                f"NO PRODUCTION CALL SITE: {gate.entry_point}() is never called outside "
                f"{gate.defined_in}, so the gate it feeds ({gate.decides}) runs nowhere"
            )
        for site in production:
            where = f"{site.module}:{site.lineno}"
            if gate.parameter in site.empty_literal_keywords:
                findings.append(
                    f"EMPTY EVIDENCE: {where} calls {gate.entry_point}("
                    f"{gate.parameter}=<empty>) -- {gate.vacuous_means}"
                )
            elif gate.parameter not in site.keywords:
                if site.has_kwargs_splat:
                    findings.append(
                        f"UNPROVABLE: {where} calls {gate.entry_point}(**kwargs); this "
                        f"check cannot tell whether {gate.parameter} is in there -- pass "
                        "it explicitly, or allowlist with a reason"
                    )
                else:
                    findings.append(
                        f"UNFED GATE: {where} calls {gate.entry_point}() without "
                        f"{gate.parameter!r}, so {gate.decides} decides on no evidence -- "
                        f"{gate.vacuous_means}"
                    )
        if findings and key not in allowlist:
            problems.extend(findings)
        elif not findings and key in allowlist:
            problems.append(
                f"STALE ALLOWLIST ENTRY: {key!r} is now supplied at every production call "
                "site; delete its allowlist entry so the check guards it from here on"
            )
    for key in allowlist:
        if key not in keys:
            problems.append(f"UNKNOWN ALLOWLIST ENTRY: {key!r} names no gate input in the roster")
    return problems


#: The roster. Every entry is a judgement that a vacuous value here is a *silent
#: pass through a safety gate*, not merely an unused option.
GATE_INPUTS: list[GateInput] = [
    GateInput(
        entry_point="capture_for_step",
        parameter="detected_text_tokens",
        defined_in="observe/capture.py",
        decides="the content gate's declared-text technique",
        vacuous_means=(
            "every reject category whose only available technique is DECLARED_TEXT is "
            "screened against no text at all, and 'checked and clean' becomes "
            "indistinguishable from 'never checked' (C0, 2026-09-22)"
        ),
    ),
    GateInput(
        entry_point="capture_for_step",
        parameter="expected_process",
        defined_in="observe/capture.py",
        decides="the source gate's process-identity check",
        vacuous_means=(
            "the frame is never proven to have come from the client process this run is "
            "attached to; a frame from any other window passes the source gate (C0b, "
            "2026-09-22)"
        ),
    ),
]


#: Each entry says why the gap is tolerated *and for how long*. Empty or uncited
#: justifications fail on their own (see ``_allowlist_problems``).
GATE_INPUT_ALLOWLIST: dict[str, str] = {
    "capture_for_step.detected_text_tokens": (
        "UNRESOLVED - hypervisor review: C0, found 2026-09-22. HALF DONE as of a395325: "
        "the gate now fails CLOSED on this gap -- an absent value (None, and None is the "
        "default) means the declared-text technique did not run, so every category it is "
        "the only technique for is withheld rather than passed. Nothing escapes any more, "
        "but nothing is delivered either: image delivery is closed on all platforms until "
        "the tokens exist. Producing them needs desktop-wide window-title enumeration, "
        "which no HostPlatform port method exposes -- adding it means editing host/port.py "
        "and host/<platform>/adapter.py, another lane's files. STALE -- delete it -- the "
        "moment run/decision_loop.py passes the tokens."
    ),
    "capture_for_step.expected_process": (
        "UNRESOLVED - hypervisor review: C0b, found 2026-09-22. RESOLVED IN SUBSTANCE as "
        "of a395325, but kept because this check reads the CALL SITE and the call site has "
        "not changed. The source gate now withholds when no process is supplied (it was "
        "skipping the check), and observe/capture.py resolves one itself via "
        "host.locate_game_process() rather than omitting it, so the identity check does "
        "run in production. What remains is that run/decision_loop.py -- which already "
        "holds the run's process -- still passes nothing, leaving a /proc scan per capture "
        "where a hand-off would do. Superseded note: the assertion this entry cited in "
        "tests/unit/test_image_screening.py is gone (test_source_gate_skips_process_check_"
        "when_none_supplied is rewritten as ..._withholds_when_no_located_process_is_"
        "supplied). STALE -- delete it -- the moment the loop passes the process."
    ),
}


# --------------------------------------------------------------------------
# ARM 2 -- derivation helpers with a naive twin in the call path
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivationHelper:
    """A public helper whose entire value is a documented exception to a simple rule.

    This is the sub-family of "unreached symbol" where being unreached is *worse*
    than dead code: the rule is simple enough that a caller will cheerfully
    reimplement it inline and be right almost every time, so the helper's absence
    from the call path shows up not as a crash but as one wrong answer in thirteen.
    ``naive_twin`` records the shortcut it exists to beat, so a reviewer can tell at
    a glance what re-derivation looks like.
    """

    name: str
    defined_in: str
    exception_it_encodes: str
    naive_twin: str
    cost_when_bypassed: str
    #: The module-level entry point through which this helper is legitimately reached
    #: from inside its own module, when it has no direct external caller. Both halves
    #: are then required: the helper must actually be *used* inside its module (not
    #: merely defined there), and the entry point must itself have a production caller
    #: outside the module. This is the ``route_prompt`` case -- the inverse mapping is
    #: called only from ``route_prompt``, which ``run/decision_loop.py`` drives -- and
    #: it is stated per helper, as data, rather than relaxed globally, because "a
    #: module calling its own deliverable" is otherwise self-confirmation, which is
    #: precisely what ``test_reachability.py``'s defining-module exclusion exists to
    #: refuse.
    reached_via: str | None = None


def _derivation_helper_problems(
    helpers: list[DerivationHelper],
    refs_by_name: dict[str, frozenset[str]],
    allowlist: dict[str, str],
) -> list[str]:
    problems: list[str] = []
    names = {helper.name for helper in helpers}
    for helper in helpers:
        refs = refs_by_name.get(helper.name, frozenset())
        callers = {module for module in refs if module != helper.defined_in}
        if not callers and helper.reached_via is not None:
            entry_callers = {
                module
                for module in refs_by_name.get(helper.reached_via, frozenset())
                if module != helper.defined_in
            }
            if helper.defined_in in refs and entry_callers:
                callers = {f"{helper.defined_in} (via {helper.reached_via})"}
        if not callers and helper.name not in allowlist:
            problems.append(
                f"UNWIRED DERIVATION HELPER: {helper.name!r} (defined in "
                f"{helper.defined_in}) has no production caller outside its own module. "
                f"It encodes {helper.exception_it_encodes}; without it a caller derives "
                f"the same thing as {helper.naive_twin}, which costs "
                f"{helper.cost_when_bypassed}"
            )
        elif callers and helper.name in allowlist:
            problems.append(
                f"STALE ALLOWLIST ENTRY: {helper.name!r} now has a production caller "
                f"({', '.join(sorted(callers))}); delete its allowlist entry"
            )
    for name in allowlist:
        if name not in names:
            problems.append(f"UNKNOWN ALLOWLIST ENTRY: {name!r} names no helper in the roster")
    return problems


DERIVATION_HELPERS: list[DerivationHelper] = [
    DerivationHelper(
        name="prompt_screen_for_declaration_id",
        defined_in="act/prompts.py",
        exception_it_encodes=(
            "PROMPT_ACTION_BY_SCREEN's documented action/screen mismatches -- the catalog "
            "names an action for what the human does and the screen for what is on screen, "
            "so prompts.ai_diplomatic_approach answers prompt.diplomatic_approach"
        ),
        naive_twin='removeprefix("prompts.") re-prefixed with "prompt."',
        cost_when_bypassed=(
            "a whole game board: the wrong key is rejected by the Lua guard, the prompt "
            "stays unanswerable, game.has_blocking_prompt stays true, turn.end_turn is "
            "refused, and no run on that board can advance a turn (2026-09-22, live)"
        ),
    ),
    DerivationHelper(
        name="prompt_declaration_id_for_screen",
        defined_in="act/prompts.py",
        exception_it_encodes="the same table read in the screen -> action direction",
        naive_twin='removeprefix("prompt.") re-prefixed with "prompts."',
        cost_when_bypassed="the mirror image of the above, on the routing side",
        # This direction never went wrong, and the reason is worth recording: it has
        # exactly one caller, `route_prompt`, in its own module, and `route_prompt` is
        # the only way `run/decision_loop.py` reaches prompt routing at all -- so there
        # was no second place for anyone to re-derive it. The outbound direction had no
        # such funnel, and that is where the naive twin grew.
        reached_via="route_prompt",
    ),
]

DERIVATION_HELPER_ALLOWLIST: dict[str, str] = {}


# --------------------------------------------------------------------------
# Shared: an allowlist entry is only as good as its citation (same rule, and
# the same reasoning, as test_reachability.py's).
# --------------------------------------------------------------------------

_UNRESOLVED_MARKER = "UNRESOLVED - hypervisor review"
_TASK_CITATION = re.compile(r"\bT\d{3}\b")
_FINDING_CITATION = re.compile(r"\bC\d+[a-z]?\b")


def _allowlist_problems(allowlist: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for name, justification in allowlist.items():
        if not justification.strip():
            problems.append(f"{name!r}: empty justification -- every entry must say why")
        elif (
            _UNRESOLVED_MARKER not in justification
            and not _TASK_CITATION.search(justification)
            and not _FINDING_CITATION.search(justification)
        ):
            problems.append(
                f"{name!r}: justification cites no task ID, no finding ID and carries no "
                f"{_UNRESOLVED_MARKER!r} marker: {justification!r}"
            )
    return problems


# --------------------------------------------------------------------------
# Enforcement against the real tree.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def harness_call_sites() -> dict[str, list[CallSite]]:
    return _scan_call_sites(_HARNESS_ROOT)


@pytest.fixture(scope="module")
def harness_refs() -> dict[str, frozenset[str]]:
    return _referencing_modules(_HARNESS_ROOT)


def test_the_harness_tree_is_being_scanned(harness_refs: dict[str, frozenset[str]]) -> None:
    """Guard against this suite passing vacuously over an empty or moved tree."""
    modules = {module for modules in harness_refs.values() for module in modules}
    assert len(modules) >= 50, (
        f"only {len(modules)} modules found under {_HARNESS_ROOT} -- the scan root is "
        "wrong or the package moved, and every verdict below is vacuous"
    )


def test_the_roster_names_functions_that_actually_exist_with_those_parameters() -> None:
    """The checker-that-cannot-fail problem, one level down: a roster entry naming a
    renamed function or a renamed parameter would silently guard nothing. Pin both
    against the real signatures, from the AST, so a rename is a failure here.
    """
    for gate in GATE_INPUTS:
        path = _HARNESS_ROOT / gate.defined_in
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert gate.entry_point in functions, (
            f"{gate.entry_point!r} is not defined in {gate.defined_in} any more -- "
            "the roster entry guards nothing until it is updated"
        )
        args = functions[gate.entry_point].args
        parameters = {
            arg.arg for arg in (*args.args, *args.posonlyargs, *args.kwonlyargs)
        }
        assert gate.parameter in parameters, (
            f"{gate.entry_point}() no longer takes {gate.parameter!r}: {sorted(parameters)}"
        )

    for helper in DERIVATION_HELPERS:
        path = _HARNESS_ROOT / helper.defined_in
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert helper.name in defined, f"{helper.name!r} is gone from {helper.defined_in}"


def test_every_gate_input_is_supplied_on_the_production_path(
    harness_call_sites: dict[str, list[CallSite]],
) -> None:
    """Arm 1, over the real tree.

    A failure here means a Principle I gate is deciding on evidence production never
    gathered. Fix it by passing the evidence, never by widening the gate's default or
    by deleting the roster entry: an allowlist entry with a cited reason is the one
    legitimate way to defer, and it fails as STALE the moment the wiring lands.
    """
    problems = _gate_input_problems(GATE_INPUTS, harness_call_sites, GATE_INPUT_ALLOWLIST)
    assert problems == [], "\n".join(problems)


def test_every_derivation_helper_has_a_production_caller(
    harness_refs: dict[str, frozenset[str]],
) -> None:
    """Arm 2, over the real tree: the correct derivation is the one production uses."""
    problems = _derivation_helper_problems(
        DERIVATION_HELPERS, harness_refs, DERIVATION_HELPER_ALLOWLIST
    )
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("name", sorted(GATE_INPUT_ALLOWLIST))
def test_every_gate_input_allowlist_entry_carries_a_cited_justification(name: str) -> None:
    assert _allowlist_problems({name: GATE_INPUT_ALLOWLIST[name]}) == []


@pytest.mark.parametrize("name", sorted(DERIVATION_HELPER_ALLOWLIST))
def test_every_helper_allowlist_entry_carries_a_cited_justification(name: str) -> None:
    assert _allowlist_problems({name: DERIVATION_HELPER_ALLOWLIST[name]}) == []


# --------------------------------------------------------------------------
# Negative controls. Written BEFORE the roster above, against a synthetic tree
# reproducing the real capture_for_step shape hop for hop -- if the checker
# could not flag that, it had no business shipping. Every control drives the
# same functions the enforcement above runs.
# --------------------------------------------------------------------------


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")


#: The real shape, reduced: a three-hop chain where production enters at hop 1 and
#: omits the evidence, the defining module forwards it internally (so hop 3 *is*
#: "supplied" from inside), and the test tier enters at hop 3 with tokens in hand
#: and passes. This is precisely the configuration the broad signature scan could
#: not see from either end.
_CAPTURE_SHAPE = {
    "observe/capture.py": """\
        def _attempt_once(*, window, detected_text_tokens=None):
            return screen_capture(window=window, detected_text_tokens=detected_text_tokens)


        def capture_for_step(*, window, detected_text_tokens=None):
            return _attempt_once(window=window, detected_text_tokens=detected_text_tokens)
        """,
    "run/decision_loop.py": """\
        from observe.capture import capture_for_step


        def run_step(ctx):
            return capture_for_step(window=ctx.window)
        """,
}


def test_negative_control_the_capture_for_step_shape_is_flagged(tmp_path: Path) -> None:
    """THE control this checker had to pass before it could ship.

    Production calls the entry point without the gate's evidence while the module
    forwards it internally; the checker must name the production call site. If this
    ever goes green with the roster entry in place, the check has stopped working.
    """
    _write_tree(tmp_path, _CAPTURE_SHAPE)
    gate = GateInput(
        entry_point="capture_for_step",
        parameter="detected_text_tokens",
        defined_in="observe/capture.py",
        decides="the content gate's declared-text technique",
        vacuous_means="unscreened frames read as clean",
    )
    problems = _gate_input_problems([gate], _scan_call_sites(tmp_path), allowlist={})

    assert len(problems) == 1, problems
    assert "UNFED GATE" in problems[0], problems
    assert "run/decision_loop.py:5" in problems[0], problems


def test_negative_control_the_internal_forwarding_hop_does_not_excuse_the_entry_point(
    tmp_path: Path,
) -> None:
    """The defining module's own forwarding is not production wiring.

    ``capture.py`` really does pass ``detected_text_tokens`` to ``_attempt_once`` --
    that is what made the innermost hop look fed. Pointing the checker at the inner
    hop must therefore report NO PRODUCTION CALL SITE rather than a pass, because the
    only caller is the defining module itself.
    """
    _write_tree(tmp_path, _CAPTURE_SHAPE)
    inner = GateInput(
        entry_point="_attempt_once",
        parameter="detected_text_tokens",
        defined_in="observe/capture.py",
        decides="the content gate's declared-text technique",
        vacuous_means="unscreened frames read as clean",
    )
    problems = _gate_input_problems([inner], _scan_call_sites(tmp_path), allowlist={})

    assert len(problems) == 1 and "NO PRODUCTION CALL SITE" in problems[0], problems


def test_negative_control_a_supplied_gate_input_passes(tmp_path: Path) -> None:
    """The positive twin, so the flag above is shown to be selective rather than
    constant -- the same tree with the one keyword added must come back clean.
    """
    _write_tree(
        tmp_path,
        {
            **_CAPTURE_SHAPE,
            "run/decision_loop.py": """\
                from observe.capture import capture_for_step


                def run_step(ctx):
                    return capture_for_step(
                        window=ctx.window, detected_text_tokens=ctx.read_window_titles()
                    )
                """,
        },
    )
    gate = GateInput(
        entry_point="capture_for_step",
        parameter="detected_text_tokens",
        defined_in="observe/capture.py",
        decides="the content gate's declared-text technique",
        vacuous_means="unscreened frames read as clean",
    )
    assert _gate_input_problems([gate], _scan_call_sites(tmp_path), allowlist={}) == []


@pytest.mark.parametrize(
    "literal",
    ["None", "frozenset()", "set()", "()", "[]", "{}", '""'],
    ids=["none", "frozenset", "set", "tuple", "list", "dict", "empty-str"],
)
def test_negative_control_passing_a_literal_empty_is_not_supplying_evidence(
    tmp_path: Path, literal: str
) -> None:
    """Saying it out loud is not better than leaving it out. Without this, the fix for
    a flagged call site would be to type ``detected_text_tokens=frozenset()`` and move
    on -- a gate fed the absence of evidence, now with the checker's blessing.
    """
    _write_tree(
        tmp_path,
        {
            **_CAPTURE_SHAPE,
            "run/decision_loop.py": f"""\
                from observe.capture import capture_for_step


                def run_step(ctx):
                    return capture_for_step(window=ctx.window, detected_text_tokens={literal})
                """,
        },
    )
    gate = GateInput(
        entry_point="capture_for_step",
        parameter="detected_text_tokens",
        defined_in="observe/capture.py",
        decides="the content gate's declared-text technique",
        vacuous_means="unscreened frames read as clean",
    )
    problems = _gate_input_problems([gate], _scan_call_sites(tmp_path), allowlist={})
    assert len(problems) == 1 and "EMPTY EVIDENCE" in problems[0], problems


def test_negative_control_a_kwargs_splat_is_reported_as_unprovable_not_as_a_pass(
    tmp_path: Path,
) -> None:
    """The one shape this scan genuinely cannot decide. It must say so rather than
    guess -- a silent pass here would be the exact failure mode the module docstring
    calls "a check that cannot fail in the way that matters".
    """
    _write_tree(
        tmp_path,
        {
            **_CAPTURE_SHAPE,
            "run/decision_loop.py": """\
                from observe.capture import capture_for_step


                def run_step(ctx, **extra):
                    return capture_for_step(window=ctx.window, **extra)
                """,
        },
    )
    gate = GateInput(
        entry_point="capture_for_step",
        parameter="detected_text_tokens",
        defined_in="observe/capture.py",
        decides="the content gate's declared-text technique",
        vacuous_means="unscreened frames read as clean",
    )
    problems = _gate_input_problems([gate], _scan_call_sites(tmp_path), allowlist={})
    assert len(problems) == 1 and "UNPROVABLE" in problems[0], problems


def test_negative_control_a_stale_gate_input_allowlist_entry_is_flagged(tmp_path: Path) -> None:
    """The ratchet: once the evidence is threaded through, the exemption must go, or
    the allowlist accretes and the check quietly stops guarding what it named.
    """
    _write_tree(
        tmp_path,
        {
            **_CAPTURE_SHAPE,
            "run/decision_loop.py": """\
                from observe.capture import capture_for_step


                def run_step(ctx):
                    return capture_for_step(window=ctx.window, detected_text_tokens=ctx.tokens)
                """,
        },
    )
    gate = GateInput(
        entry_point="capture_for_step",
        parameter="detected_text_tokens",
        defined_in="observe/capture.py",
        decides="the content gate's declared-text technique",
        vacuous_means="unscreened frames read as clean",
    )
    problems = _gate_input_problems(
        [gate],
        _scan_call_sites(tmp_path),
        allowlist={"capture_for_step.detected_text_tokens": "C0: awaiting the fix"},
    )
    assert len(problems) == 1 and "STALE" in problems[0], problems


def test_negative_control_a_gate_input_allowlist_entry_naming_nothing_is_flagged() -> None:
    assert _gate_input_problems([], {}, allowlist={"gone.param": "C0: was real once"}) != []


def test_negative_control_an_unwired_derivation_helper_is_flagged(tmp_path: Path) -> None:
    """Arm 2's control, reproducing the ``prompt_screen_for_declaration_id`` shape: the
    correct helper exists and is exercised only by its own module, while the caller
    inlines the naive prefix swap that is right twelve times in thirteen.
    """
    _write_tree(
        tmp_path,
        {
            "act/prompts.py": """\
                PROMPT_ACTION_BY_SCREEN = {"prompt.diplomatic_approach": "prompts.ai_approach"}


                def prompt_screen_for_declaration_id(declaration_id):
                    for screen, action in PROMPT_ACTION_BY_SCREEN.items():
                        if action == declaration_id:
                            return screen
                    return "prompt." + declaration_id.removeprefix("prompts.")
                """,
            "act/executor.py": """\
                def screen_key(declaration_id):
                    return "prompt." + declaration_id.removeprefix("prompts.")
                """,
        },
    )
    helper = DerivationHelper(
        name="prompt_screen_for_declaration_id",
        defined_in="act/prompts.py",
        exception_it_encodes="the documented action/screen mismatch",
        naive_twin="a prefix swap",
        cost_when_bypassed="an unanswerable prompt that wedges the board",
    )
    problems = _derivation_helper_problems([helper], _referencing_modules(tmp_path), allowlist={})
    assert len(problems) == 1 and "UNWIRED DERIVATION HELPER" in problems[0], problems


def test_negative_control_an_import_or_reexport_alone_does_not_wire_a_helper(
    tmp_path: Path,
) -> None:
    """Importing the right function and then not calling it is the defect, not the fix.

    ``act/executor.py`` could plausibly have imported the helper for a type hint while
    still deriving the key inline; if an import counted, this arm would pass on the
    exact tree it was written against.
    """
    _write_tree(
        tmp_path,
        {
            "act/prompts.py": """\
                def prompt_screen_for_declaration_id(declaration_id):
                    return declaration_id
                """,
            "act/executor.py": """\
                from act.prompts import prompt_screen_for_declaration_id

                __all__ = ["prompt_screen_for_declaration_id"]


                def screen_key(declaration_id):
                    return "prompt." + declaration_id.removeprefix("prompts.")
                """,
        },
    )
    helper = DerivationHelper(
        name="prompt_screen_for_declaration_id",
        defined_in="act/prompts.py",
        exception_it_encodes="the documented action/screen mismatch",
        naive_twin="a prefix swap",
        cost_when_bypassed="an unanswerable prompt that wedges the board",
    )
    problems = _derivation_helper_problems([helper], _referencing_modules(tmp_path), allowlist={})
    assert len(problems) == 1 and "UNWIRED DERIVATION HELPER" in problems[0], problems


def test_negative_control_a_wired_derivation_helper_passes(tmp_path: Path) -> None:
    """The positive twin -- the tree as it stands after the live lane's fix landed."""
    _write_tree(
        tmp_path,
        {
            "act/prompts.py": """\
                def prompt_screen_for_declaration_id(declaration_id):
                    return declaration_id
                """,
            "act/executor.py": """\
                from act.prompts import prompt_screen_for_declaration_id


                def screen_key(declaration_id):
                    return prompt_screen_for_declaration_id(declaration_id)
                """,
        },
    )
    helper = DerivationHelper(
        name="prompt_screen_for_declaration_id",
        defined_in="act/prompts.py",
        exception_it_encodes="the documented action/screen mismatch",
        naive_twin="a prefix swap",
        cost_when_bypassed="an unanswerable prompt that wedges the board",
    )
    assert _derivation_helper_problems([helper], _referencing_modules(tmp_path), allowlist={}) == []


def test_negative_control_reached_via_requires_both_halves(tmp_path: Path) -> None:
    """``reached_via`` must not become a blanket excuse for a same-module caller.

    Three trees, one rule: the helper counts as wired only when it is genuinely used
    inside its module AND that module's named entry point is itself driven from
    outside. Take away either half and the finding comes back -- otherwise
    ``reached_via`` would re-admit exactly the self-confirmation the defining-module
    exclusion refuses.
    """
    helper = DerivationHelper(
        name="derive",
        defined_in="act/prompts.py",
        exception_it_encodes="an exception",
        naive_twin="a shortcut",
        cost_when_bypassed="a wedged board",
        reached_via="route",
    )

    wired = tmp_path / "wired"
    _write_tree(
        wired,
        {
            "act/prompts.py": """\
                def derive(screen):
                    return screen


                def route(screen):
                    return derive(screen)
                """,
            "run/loop.py": """\
                from act.prompts import route


                def step(screen):
                    return route(screen)
                """,
        },
    )
    assert _derivation_helper_problems([helper], _referencing_modules(wired), allowlist={}) == []

    # Half one missing: the entry point exists but nothing outside the module drives it.
    orphan_entry = tmp_path / "orphan_entry"
    _write_tree(
        orphan_entry,
        {
            "act/prompts.py": """\
                def derive(screen):
                    return screen


                def route(screen):
                    return derive(screen)
                """,
            "run/loop.py": """\
                def step(screen):
                    return "prompt." + screen
                """,
        },
    )
    problems = _derivation_helper_problems([helper], _referencing_modules(orphan_entry), {})
    assert len(problems) == 1 and "UNWIRED" in problems[0], problems

    # Half two missing: the entry point is driven, but it does not use the helper --
    # the real defect, wearing the `reached_via` declaration as camouflage.
    bypassed = tmp_path / "bypassed"
    _write_tree(
        bypassed,
        {
            "act/prompts.py": """\
                def derive(screen):
                    return screen


                def route(screen):
                    return "prompt." + screen
                """,
            "run/loop.py": """\
                from act.prompts import route


                def step(screen):
                    return route(screen)
                """,
        },
    )
    problems = _derivation_helper_problems([helper], _referencing_modules(bypassed), {})
    assert len(problems) == 1 and "UNWIRED" in problems[0], problems


def test_negative_control_a_stale_helper_allowlist_entry_is_flagged(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        {
            "act/prompts.py": """\
                def helper(x):
                    return x
                """,
            "act/executor.py": """\
                from act.prompts import helper


                def use(x):
                    return helper(x)
                """,
        },
    )
    target = DerivationHelper(
        name="helper",
        defined_in="act/prompts.py",
        exception_it_encodes="an exception",
        naive_twin="a shortcut",
        cost_when_bypassed="a wedged board",
    )
    problems = _derivation_helper_problems(
        [target], _referencing_modules(tmp_path), allowlist={"helper": "T999: awaiting wiring"}
    )
    assert len(problems) == 1 and "STALE" in problems[0], problems


@pytest.mark.parametrize(
    "justification",
    ["", "   ", "looks fine, leaving it"],
    ids=["empty", "whitespace", "prose-without-citation"],
)
def test_negative_control_an_unjustified_allowlist_entry_is_rejected(justification: str) -> None:
    assert _allowlist_problems({"whatever": justification}) != []


def test_negative_control_properly_cited_justifications_are_accepted() -> None:
    """The positive twin, so the rejection above is shown to be selective."""
    assert _allowlist_problems({"a": "T232: uncalled by design (see ledger)"}) == []
    assert _allowlist_problems({"b": "C0: the screening lane owns the fix"}) == []
    assert _allowlist_problems({"c": "UNRESOLVED - hypervisor review: found 2026-09-22"}) == []
