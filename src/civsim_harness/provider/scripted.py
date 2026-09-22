"""A ``ModelProvider`` that executes a fixed, declared action sequence (T326).

**What this is for, and what it is emphatically not for.** Some actions are only reachable
through a *chain*: nothing in Civilization VI offers ``cities.set_production`` until
``cities.select`` has landed, nothing offers a unit order until a unit exists, and nothing
produces a unit until production has been set. Under a sampling provider the chain's first link
may simply never be drawn -- MEASURED on the live board, 2026-09-22: ``cities.select`` was drawn
**0 times in 133 world-screen steps** under the coverage policy, so the board sat at one city and
zero units at game turn 65 and five of thirteen goals were unreachable by any available means.
An entire region of the action catalog was therefore not merely undemonstrated but **untestable**.

This provider makes it testable. It is a **harness capability test**: it proves that a declared
action chain works end to end. It is not gameplay and it is not an agent.

**Why supplying the choice from outside is legitimate here, and why this reasoning is
load-bearing rather than decoration.**

- An operator clicking in the game client **bypasses the harness entirely**. Nothing is
  exercised, nothing is recorded, and the only thing proven is that the game works.
  A scripted provider **exercises all of it**: the action's declared
  ``availability_predicate``, argument normalisation, dispatch through the capability executor,
  the declared ``verification_predicate``'s re-read of the board, and the store write. Every
  mechanism a real decision would travel through is travelled.
- **The only thing supplied from outside is the choice.** The mechanism stays entirely ours,
  which is exactly what a capability test must measure. A test that also supplied the mechanism
  would be measuring the operator.
- **Principle I (human-parity, NON-NEGOTIABLE) is untouched.** A script grants no capability a
  human lacks: every step names a catalogued action, every action still passes its own
  availability predicate against the live board, and every dispatch still goes through the
  human-parity path. A declared step for an action the game is not offering is *refused*, loudly
  and on the record, exactly as an agent's own such choice would be.
- **Principle IV (seeded, reproducible experimentation) is served, not strained.** A declared
  sequence is strictly more reproducible than a seed: re-running the same script against the same
  save reproduces the same decisions without depending on a sampler's stream, a policy version,
  or what the board happened to render.

**THE GUARDRAIL, and it is not optional. A scripted landing proves the ACTION works and says
NOTHING about whether an agent would choose it.** Every call this adapter serves reports
``model_served = scripted/declared-sequence-v1``
(:data:`~civsim_harness.models.provenance.SCRIPTED_MODEL_REF`), which
``provider/accounting.py`` copies verbatim onto the step's ``ModelCall`` and the store persists.
``store/coverage.py`` routes on that, at the single place a step is read, into an accumulator the
chosen tier cannot reach -- see ``models/provenance.py`` for why the classification is derived
from a field that is required on every record rather than stored as a defaulted flag, and
``tests/unit/test_scripted_coverage_separation.py`` for the control that fails if a scripted
landing is ever counted as a chosen one.

**Absence and unobservability do not share a representation here.** A declared step that was
issued and refused is recorded by the harness like any other refusal, with the game's own reason.
A declared step that was never *reached* -- because an earlier one halted the script -- produces
no decision at all, so it is stated explicitly instead: the ledger
(:meth:`ScriptedModelProvider.ledger`) carries one row per declared step with an explicit
status, and every decision this provider returns after the script stops names, in its own
``reasoning`` and therefore durably in the store, which step it stopped at and how many steps
were not reached. "The script did nothing" and "the script never got there" are never the same
record.

**Unavailability is a declared policy, never a silent skip** (:class:`UnavailablePolicy`). When
the request shows the current step's action greyed out, this provider always issues it anyway,
so the harness's own dispatcher records the refusal with the game's own reason -- and then
applies the script's declared policy: ``halt`` stops, ``continue`` advances to the next step,
``retry`` re-issues the same step up to its declared ``attempts_per_step`` and then halts. There
is no fourth behaviour and no default: ``on_unavailable`` is a required key of every script.

**What this provider reads.** Only the ``DecisionRequest`` it was handed, exactly like
``provider/stochastic.py``: the rendered action catalog's two groups tell it whether the game is
offering the step's action right now. It consults no store, no catalog loader at *runtime*, and
no game. The catalog is read once, at **load** time, to validate the script.

**What it does not do.** It does not answer blocking prompts of its own accord, because that
would be a choice it was not given. A script that expects a prompt declares the answering action
as one of its own steps. A prompt nothing answers will refuse the current step; the declared
policy then decides, and the harness's own no-progress backstop ends the turn.

**THE REPLAY QUESTION, answered explicitly rather than left to the cursor's default.** The cursor
is monotonic across the whole run and never rewinds. If the harness abandons a turn attempt and
replays it, the steps already issued stay issued -- so a dependent step can be served after the
setup step it depends on went to a discarded attempt, and the chain the script exists to prove is
then split. A refusal in that situation is a **replay artefact, not a finding about the harness**,
and the two must not arrive as the same record.

*Halting on a replay was considered and rejected*, because the detector would be wrong in the
dangerous direction. The only signal a ``DecisionRequest`` carries is ``step_index``, and a
restart at 1 is a new **turn cycle**, which is a new turn far more often than it is a replay --
``run/decision_loop.py`` numbers each attempt from ``step_index_base + 1`` and increments
monotonically within it, sharing that numbering with the pre-save prompt-clearance pass rather
than restarting for it. The observed turn number does not disambiguate either: an end turn that
verified through ``is_waiting_for_other_players`` leaves it where it was. A halt on that signal
would stop healthy scripts **while looking exactly like a finding** -- the same confusion, merely
inverted, and the fail-safe direction that this project has repeatedly found survives longest
precisely because it never produces a visible incident.

*What is done instead, on both sides, so a reader can tell a scrambled run from a clean one.*

1. **The board is the authority on whether a precondition held.** A dependent step whose setup
   went to a discarded attempt is rendered by the game as greyed out, with its own reason -- so
   it is issued as ``issued_unavailable`` with that reason in its ledger row, never blindly as
   though the setup had landed. A setup step recorded ``issued_available`` followed by a
   dependent step recorded ``issued_unavailable`` IS a finding about the harness; a dependent
   step whose setup never ran in this sequence is not, and the two ledger rows differ.
2. **The ledger records what is observable and interprets nothing.** Each row carries the
   ``sequence_epoch`` (how many ``step_index`` restarts preceded it) and the ``step_index`` it
   was issued at. Two steps of one chain carrying different epochs is the signal that they may
   not have run against one continuous board.
3. **The store settles it.** ``store/coverage.py``'s scripted tier counts scripted steps recorded
   in attempts the store marks non-authoritative
   (``ScriptedCoverage.steps_in_abandoned_attempts``) and, when that is non-zero, prints an
   explicit caveat in both the Markdown and the JSON report. The store knows the attempt index
   and whether it was abandoned; this adapter does not, and does not guess.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

from civsim_harness.capability.loader import Catalog
from civsim_harness.errors import HarnessError
from civsim_harness.models.catalog import DeclarationKind, TargetKind
from civsim_harness.models.common import Cost, DeclarationId, ModelRef
from civsim_harness.models.provenance import (
    SCRIPTED_MODEL_NAME,
    SCRIPTED_MODEL_REF,
    SCRIPTED_PROVIDER_NAME,
)
from civsim_harness.models.records import CallOutcome
from civsim_harness.provider.port import (
    DecisionRequest,
    DecisionResponse,
    ModelCapabilities,
    RawDecision,
)
from civsim_harness.provider.stochastic import END_TURN_DECLARATION_ID

__all__ = [
    "SCRIPTED_MODEL_NAME",
    "SCRIPTED_MODEL_REF",
    "SCRIPTED_PROVIDER_NAME",
    "ActionScript",
    "LedgerRow",
    "ScriptStep",
    "ScriptStepStatus",
    "ScriptValidationError",
    "ScriptedModelProvider",
    "UnavailablePolicy",
    "load_action_script",
]

#: Provider-reported usage for a call that never left the process (P7). Identical in kind to
#: ``provider/stochastic.py``'s: not a placeholder and not an independent pricing of anything.
ZERO_COST: Final = Cost(input_tokens=0, output_tokens=0, total_tokens=0, amount_usd=0.0)

#: ``describe()``'s reported context window. This adapter performs no I/O, so there is nothing
#: for a context to overflow and no wire for an image to be dropped from.
_UNBOUNDED_CONTEXT_TOKENS: Final = 1_000_000_000

# --------------------------------------------------------------------------
# Reading the request -- the only input this module has at runtime
# --------------------------------------------------------------------------

#: The headers ``agent/context.py`` writes above the action list and its two groups. Copied as
#: text rather than imported for the reason ``provider/stochastic.py`` gives for the same three
#: constants: this module may read the request and nothing else at runtime, and
#: ``agent/context.py`` reaches the catalog loader. ``tests/unit/test_scripted_provider.py`` pins
#: all four spellings against ``agent.context``'s own, so a rename cannot silently stop the
#: grouping being seen -- which would otherwise make every step look unavailable.
_ACTION_SECTION_HEADER: Final = "Actions you may take"
_AVAILABLE_GROUP_HEADER: Final = "Available now"
_UNAVAILABLE_GROUP_HEADER: Final = "Not available now"
_UNAVAILABLE_REASON_MARKER: Final = " -- not available: "

#: One action line: ``- units.move_to: <summary> -- target: ...``.
_ACTION_LINE_RE: Final = re.compile(r"^-\s+(?P<id>[A-Za-z0-9_.]+):\s*(?P<rest>.*)$")

#: What the provider reports when the request did not list the action at all -- distinct from
#: "listed and greyed out with a reason", because the two are different facts about the board.
NOT_LISTED_REASON: Final = "this request did not list the action at all"


def _listed_actions(observation: str) -> dict[str, tuple[bool, str]]:
    """``declaration id -> (available now, the reason it is greyed out)`` from the rendered text.

    An action the request lists without any grouping is read as available, which is what "the
    request lists it" meant before the groups existed. Only bullets below the action-catalog
    header are read, so an observed-state line can never be mistaken for an action.
    """
    listed: dict[str, tuple[bool, str]] = {}
    in_section = False
    available = True
    for line in observation.splitlines():
        if not in_section:
            in_section = line.startswith(_ACTION_SECTION_HEADER)
            continue
        if line.startswith(_UNAVAILABLE_GROUP_HEADER):
            available = False
            continue
        if line.startswith(_AVAILABLE_GROUP_HEADER):
            available = True
            continue
        match = _ACTION_LINE_RE.match(line)
        if match is None:
            continue
        _, _, reason = match.group("rest").partition(_UNAVAILABLE_REASON_MARKER)
        listed[match.group("id")] = (available, reason.strip())
    return listed


# --------------------------------------------------------------------------
# The declared script
# --------------------------------------------------------------------------


class ScriptValidationError(HarnessError):
    """A script is unloadable: an unknown declaration, a malformed argument set, a bad policy.

    Always raised at **load** time, never mid-run. A script that names an action the catalog does
    not have, or hands an action a target of the wrong shape, is a mistake in the script; finding
    it on turn nine of a live block -- after the client is up, the save is loaded and the operator
    is watching -- costs a whole block to learn something a file read could have said instantly.
    """


class UnavailablePolicy(StrEnum):
    """What a script does when the game is not offering its current step's action.

    There is no "skip silently" member, by design: in every case below the action **is** issued
    at least once, so the harness's own dispatcher records the refusal with the game's own reason
    and the attempt exists on the record. The policy only decides what happens *after* that.
    """

    #: Stop the script. Every later call returns the end turn, stating where it stopped and how
    #: many declared steps were never reached.
    HALT = "halt"
    #: Advance to the next declared step. The refusal stays on the record; the script goes on.
    CONTINUE = "continue"
    #: Re-issue the same step on later requests, up to :attr:`ActionScript.attempts_per_step`
    #: issues in total, then halt. For a step that becomes available once something else settles.
    RETRY = "retry"


@dataclass(frozen=True)
class ScriptStep:
    """One declared action invocation.

    ``parameters`` is the argument set the harness dispatches, in the catalog's own single-key
    ``{"target": ...}`` convention (or ``{}`` for an action whose ``target_kind`` is ``none``).
    Validated against the named declaration at load time by :func:`load_action_script`, so a
    ``ScriptStep`` that reached a provider has already been checked for shape.
    """

    declaration_id: DeclarationId
    parameters: Mapping[str, Any] = field(default_factory=dict)
    #: A human-facing note carried into the decision's recorded reasoning -- why this step is in
    #: the chain. Never read by the harness for control flow.
    note: str = ""

    @property
    def is_end_turn(self) -> bool:
        return self.declaration_id == END_TURN_DECLARATION_ID


@dataclass(frozen=True)
class ActionScript:
    """A fixed, declared action sequence, validated against the catalog.

    **Cannot be constructed without steps.** ``steps`` is a required positional-or-keyword field
    with no default, and an empty sequence raises here. That is deliberate and it is the whole
    reason this type exists rather than the provider simply taking a list: this project's three
    most serious defects to date all had the shape *an optional parameter with a safe-looking
    empty default that every unit test supplies and the one production call site does not*. A
    scriptless :class:`ScriptedModelProvider` -- which would silently behave as "end every turn
    forever" -- is not constructible, because the thing it would need does not have an empty
    form.

    ``on_unavailable`` likewise has no default: a script must say what it does when the board is
    not offering its step, because the alternative is that the answer is whatever the
    implementation happened to do.
    """

    script_id: str
    steps: tuple[ScriptStep, ...]
    on_unavailable: UnavailablePolicy
    attempts_per_step: int
    description: str = ""
    source: Path | None = None

    def __post_init__(self) -> None:
        if not self.script_id.strip():
            raise ScriptValidationError("a script must have a non-empty script_id")
        if not self.steps:
            raise ScriptValidationError(
                "a script must declare at least one step; an empty script is not a script, it "
                "is a provider that silently ends every turn",
                detail={"script_id": self.script_id},
            )
        if self.attempts_per_step < 1:
            raise ScriptValidationError(
                "attempts_per_step must be at least 1",
                detail={"script_id": self.script_id, "attempts_per_step": self.attempts_per_step},
            )
        if self.on_unavailable is not UnavailablePolicy.RETRY and self.attempts_per_step != 1:
            raise ScriptValidationError(
                "attempts_per_step is only meaningful for on_unavailable: retry -- halt and "
                "continue issue the step exactly once",
                detail={
                    "script_id": self.script_id,
                    "on_unavailable": self.on_unavailable.value,
                    "attempts_per_step": self.attempts_per_step,
                },
            )


# --------------------------------------------------------------------------
# Loading and validating a script against the catalog
# --------------------------------------------------------------------------

#: What each ``TargetKind`` accepts as a declared target, and how to say so when it does not.
#: Mirrors ``agent/context.py``'s ``_TARGET_KIND_EXAMPLES`` -- the shape the harness renders to a
#: model as the command's shape is the shape a script must declare, or the script is declaring
#: something the game was never offered.
_TARGET_SHAPE_DESCRIPTION: Final[Mapping[TargetKind, str]] = {
    TargetKind.PLOT: 'an object with integer x and y, e.g. {"x": 43, "y": 31}',
    TargetKind.UNIT_ID: "an integer unit_id",
    TargetKind.CITY_ID: "an integer city_id",
    TargetKind.PLAYER_ID: "an integer player_id",
    TargetKind.RESOLUTION_ID: "an integer resolution_id",
    TargetKind.INDIVIDUAL_ID: "an integer individual_id",
    TargetKind.SPY_ID: "an integer unit_id for a spy",
    TargetKind.NAME: 'a non-empty string, e.g. "UNIT_BUILDER"',
    TargetKind.OPTION: 'a non-empty string naming one of the offered options, e.g. "continue"',
    TargetKind.NUMBER: "a number",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _target_is_well_shaped(kind: TargetKind, value: Any) -> bool:
    if kind is TargetKind.PLOT:
        return isinstance(value, Mapping) and _is_int(value.get("x")) and _is_int(value.get("y"))
    if kind in {
        TargetKind.UNIT_ID,
        TargetKind.CITY_ID,
        TargetKind.PLAYER_ID,
        TargetKind.RESOLUTION_ID,
        TargetKind.INDIVIDUAL_ID,
        TargetKind.SPY_ID,
    }:
        return _is_int(value)
    if kind in {TargetKind.NAME, TargetKind.OPTION}:
        return isinstance(value, str) and bool(value.strip())
    if kind is TargetKind.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False  # pragma: no cover - TargetKind.NONE is handled by the caller


def _validate_step(
    raw: Mapping[str, Any], *, index: int, catalog: Catalog, script_id: str
) -> ScriptStep:
    """One declared step, checked against the catalog. Raises rather than guessing."""
    where = {"script_id": script_id, "step": index}
    declaration_id = raw.get("declaration_id") or raw.get("action")
    if not isinstance(declaration_id, str) or not declaration_id.strip():
        raise ScriptValidationError(
            "every script step must name an action with declaration_id", detail=where
        )
    declaration = catalog.declarations.get(DeclarationId(declaration_id))
    if declaration is None:
        raise ScriptValidationError(
            f"unknown declaration_id {declaration_id!r} -- no such entry in the catalog",
            detail={**where, "declaration_id": declaration_id},
        )
    if declaration.kind is not DeclarationKind.ACTION:
        raise ScriptValidationError(
            f"{declaration_id!r} is a {declaration.kind.value} declaration, not an action; a "
            "script may only name actions",
            detail={**where, "declaration_id": declaration_id},
        )

    parameters = raw.get("parameters", {})
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, Mapping):
        raise ScriptValidationError(
            "a step's parameters must be a mapping",
            detail={**where, "declaration_id": declaration_id},
        )
    unexpected = sorted(set(parameters) - {"target"})
    if unexpected:
        raise ScriptValidationError(
            "a step's parameters may only carry 'target' -- the catalog's own single-argument "
            f"convention; got {unexpected}",
            detail={**where, "declaration_id": declaration_id},
        )

    kind = declaration.target_kind
    has_target = "target" in parameters
    if kind is None or kind is TargetKind.NONE:
        if has_target:
            raise ScriptValidationError(
                f"{declaration_id!r} takes no target, but this step declares one",
                detail={**where, "declaration_id": declaration_id},
            )
    else:
        if not has_target:
            raise ScriptValidationError(
                f"{declaration_id!r} requires a target ({_TARGET_SHAPE_DESCRIPTION[kind]}), but "
                "this step declares none",
                detail={**where, "declaration_id": declaration_id, "target_kind": kind.value},
            )
        if not _target_is_well_shaped(kind, parameters["target"]):
            raise ScriptValidationError(
                f"{declaration_id!r} declares target_kind {kind.value}, which needs "
                f"{_TARGET_SHAPE_DESCRIPTION[kind]}; got {parameters['target']!r}",
                detail={**where, "declaration_id": declaration_id, "target_kind": kind.value},
            )

    note = raw.get("note", "")
    if not isinstance(note, str):
        raise ScriptValidationError("a step's note must be a string", detail=where)

    repeat = raw.get("repeat", 1)
    if not _is_int(repeat) or repeat < 1:
        raise ScriptValidationError(
            "a step's repeat must be an integer of at least 1",
            detail={**where, "declaration_id": declaration_id, "repeat": repeat},
        )

    return ScriptStep(
        declaration_id=DeclarationId(declaration_id),
        parameters=dict(parameters),
        note=" ".join(note.split()),
    )


def load_action_script(path: str | Path, *, catalog: Catalog) -> ActionScript:
    """Read and fully validate a declared action script.

    **Everything that can be wrong with a script is wrong here, loudly, before a run starts.**
    An unknown ``declaration_id``, a declaration that is an observation rather than an action, a
    target of the wrong shape for the action's declared ``target_kind``, a target supplied to an
    action that takes none, an unknown ``on_unavailable`` policy, an empty step list -- each
    raises :class:`ScriptValidationError` naming the offending step by index. Nothing is
    corrected, defaulted or guessed: a script that does not say what it means is a script whose
    run would not mean what it said.

    ``repeat: N`` on a step is expanded here into N identical entries, so the ledger and the
    recorded reasoning both address a concrete step ordinal rather than "iteration 3 of step 2".
    """
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScriptValidationError(
            "could not read the script file", detail={"path": str(source), "error": str(exc)}
        ) from exc
    except yaml.YAMLError as exc:
        raise ScriptValidationError(
            "the script file is not valid YAML", detail={"path": str(source), "error": str(exc)}
        ) from exc

    if not isinstance(raw, Mapping):
        raise ScriptValidationError(
            "a script file must be a mapping with script_id, on_unavailable and steps",
            detail={"path": str(source)},
        )

    script_id = raw.get("script_id") or source.stem
    if not isinstance(script_id, str):
        raise ScriptValidationError("script_id must be a string", detail={"path": str(source)})

    policy_name = raw.get("on_unavailable")
    if policy_name is None:
        raise ScriptValidationError(
            "a script must declare on_unavailable -- what it does when the game is not offering "
            f"the current step's action; one of {sorted(p.value for p in UnavailablePolicy)}",
            detail={"path": str(source), "script_id": script_id},
        )
    try:
        policy = UnavailablePolicy(policy_name)
    except ValueError as exc:
        raise ScriptValidationError(
            f"unknown on_unavailable policy {policy_name!r}; expected one of "
            f"{sorted(p.value for p in UnavailablePolicy)}",
            detail={"path": str(source), "script_id": script_id},
        ) from exc

    declared_attempts = raw.get("attempts_per_step")
    if policy is UnavailablePolicy.RETRY:
        if declared_attempts is None:
            raise ScriptValidationError(
                "on_unavailable: retry must declare attempts_per_step -- how many times one "
                "step may be re-issued before the script halts. A retry without a declared "
                "bound is an unbounded wait wearing a policy's name",
                detail={"path": str(source), "script_id": script_id},
            )
        if not _is_int(declared_attempts) or declared_attempts < 1:
            raise ScriptValidationError(
                "attempts_per_step must be an integer of at least 1",
                detail={"path": str(source), "script_id": script_id},
            )
        attempts_per_step = int(declared_attempts)
    else:
        if declared_attempts is not None:
            raise ScriptValidationError(
                f"attempts_per_step is only meaningful for on_unavailable: retry, not "
                f"{policy.value}",
                detail={"path": str(source), "script_id": script_id},
            )
        attempts_per_step = 1

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
        raise ScriptValidationError(
            "a script's steps must be a list", detail={"path": str(source), "script_id": script_id}
        )

    steps: list[ScriptStep] = []
    for index, entry in enumerate(raw_steps, start=1):
        if not isinstance(entry, Mapping):
            raise ScriptValidationError(
                "each script step must be a mapping",
                detail={"path": str(source), "script_id": script_id, "step": index},
            )
        step = _validate_step(entry, index=index, catalog=catalog, script_id=str(script_id))
        repeat = entry.get("repeat", 1)
        steps.extend([step] * int(repeat))

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ScriptValidationError(
            "description must be a string", detail={"path": str(source), "script_id": script_id}
        )

    return ActionScript(
        script_id=str(script_id),
        steps=tuple(steps),
        on_unavailable=policy,
        attempts_per_step=attempts_per_step,
        description=" ".join(description.split()),
        source=source,
    )


# --------------------------------------------------------------------------
# The ledger -- what happened to each declared step
# --------------------------------------------------------------------------


class ScriptStepStatus(StrEnum):
    """What became of one declared step.

    The distinction :data:`NOT_REACHED` draws is the point of this type. A step that was issued
    and refused leaves a record in the store; a step the script never got to leaves none, and an
    absent record is indistinguishable from "ran and did nothing" unless something says otherwise.
    This says otherwise.
    """

    #: Not yet its turn, and the script is still running.
    PENDING = "pending"
    #: Issued while the request showed the game offering it.
    ISSUED_AVAILABLE = "issued_available"
    #: Issued while the request showed it greyed out (or did not list it). The refusal is on the
    #: record with the game's own reason; ``reason`` below carries what the request said.
    ISSUED_UNAVAILABLE = "issued_unavailable"
    #: The script stopped before this step. It was never issued and never refused; nothing about
    #: it was observed at all.
    NOT_REACHED = "not_reached"


@dataclass(frozen=True)
class LedgerRow:
    """One declared step's outcome, as this provider observed it.

    ``sequence_epoch`` is how many times the harness restarted ``step_index`` at 1 before this
    step was issued. **A restart is a new turn cycle -- and the provider cannot tell a new TURN
    from a REPLAYED ATTEMPT of the same one**, because a `DecisionRequest` carries no attempt
    index and the observed turn number does not reliably advance between cycles (an end turn that
    verified through ``is_waiting_for_other_players`` leaves it where it was). So this number does
    not claim to be either; it is the one thing that IS observable, and it is recorded rather than
    interpreted. Two steps of one chain carrying different epochs is the signal that the chain may
    not have run against one continuous board. ``civsim store coverage`` answers the same question
    from the other side and can be definite about it: it counts scripted steps recorded in
    attempts the store marks non-authoritative (``ScriptedCoverage.steps_in_abandoned_attempts``).
    """

    ordinal: int
    declaration_id: str
    parameters: Mapping[str, Any]
    status: ScriptStepStatus
    issues: int = 0
    reason: str = ""
    note: str = ""
    sequence_epoch: int | None = None
    step_index: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "declaration_id": self.declaration_id,
            "parameters": dict(self.parameters),
            "status": self.status.value,
            "issues": self.issues,
            "reason": self.reason,
            "note": self.note,
            "sequence_epoch": self.sequence_epoch,
            "step_index": self.step_index,
        }


# --------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------


class ScriptedModelProvider:
    """Serves each decision from the next step of a declared :class:`ActionScript` (T326).

    Satisfies :class:`~civsim_harness.provider.port.ModelProvider` structurally and is wired the
    same way every other adapter is -- handed to ``run/composition.py``'s
    ``build_runner_dependencies(provider=...)``, where the ordinary ``ProviderChain`` wraps it.

    **The script is a required positional argument with no default.** There is no scriptless
    construction, no empty-script fallback, and no "if script is None" branch anywhere below.

    **The cursor is monotonic across the whole run and never rewinds.** A script is a sequence of
    decisions for a run, not for a turn, so nothing here resets on a turn boundary. If the harness
    abandons and replays a turn attempt, the steps already issued stay issued: the ledger records
    exactly what was sent, which is the honest record, rather than a reconstruction of what a
    fresh attempt "should" have sent.
    """

    def __init__(self, script: ActionScript) -> None:
        self.script = script
        self._cursor = 0
        self._issues_at_cursor = 0
        self._status: list[ScriptStepStatus] = [ScriptStepStatus.PENDING] * len(script.steps)
        self._issues: list[int] = [0] * len(script.steps)
        self._reasons: list[str] = [""] * len(script.steps)
        self._epochs: list[int | None] = [None] * len(script.steps)
        self._step_indexes: list[int | None] = [None] * len(script.steps)
        self._halted_at: int | None = None
        self._halt_reason = ""
        #: How many times ``step_index`` has restarted at 1 -- see :class:`LedgerRow`.
        self._sequence_epoch = 0
        self._last_step_index: int | None = None

    # -- ModelProvider protocol ---------------------------------------------

    def describe(self, model: ModelRef) -> ModelCapabilities:
        """Report this adapter's own capabilities (P1 chain preflight, FR-039).

        Independent of *model*: nothing about which model a configuration names changes what this
        adapter can carry, because it sends nothing anywhere.
        """
        return ModelCapabilities(
            accepts_images=True,
            max_context_tokens=_UNBOUNDED_CONTEXT_TOKENS,
            max_images_per_request=None,
            confirmed=True,
        )

    def complete(self, request: DecisionRequest) -> DecisionResponse:
        """Serve exactly one decision for *request*'s single decision step.

        Never fails: there is no wire, no quota and no parse to go wrong, so every call comes back
        ``CallOutcome.DECISION_RETURNED``. ``request`` is read and never modified (the port
        contract's "Adapter obligations"), and ``image_count`` is reported as the request's own.

        This is the **only** ``DecisionResponse`` this adapter ever constructs, and it always
        reports :data:`~civsim_harness.models.provenance.SCRIPTED_MODEL_REF`. That is what makes
        the coverage separation hold: there is no path through this class that yields a response
        attributed to anything else.
        """
        started = time.perf_counter()
        decision = self._decide(request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return DecisionResponse(
            decision=decision,
            model_served=SCRIPTED_MODEL_REF,
            latency_ms=latency_ms,
            cost=ZERO_COST,
            retry_count=0,
            fallback_occurred=False,
            image_count=len(request.images),
            outcome=CallOutcome.DECISION_RETURNED,
        )

    # -- the ledger ---------------------------------------------------------

    def ledger(self) -> tuple[LedgerRow, ...]:
        """One row per declared step, with its explicit status.

        Callable at any time. While the script is running, steps beyond the cursor are
        ``pending``; once it has halted or run out they are ``not_reached``, which is a different
        claim and is reported as one.
        """
        stopped = self._halted_at is not None or self._cursor >= len(self.script.steps)
        rows: list[LedgerRow] = []
        for index, step in enumerate(self.script.steps):
            status = self._status[index]
            if status is ScriptStepStatus.PENDING and stopped:
                status = ScriptStepStatus.NOT_REACHED
            rows.append(
                LedgerRow(
                    ordinal=index + 1,
                    declaration_id=str(step.declaration_id),
                    parameters=dict(step.parameters),
                    status=status,
                    issues=self._issues[index],
                    reason=self._reasons[index],
                    note=step.note,
                    sequence_epoch=self._epochs[index],
                    step_index=self._step_indexes[index],
                )
            )
        return tuple(rows)

    @property
    def steps_not_reached(self) -> int:
        return sum(
            1 for row in self.ledger() if row.status is ScriptStepStatus.NOT_REACHED
        )

    def ledger_summary(self) -> str:
        """One sentence naming exactly where the script stands -- the text written into the
        recorded reasoning of every decision issued after the script stops."""
        total = len(self.script.steps)
        if self._halted_at is not None:
            step = self.script.steps[self._halted_at]
            return (
                f"script {self.script.script_id!r} HALTED at declared step "
                f"{self._halted_at + 1} of {total} ({step.declaration_id}) because "
                f"{self._halt_reason}; {self.steps_not_reached} later step(s) were never "
                "reached, and nothing was observed about them"
            )
        return (
            f"script {self.script.script_id!r} COMPLETE: all {total} declared step(s) were "
            "issued"
        )

    # -- deciding -----------------------------------------------------------

    def _decide(self, request: DecisionRequest) -> RawDecision:
        self._note_sequence(request.step_index)
        listed = _listed_actions(request.observation)

        if self._halted_at is not None or self._cursor >= len(self.script.steps):
            return self._end_turn_decision(reason=self.ledger_summary())

        index = self._cursor
        step = self.script.steps[index]
        available, rendered_reason = listed.get(
            str(step.declaration_id), (False, NOT_LISTED_REASON)
        )

        self._issues[index] += 1
        self._issues_at_cursor += 1
        self._epochs[index] = self._sequence_epoch
        self._step_indexes[index] = request.step_index
        if available:
            self._status[index] = ScriptStepStatus.ISSUED_AVAILABLE
            self._reasons[index] = ""
            self._advance()
            disposition = "the request shows the game offering this action right now"
        else:
            # Never a silent skip: the action is issued regardless, so the harness's own
            # dispatcher records the refusal with the game's word for it. The declared policy
            # only decides what the SCRIPT does next.
            self._status[index] = ScriptStepStatus.ISSUED_UNAVAILABLE
            self._reasons[index] = rendered_reason or "no reason rendered"
            disposition = self._apply_unavailable_policy(index, rendered_reason)

        target_note = (
            "this action takes no target"
            if "target" not in step.parameters
            else f"declared target: {step.parameters['target']!r}"
        )
        purpose = f" Declared purpose: {step.note}" if step.note else ""
        return RawDecision(
            action_declaration_id=step.declaration_id,
            reasoning=(
                f"scripted provider (script={self.script.script_id!r}, declared step "
                f"{index + 1} of {len(self.script.steps)}, issue {self._issues[index]}): "
                f"{step.declaration_id}; {target_note}. "
                f"{disposition}. "
                "THIS ACTION WAS NAMED BY A DECLARED SCRIPT, NOT CHOSEN BY AN AGENT -- it is a "
                "harness capability test of the action chain and is recorded in a separate "
                "coverage tier; it is no evidence about what an agent would choose."
                f"{purpose} "
                "No model was consulted and no information outside this request was read."
            ),
            parameters=dict(step.parameters),
            is_end_turn=step.is_end_turn,
        )

    def _note_sequence(self, step_index: int) -> None:
        """Count a ``step_index`` restart, and interpret nothing from it.

        ``run/decision_loop.py`` numbers a turn attempt's steps from ``step_index_base + 1``
        and increments monotonically within the attempt (the pre-save prompt-clearance pass
        shares the numbering through that base rather than restarting it), so a step index that
        is 1, or that has gone backwards, is a new turn cycle. It is **not** necessarily a new
        turn: an abandoned attempt's replay looks exactly the same from here.
        """
        if self._last_step_index is not None and (
            step_index <= 1 or step_index < self._last_step_index
        ):
            self._sequence_epoch += 1
        self._last_step_index = step_index

    def _advance(self) -> None:
        self._cursor += 1
        self._issues_at_cursor = 0

    def _apply_unavailable_policy(self, index: int, rendered_reason: str) -> str:
        """Act on the declared policy and return the wording for the decision's own record."""
        policy = self.script.on_unavailable
        said = rendered_reason or NOT_LISTED_REASON
        if policy is UnavailablePolicy.CONTINUE:
            self._advance()
            return (
                f"the request shows this action as NOT available ({said}); it is issued anyway "
                "so the refusal is recorded with the game's own reason, and the declared "
                "on_unavailable policy 'continue' advances the script to its next step"
            )
        if policy is UnavailablePolicy.RETRY:
            if self._issues_at_cursor >= self.script.attempts_per_step:
                self._halt(
                    index,
                    f"it was not available on any of its {self.script.attempts_per_step} "
                    f"declared attempt(s); the request last said: {said}",
                )
                return (
                    f"the request shows this action as NOT available ({said}); it is issued "
                    "anyway so the refusal is recorded, and this was its last of "
                    f"{self.script.attempts_per_step} declared attempt(s) under the "
                    "on_unavailable policy 'retry', so the script halts here"
                )
            return (
                f"the request shows this action as NOT available ({said}); it is issued anyway "
                "so the refusal is recorded, and the declared on_unavailable policy 'retry' "
                f"keeps the script on this step (attempt {self._issues_at_cursor} of "
                f"{self.script.attempts_per_step})"
            )
        self._halt(index, f"the request showed it as not available: {said}")
        return (
            f"the request shows this action as NOT available ({said}); it is issued anyway so "
            "the refusal is recorded with the game's own reason, and the declared "
            "on_unavailable policy 'halt' stops the script here"
        )

    def _halt(self, index: int, reason: str) -> None:
        self._halted_at = index
        self._halt_reason = reason

    def _end_turn_decision(self, *, reason: str) -> RawDecision:
        """The turn ends once the script has nothing left to say.

        The ledger summary travels in the reasoning, so the store -- not only this process's
        memory -- carries the statement that the script stopped and that N declared steps were
        never reached.
        """
        return RawDecision(
            action_declaration_id=END_TURN_DECLARATION_ID,
            reasoning=(
                f"scripted provider: ending the turn because {reason}. "
                "THIS DECISION WAS PRODUCED BY A DECLARED SCRIPT, NOT CHOSEN BY AN AGENT. "
                "No model was consulted and no information outside this request was read."
            ),
            parameters={},
            is_end_turn=True,
        )
