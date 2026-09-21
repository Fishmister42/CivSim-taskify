"""A seeded, uniformly-sampling ``ModelProvider`` that costs nothing (T261).

**What it is for.** The live lane wants *breadth of demonstrated harness behaviour* -- many
different actions dispatched, verified, refused and recorded -- without paying for a model and
without the scripted-single-decision narrowness of ``tests/fakes/fake_provider.py`` (which ends
every turn immediately and therefore exercises exactly one declaration). This provider picks
uniformly among the actions *the request itself lists*, derives a target from what the same
request shows, and returns it. Play quality is not a goal; coverage of the action surface at
$0 is.

**Principle I (NON-NEGOTIABLE) is the whole design constraint here, and it is enforced
structurally rather than by care.** The only thing this module reads is the
``DecisionRequest`` it was handed: ``request.observation`` (the parity-filtered state text plus
the rendered action catalog, exactly as the model would read it) and ``request.step_index``.
It imports nothing from ``store``, ``observe``, ``capability`` (the catalog loaders), ``act``,
``run`` or ``host``; it opens no file, reads no YAML, and speaks to no game. That is not a
convention -- ``tests/unit/test_stochastic_provider.py`` walks this module's transitive
first-party import closure and fails if any of those packages appears in it. A provider that
could consult the catalog's own ``availability_predicate`` would be choosing with information
the agent does not have, which is precisely the leak Principle I exists to prevent: the
measurements it would improve would no longer be measurements of an agent playing at human
parity.

**T262 does not weaken that, it moves the line.** Availability is no longer private to the
executor: ``agent/context.py`` now evaluates each action's predicate against this step's
observation and renders the catalog in two groups -- the commands the game is offering, and the
ones it is showing greyed out with the reason why -- because that is what a human reads off the
screen. This module therefore *may* know which actions are available, and knows it the only way
it is allowed to: by reading the request's own rendered groups (:data:`_AVAILABLE_GROUP_HEADER`,
:data:`_UNAVAILABLE_GROUP_HEADER`), exactly as the model does. The import closure is unchanged,
and the closure test still fails if that ever stops being true.

**Two policies (:data:`PROVIDER_POLICIES`).** ``uniform`` is the original behaviour and the
default: draw from everything the request lists. ``coverage`` (T262) draws only from the
"available now" group, preferring actions this run has not landed yet -- the owner's rule,
verbatim: *"the stochastic testing should only select from actions of a reachable state."* It
never falls back to the greyed-out group, not even when the available group is empty; it ends
the turn instead, and that end-turn decision is recorded like any other. MEASURED (2026-09-21,
``civsim store coverage``): under ``uniform``, nine of the fourteen catalog actions that had
been attempted but never applied were draws for a situation that was not on screen -- a
promotion with no promotable unit, a pantheon with no faith, a peace offer with nobody at war,
a prompt answer with no prompt up -- every one refused at dispatch without reaching the game.

**How a target is derived, and why it is never invented.** T256 gave every action a rendered
``-- target: <one concrete example>; <hint>`` tail (``agent/context.py``), and both halves name
their own source in plain text -- "a unit_id from units.state", "a destination plot from the
selected unit's reachable_plots", "a promotion from the selected unit's available_promotions".
This module reads that tail for two things: the JSON *example* fixes the shape the target must
have (a ``{"x": .., "y": ..}`` plot, an integer id, a name string, a number, or no target at
all), and the identifiers named in the prose say which observed field to draw candidate values
from. Candidates are harvested out of the observed-state lines' own JSON and sampled uniformly.
When no candidate of the required shape can be found in what the request shows, the action is
**dropped and another is sampled** -- never completed with a plausible-looking guess. If every
listed action falls that way, the turn is ended. A fabricated target would be recorded as the
agent's own decision and would corrupt exactly the refusal/verification statistics this
provider exists to generate.

**A blocking prompt is answered first, not sampled into.** MEASURED twice in live play: with a
modal up, uniform sampling needed roughly seven draws to find the one acknowledge action, and a
whole play block went on nothing else. When the rendered observation says a prompt is blocking
play -- ``has_blocking_prompt`` true, a ``prompt.*`` screen id, or non-empty ``prompt_options``
-- this provider samples first from the listed actions that answer *that* prompt, identified by
matching the rendered screen id against each action's own rendered id and summary, with the
target drawn from the rendered ``prompt_options``. Only if no such action is listed (or none of
them yields a usable target) does it fall back to the ordinary uniform draw. Answering the block
does not spend the turn's action budget below -- the game is demanding a response, this provider
is not choosing to spend a move -- and the per-turn repeat bound still stops it retrying the same
answer forever.

**Within-turn behaviour.**

- At most ``max_actions_per_turn`` non-end-turn decisions per turn (default
  :data:`DEFAULT_MAX_ACTIONS_PER_TURN`), then the end turn. This is a property of *this
  provider's* play, not of the harness: ``run/decision_loop.py`` deliberately has no step cap
  (invariant I16), and nothing here asks it to acquire one.
- No listed actions at all (or none whose target can be derived) -> end the turn.
- An action id the request reports as refused is **down-weighted, not forbidden**
  (:data:`REFUSED_ACTION_WEIGHT`), using only :func:`_refused_action_ids` -- a narrow scan of
  the request text for the harness's own rejection vocabulary. The harness does not render
  refusal information into the observation today, so that scan ordinarily finds nothing and
  nothing is down-weighted; it is written against the text rather than against a private
  channel so that it starts working the moment such information *is* rendered, and can never
  start working by reading a record the agent cannot see.
- The same ``(action id, target)`` pair is chosen at most :data:`MAX_REPEATS_PER_TURN` times in
  one turn, from this provider's own memory of what it returned -- the loop bound. A pair that
  has hit the bound is skipped and another is sampled.
- An action id not yet chosen anywhere in this run is mildly preferred (weight ``1.0`` against
  :data:`ALREADY_CHOSEN_WEIGHT` for one already seen), again from its own memory only. "Mildly"
  is the point: it biases towards breadth without ever making the choice deterministic.

A turn boundary is read off ``request.step_index`` (the decision loop restarts it at 1 for every
turn *and* every replayed attempt), so the per-turn state above resets on exactly the same
signal the harness itself uses, with nothing carried across a turn that should not be.

**Accounting.** Every call reports ``model_served = stochastic/uniform-v1`` and a zero
:class:`~civsim_harness.models.common.Cost`, so the store's ``model_calls`` table shows this
run's decisions as served by ``provider=stochastic`` at ``amount_usd=0.0`` rather than
attributing them to whichever model the configuration nominally names (P3: a substituted model
must always be *reported*, never hidden). The zero is not a placeholder or an independent
pricing of anything -- it is this provider's own true, provider-reported usage (P7): no request
leaves the process.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from civsim_harness.models.common import Cost, DeclarationId, ModelRef
from civsim_harness.models.records import CallOutcome
from civsim_harness.provider.port import (
    DecisionRequest,
    DecisionResponse,
    ModelCapabilities,
    RawDecision,
)

# --------------------------------------------------------------------------
# Identity and tunables
# --------------------------------------------------------------------------

#: What the store records as the provider for every call this adapter serves.
STOCHASTIC_PROVIDER_NAME: Final = "stochastic"

#: The "model" half of :data:`STOCHASTIC_MODEL_REF`. Versioned because the sampling policy is
#: what a recorded run is reproducible *against*: a future change to how actions are weighted or
#: targets harvested should be a new name here, not a silent re-interpretation of old records.
STOCHASTIC_MODEL_NAME: Final = "uniform-v1"

#: Reported as ``DecisionResponse.model_served`` on every call (P3).
STOCHASTIC_MODEL_REF: Final = ModelRef(
    provider=STOCHASTIC_PROVIDER_NAME, model=STOCHASTIC_MODEL_NAME
)

#: Provider-reported usage for a call that never left the process (P7).
ZERO_COST: Final = Cost(input_tokens=0, output_tokens=0, total_tokens=0, amount_usd=0.0)

#: How many non-end-turn decisions this provider makes in one turn before ending it.
DEFAULT_MAX_ACTIONS_PER_TURN: Final = 6

#: The catalog's own well-known end-turn declaration id -- the same "named by convention, looked
#: up in what the request actually shows" arrangement ``run/decision_loop.py`` uses for
#: ``game.screen_state``. It is only ever *used* as a fallback: :meth:`_end_turn_action` prefers
#: whichever listed action the request itself presents as the end turn.
END_TURN_DECLARATION_ID: Final = DeclarationId("turn.end_turn")

#: Weight multiplier for an action id the request reports as refused earlier in this turn.
#: Deliberately not zero: a refusal is often about *this* target or *this* moment, and forbidding
#: the id outright would hide from the record that the same action becomes available again.
REFUSED_ACTION_WEIGHT: Final = 0.25

#: Weight multiplier for an action id already chosen somewhere in this run -- the mild coverage
#: preference. Close to 1.0 on purpose; this biases, it does not schedule.
ALREADY_CHOSEN_WEIGHT: Final = 0.6

#: How many times one ``(action id, target)`` pair may be chosen within a single turn.
MAX_REPEATS_PER_TURN: Final = 2

#: The sampling policy this provider was built with. ``uniform`` is the original T261 behaviour
#: and remains the default: draw from every action the request lists, biased only by the two
#: documented weights. ``coverage`` (T262) draws **only** from the actions the request shows in
#: its "available now" group -- the owner's rule, verbatim: *"the stochastic testing should only
#: select from actions of a reachable state."*
UNIFORM_POLICY: Final = "uniform"
COVERAGE_POLICY: Final = "coverage"

#: Every value :meth:`StochasticModelProvider.__init__`'s ``policy`` accepts, and therefore every
#: value a ``--provider-policy`` flag in front of it may take.
PROVIDER_POLICIES: Final = (UNIFORM_POLICY, COVERAGE_POLICY)

#: The "model" half of what each policy reports as ``model_served``. A recorded run is
#: reproducible against the policy that produced it, so the two policies must never be
#: indistinguishable in the store's ``model_calls`` table (P3).
STOCHASTIC_MODEL_NAME_BY_POLICY: Final[Mapping[str, str]] = {
    UNIFORM_POLICY: STOCHASTIC_MODEL_NAME,
    COVERAGE_POLICY: "coverage-v1",
}

#: What :meth:`StochasticModelProvider.describe` reports. This adapter performs no I/O, so there
#: is no context window to exceed and no wire format for an image to be dropped from: it accepts
#: whatever it is handed and reports ``image_count`` faithfully (it simply does not look at the
#: pixels). ``confirmed=True`` because these are facts about this module, not an unverified claim
#: about a remote service.
_UNBOUNDED_CONTEXT_TOKENS: Final = 1_000_000_000


# --------------------------------------------------------------------------
# Reading the request -- the only input this module has
# --------------------------------------------------------------------------

#: The header ``agent.context.assemble_action_catalog_text`` writes above the action list.
#: Everything before it in ``request.observation`` is observed state; everything after it that
#: looks like a bullet is an action the request lists.
_ACTION_SECTION_HEADER: Final = "Actions you may take"

#: T262: the two group headers the request writes above the actions the game is offering and the
#: actions it is showing greyed out (``agent.context.{AVAILABLE,UNAVAILABLE}_GROUP_HEADER``).
#: Copied here as text rather than imported, for the same reason
#: :data:`_ACTION_SECTION_HEADER` is: this module may read the request and nothing else, and
#: ``agent/context.py`` reaches the catalog loader. ``tests/unit/test_stochastic_provider.py``
#: pins the two spellings together, so a rename cannot silently stop the grouping being seen.
_AVAILABLE_GROUP_HEADER: Final = "Available now"
_UNAVAILABLE_GROUP_HEADER: Final = "Not available now"

#: T262: what the request writes after an action's target tail when it is greyed out, followed by
#: the reason. Stripped before the target tail is parsed so a reason's own words and quoted
#: values can never be harvested as a target.
_UNAVAILABLE_REASON_MARKER: Final = " -- not available: "

#: One observed-state line, exactly as ``agent.context._render_entry`` renders it:
#: ``- [InGame] units.state: {"units": [...]}``.
_STATE_LINE_RE: Final = re.compile(r"^- \[(?P<context>[^\]]*)\]\s+(?P<key>[^:]+):\s*(?P<value>.*)$")

#: One action line: ``- units.move_to: <summary> -- target: <example>; <hint>``.
_ACTION_LINE_RE: Final = re.compile(r"^-\s+(?P<id>[A-Za-z0-9_.]+):\s*(?P<rest>.*)$")

#: The separator T256 renders between an action's summary and its target guidance.
_TARGET_SEPARATOR: Final = " -- target: "

#: A snake_case identifier, optionally dotted (``unit_id``, ``units.state``,
#: ``player.researchable_techs``) -- how the rendered example and hint name the observed field a
#: target is drawn from. Deliberately requires an ``_`` or a ``.``: a bare English word in the
#: prose ("a revealed plot", "a name exactly as the observed state lists it") must not be read as
#: naming an observed field, or a technology target would be sampled from city *names* because
#: "name" happens to be a key somewhere in the state.
_IDENTIFIER_RE: Final = re.compile(
    r"\b[a-z][a-z0-9_]*(?:[._][a-z][a-z0-9_]*)+\b"
)

#: A double-quoted literal in the target guidance, e.g. ``"world" or "strategic"``. Used only for
#: option-shaped targets, where the request is *enumerating the offered options* rather than
#: illustrating a shape.
_QUOTED_RE: Final = re.compile(r'"([^"]+)"')

#: A numeric literal in the target guidance, e.g. ``from 0.05 (closest) to 1.0 (farthest)``.
_NUMBER_RE: Final = re.compile(r"-?\d+(?:\.\d+)?")

#: The rendered example's own wording for the two string-shaped target kinds. "offered options"
#: is what tells an option apart from a name, which matters because an option's guidance may
#: quote the options themselves while a name must always come from the observed state.
_OPTION_EXAMPLE_MARKER: Final = "offered options"

#: A screen id that names an open prompt, as ``game.screen_state`` renders it
#: (``"prompt.tech_civic_completed"``). Matched against *any* string in the observed state rather
#: than against a particular key, so it does not depend on which field a future catalog writes it
#: under.
_PROMPT_SCREEN_RE: Final = re.compile(r"^prompt\.[a-z][a-z0-9_]*$")

#: The observed field that carries an open prompt's own offered answers.
_PROMPT_OPTIONS_FIELD: Final = "prompt_options"

#: The observed field that says a prompt is blocking play.
_BLOCKING_PROMPT_FIELD: Final = "has_blocking_prompt"

#: How a listed action says, in what the request renders, that it answers a prompt: its id's
#: first segment, or its summary's own wording. Both are request text -- no catalog is consulted
#: to decide which actions are prompt answers.
_PROMPT_ACTION_PREFIX: Final = "prompts."
_PROMPT_SUMMARY_MARKERS: Final = ("prompt", "popup", "acknowledge")

#: How many leading characters of a prompt-id word must appear in an action's text for the
#: summary-match tier to count it. Five is enough to bridge the real gaps between a screen id and
#: the prose that describes it ("declare_war" against "declaration-of-war", "tech_civic_completed"
#: against "technology / civic completed") without matching on a shared prefix by accident.
_WORD_STEM_LEN: Final = 5

#: The vocabulary the harness uses for a refused action
#: (``models/decision.py``'s ``RejectionReason``). Matched against the request text only -- see
#: the module docstring on why this is deliberately a text scan and not a record read.
_REFUSAL_MARKERS: Final = (
    "unavailable_to_human_now",
    "not_in_catalog",
    "illegal_in_context",
    "out_of_parity_camera",
    "rejection_reason",
    "was refused",
    "was rejected",
)

#: The vocabulary the harness uses for an action that really ran
#: (``models/decision.py``'s ``ExecutionOutcome.APPLIED``). Matched against the request text only,
#: exactly like :data:`_REFUSAL_MARKERS` and for the same reason: the coverage policy's "prefer
#: what has not landed yet" may only be informed by what a *later request reports back*, never by
#: a store read. Nothing renders an outcome into the observation today, so this ordinarily finds
#: nothing and the policy falls back to its own record of what it has chosen -- but it starts
#: working the moment such information is rendered, and can never start working by reading a
#: record the agent cannot see.
_APPLIED_MARKERS: Final = (
    '"applied"',
    "was applied",
    "execution_outcome",
    "outcome: applied",
)


@dataclass(frozen=True)
class _ListedAction:
    """One action line as the request rendered it."""

    declaration_id: str
    summary: str
    #: The parsed ``{"target": <example>}`` value from the rendered example, or ``None`` when the
    #: request shows this action as taking no target at all.
    target_example: Any | None
    #: Everything after ``-- target: `` -- example prose plus hint. The prose names the observed
    #: field a real target is drawn from; the example only fixes its shape.
    target_guidance: str
    #: The hint half alone (the guidance after the example's JSON), which is where an option's
    #: own offered values and a number's own range are written.
    target_hint: str
    #: T262: whether the request listed this action under its "available now" group. ``True``
    #: when the request renders no grouping at all, which is what a pre-T262 rendering meant.
    available: bool = True
    #: The reason the request gave for greying it out, verbatim; ``""`` when it did not.
    unavailable_reason: str = ""


@dataclass(frozen=True)
class _Candidate:
    """One value harvested from the observed state, with where it came from."""

    value: Any
    entry_key: str
    under_selected: bool


def _parse_observed_state(observation: str) -> dict[str, Any]:
    """Every observed-state line's declaration key mapped to its decoded JSON value.

    Stops at the action-catalog header: nothing below it is observed state. A line whose value
    will not decode as JSON is skipped rather than raised on -- this is a best-effort read of
    text written for a model, and a single odd line must not cost the whole turn.
    """
    values: dict[str, Any] = {}
    for line in observation.splitlines():
        if line.startswith(_ACTION_SECTION_HEADER):
            break
        match = _STATE_LINE_RE.match(line)
        if match is None:
            continue
        try:
            values[match.group("key").strip()] = json.loads(match.group("value"))
        except (json.JSONDecodeError, ValueError):
            continue
    return values


def _split_target_example(guidance: str) -> tuple[Any | None, str]:
    """``(example target value, the hint that followed it)`` from one rendered target tail.

    The example is found by locating ``{"target":`` and reading to its balanced closing brace,
    which is more robust than a regex against an example whose value is itself an object
    (``{"target": {"x": 43, "y": 31}}``). ``(None, guidance)`` when the tail shows no example at
    all -- which is how T256 renders an action that takes no target.
    """
    start = guidance.find('{"target"')
    if start == -1:
        return None, guidance
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(guidance)):
        char = guidance[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                blob = guidance[start : index + 1]
                try:
                    parsed = json.loads(blob)
                except (json.JSONDecodeError, ValueError):
                    return None, guidance
                if not isinstance(parsed, dict) or "target" not in parsed:
                    return None, guidance
                return parsed["target"], guidance[index + 1 :]
    return None, guidance


def _parse_listed_actions(observation: str) -> list[_ListedAction]:
    """Every action the request lists, in the order it listed them, with its rendered group.

    Only bullets *below* the action-catalog header are read, so an observed-state line can never
    be mistaken for an action. An action rendered without a target tail (one authored before
    T256) is treated as taking no target, which is what the pre-T256 rendering meant.

    T262: a line under the request's "not available now" heading is recorded as such, with the
    reason it gave, and its ``-- not available: ...`` tail is removed before the target tail is
    read -- so an action's parsed target guidance is exactly what it was before the grouping
    existed. A request that renders no grouping leaves every action ``available=True``, which is
    what "the request lists it" meant before T262 and keeps the uniform policy unchanged.
    """
    actions: list[_ListedAction] = []
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
        rest, _, reason = match.group("rest").partition(_UNAVAILABLE_REASON_MARKER)
        summary, separator, guidance = rest.partition(_TARGET_SEPARATOR)
        example, hint = _split_target_example(guidance) if separator else (None, "")
        actions.append(
            _ListedAction(
                declaration_id=match.group("id"),
                summary=summary.strip(),
                target_example=example,
                target_guidance=guidance,
                target_hint=hint,
                available=available,
                unavailable_reason=reason.strip(),
            )
        )
    return actions


def _ids_reported_with(
    observation: str, listed_ids: frozenset[str], markers: Sequence[str]
) -> frozenset[str]:
    """Action ids this request reports alongside one of *markers*, from the request text alone.

    Scans for the harness's own outcome vocabulary and, on any line carrying it, collects the
    declaration-id-shaped tokens that are also ids this same request listed as actions. Returns
    an empty set when the request says nothing -- which is the ordinary case today, since nothing
    renders outcome information into the observation yet. Deliberately intersected with
    *listed_ids*: an observed-state key is itself a dotted declaration id, and only an action can
    be weighted.

    Stops at the action-catalog header: the rendered catalog names every action and, since T262,
    also carries the words the harness uses when it greys one out ("refused", "recorded"). Only
    the *observed state* above it can report what an earlier decision did.
    """
    if not listed_ids:
        return frozenset()
    found: set[str] = set()
    for line in observation.splitlines():
        if line.startswith(_ACTION_SECTION_HEADER):
            break
        lowered = line.lower()
        if not any(marker in lowered for marker in markers):
            continue
        found.update(token for token in _IDENTIFIER_RE.findall(line) if token in listed_ids)
    return frozenset(found)


def _refused_action_ids(observation: str, listed_ids: frozenset[str]) -> frozenset[str]:
    """Action ids this request reports as refused (:data:`_REFUSAL_MARKERS`)."""
    return _ids_reported_with(observation, listed_ids, _REFUSAL_MARKERS)


def _applied_action_ids(observation: str, listed_ids: frozenset[str]) -> frozenset[str]:
    """Action ids this request reports as having really run (:data:`_APPLIED_MARKERS`)."""
    return _ids_reported_with(observation, listed_ids, _APPLIED_MARKERS)


# --------------------------------------------------------------------------
# Harvesting candidate targets out of the observed state
# --------------------------------------------------------------------------


def _is_plot(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("x"), int)
        and isinstance(value.get("y"), int)
        and not isinstance(value.get("x"), bool)
        and not isinstance(value.get("y"), bool)
    )


def _as_plot(value: Any) -> dict[str, int]:
    """A plot reduced to exactly the two coordinates a target is compared on.

    The values are the request's own; only the surrounding keys are dropped, so a plot harvested
    from a richer object (a unit's ``plot``, a path's ``destination``) is offered in the same
    shape T256's example shows.
    """
    return {"x": int(value["x"]), "y": int(value["y"])}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _expand(value: Any) -> Iterator[Any]:
    """*value* itself, plus its items when it is a list.

    An observed field named by a hint is as often the list (``reachable_plots``,
    ``available_promotions``) as it is the scalar (``unit_id``), so both readings are offered and
    the shape filter decides which one was meant.
    """
    yield value
    if isinstance(value, list):
        yield from value


class _ObservedIndex:
    """Every value in the observed state, indexed by the field name it was written under.

    Built once per call. ``under_selected`` records whether a value sat inside an object the game
    reports as selected (``is_selected: true``) -- the distinction a hint like "the selected
    unit's reachable_plots" is drawing, and the only way to prefer the unit a unit order will
    actually act on without consulting anything outside the request.
    """

    def __init__(self, observed: dict[str, Any]) -> None:
        self._fields: dict[str, list[_Candidate]] = {}
        self._plots: list[_Candidate] = []
        self._blocking_flagged = False
        self._prompt_screen_ids: list[str] = []
        self.entry_keys: frozenset[str] = frozenset(observed)
        for key, value in observed.items():
            self._walk(value, entry_key=key, under_selected=False)

    def _walk(self, node: Any, *, entry_key: str, under_selected: bool) -> None:
        if isinstance(node, dict):
            selected = under_selected or node.get("is_selected") is True
            if node.get(_BLOCKING_PROMPT_FIELD) is True:
                self._blocking_flagged = True
            if _is_plot(node):
                self._plots.append(
                    _Candidate(value=_as_plot(node), entry_key=entry_key, under_selected=selected)
                )
            for name, child in node.items():
                self._fields.setdefault(name, []).append(
                    _Candidate(value=child, entry_key=entry_key, under_selected=selected)
                )
                self._walk(child, entry_key=entry_key, under_selected=selected)
        elif isinstance(node, list):
            for item in node:
                self._walk(item, entry_key=entry_key, under_selected=under_selected)
        elif isinstance(node, str) and _PROMPT_SCREEN_RE.match(node):
            if node not in self._prompt_screen_ids:
                self._prompt_screen_ids.append(node)

    # -- the open prompt, if the request says one is blocking play --------------

    @property
    def prompt_options(self) -> tuple[str, ...]:
        """Every answer the request shows the open prompt offering, in the order it showed them."""
        return tuple(
            value
            for candidate in self.field(_PROMPT_OPTIONS_FIELD)
            for value in _expand(candidate.value)
            if isinstance(value, str) and value.strip()
        )

    @property
    def prompt_screen_ids(self) -> tuple[str, ...]:
        """Every ``prompt.*`` screen id the observed state names."""
        return tuple(self._prompt_screen_ids)

    @property
    def blocking_prompt(self) -> bool:
        """Whether the request says a prompt is blocking play right now.

        Any of the three signals is enough, per the live finding this preference was written for:
        the explicit ``has_blocking_prompt`` flag, a ``prompt.*`` screen id, or a non-empty list
        of offered options. An *empty* ``prompt_options`` is deliberately not a signal -- it
        offers nothing to answer with -- and an explicit ``has_blocking_prompt: false`` does not
        veto a ``prompt.*`` screen id: when the request contradicts itself the prompt answer is
        still tried, so the disagreement lands in the record as a refused decision rather than
        being resolved silently here.
        """
        return (
            self._blocking_flagged
            or bool(self._prompt_screen_ids)
            or bool(self.prompt_options)
        )

    def field(self, name: str) -> list[_Candidate]:
        return self._fields.get(name, [])

    @property
    def plots(self) -> list[_Candidate]:
        return list(self._plots)

    def has_field(self, name: str) -> bool:
        return name in self._fields


def _named_sources(guidance: str, index: _ObservedIndex) -> tuple[list[str], frozenset[str]]:
    """``(field names, observation keys)`` the rendered target guidance names.

    Both halves are read out of the prose the request already shows the model: "a unit_id from
    units.state" names the field ``unit_id`` and the entry ``units.state``; "an individual_id
    from great_people.state's recruitable_individuals" names two fields and one entry. Only
    identifiers that actually exist in this request's observed state are returned, so a hint
    naming a field the game did not report contributes nothing rather than a guess.
    """
    fields: list[str] = []
    entries: set[str] = set()
    for token in _IDENTIFIER_RE.findall(guidance):
        if token in index.entry_keys:
            entries.add(token)
            continue
        leaf = token.rsplit(".", 1)[-1]
        if index.has_field(leaf) and leaf not in fields:
            fields.append(leaf)
    return fields, frozenset(entries)


def _dedupe(values: Iterable[Any]) -> list[Any]:
    """Distinct values, order preserved, keyed by their JSON rendering (plots are dicts)."""
    seen: set[str] = set()
    unique: list[Any] = []
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def _collect(
    candidates: Sequence[_Candidate],
    *,
    entries: frozenset[str],
    prefer_selected: bool,
    accept: Callable[[Any], bool],
    convert: Callable[[Any], Any],
) -> list[Any]:
    """Shape-filter *candidates* down to usable target values.

    *entries* restricts to the observation entries the guidance named (when it named any);
    *prefer_selected* drops back to the full set only when nothing sat under a selected object,
    so "the selected unit's ..." is honoured when the game reports a selection and does not turn
    into "no target available" when it does not.
    """
    scoped = [c for c in candidates if not entries or c.entry_key in entries]
    if prefer_selected:
        selected = [c for c in scoped if c.under_selected]
        if selected:
            scoped = selected
    return _dedupe(
        convert(value)
        for candidate in scoped
        for value in _expand(candidate.value)
        if accept(value)
    )


def _target_candidates(action: _ListedAction, index: _ObservedIndex) -> list[Any]:
    """Every target value *this request shows* that fits *action*'s rendered example.

    Empty means "the request does not show a usable target for this action" -- the caller drops
    the action rather than inventing one. Never returns a value that was not read out of the
    request: the only literals ever taken from the guidance itself are an option's own offered
    values and a number's own stated range, both of which the request is *enumerating* rather
    than illustrating.
    """
    example = action.target_example
    guidance = action.target_guidance
    fields, entries = _named_sources(guidance, index)
    prefer_selected = "selected" in guidance.lower()
    named: list[_Candidate] = [c for name in fields for c in index.field(name)]

    if _is_plot(example):
        source = named if named else index.plots
        return _collect(
            source,
            entries=entries,
            prefer_selected=prefer_selected,
            accept=_is_plot,
            convert=_as_plot,
        )

    if isinstance(example, str):
        values = _collect(
            named,
            entries=entries,
            prefer_selected=prefer_selected,
            accept=lambda v: isinstance(v, str) and bool(v.strip()),
            convert=lambda v: v,
        )
        if values:
            return values
        if _OPTION_EXAMPLE_MARKER in guidance.lower():
            # The request is listing the options themselves (e.g. `"world" or "strategic"`), so
            # the quoted literals ARE what it shows as available -- not an illustration of one.
            return _dedupe(_QUOTED_RE.findall(action.target_hint))
        return []

    if _is_int(example):
        return _collect(
            named,
            entries=entries,
            prefer_selected=prefer_selected,
            accept=_is_int,
            convert=int,
        )

    if _is_number(example):
        # A number's range is stated in the hint (e.g. "from 0.05 (closest) to 1.0 (farthest)");
        # the example itself is only a shape, so it is never offered as a value.
        stated = _dedupe(float(text) for text in _NUMBER_RE.findall(action.target_hint))
        if stated:
            return stated
        return _collect(
            named,
            entries=entries,
            prefer_selected=prefer_selected,
            accept=_is_number,
            convert=float,
        )

    return []


# --------------------------------------------------------------------------
# Answering the prompt that is blocking play
# --------------------------------------------------------------------------


def _suffix(declaration_id: str) -> str:
    """``units.move_to`` -> ``move_to``; ``prompt.congress_vote`` -> ``congress_vote``."""
    _, _, tail = declaration_id.partition(".")
    return tail or declaration_id


def _looks_like_a_prompt_answer(action: _ListedAction) -> bool:
    """Whether *action* presents itself, in what the request rendered, as answering a prompt."""
    if action.declaration_id.startswith(_PROMPT_ACTION_PREFIX):
        return True
    summary = action.summary.lower()
    return any(marker in summary for marker in _PROMPT_SUMMARY_MARKERS)


def _matches_prompt(action: _ListedAction, screen_id: str) -> bool:
    """Whether *action*'s rendered id or summary answers the prompt *screen_id* names.

    Two tiers, both read off the request and nothing else. **Identity**: the screen id's suffix
    and the action id's suffix contain one another -- exact for most, and the reason
    ``prompt.diplomatic_approach`` still finds ``prompts.ai_diplomatic_approach``. **Prose**:
    every word of the screen id's suffix, stemmed to :data:`_WORD_STEM_LEN`, appears in the
    action's own id and summary text -- which is what bridges ``prompt.declare_war_response`` to
    a summary that says "declaration-of-war ... Respond".
    """
    wanted = _suffix(screen_id)
    mine = _suffix(action.declaration_id)
    if wanted in mine or mine in wanted:
        return True
    haystack = f"{action.declaration_id} {action.summary}".lower()
    words = [word for word in wanted.split("_") if len(word) >= 3]
    return bool(words) and all(word[:_WORD_STEM_LEN] in haystack for word in words)


def _prompt_answer_actions(
    actions: Sequence[_ListedAction], screen_ids: Sequence[str]
) -> list[_ListedAction]:
    """The listed actions to try first while a prompt is blocking play, best match first.

    When the request names which prompt is open, the actions matching it are returned alone --
    that is the whole point of the preference, and offering the rest alongside them would be the
    uniform draw again. When it does not (``has_blocking_prompt`` or bare options, with no screen
    id), every listed action that presents itself as a prompt answer is returned, which is still
    a far smaller set than the full catalog. Empty means nothing listed answers a prompt, and the
    caller falls back to the ordinary draw.
    """
    answers = [action for action in actions if _looks_like_a_prompt_answer(action)]
    if not answers:
        return []
    matched = [
        action
        for action in answers
        if any(_matches_prompt(action, screen_id) for screen_id in screen_ids)
    ]
    return matched if matched else answers


# --------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------


class StochasticModelProvider:
    """A ``ModelProvider`` that samples uniformly from what the request shows (T261).

    Satisfies :class:`~civsim_harness.provider.port.ModelProvider` structurally (it is a
    ``Protocol``, so no inheritance is needed), and is wired the same way every other adapter is:
    handed to ``run/composition.py``'s ``build_runner_dependencies(provider=...)``, where the
    ordinary ``ProviderChain`` wraps it.

    *seed* fixes the sampling stream and is kept on :attr:`seed` so the run that used it can be
    reported and repeated; *max_actions_per_turn* is how many non-end-turn decisions it makes
    before ending the turn; *policy* is one of :data:`PROVIDER_POLICIES` and is reported in
    ``model_served`` (``stochastic/uniform-v1`` or ``stochastic/coverage-v1``), so a recorded run
    always says which sampler produced it.
    """

    def __init__(
        self,
        *,
        seed: int = 0,
        max_actions_per_turn: int = DEFAULT_MAX_ACTIONS_PER_TURN,
        policy: str = UNIFORM_POLICY,
    ) -> None:
        if max_actions_per_turn < 0:
            raise ValueError("max_actions_per_turn cannot be negative")
        if policy not in PROVIDER_POLICIES:
            raise ValueError(
                f"unknown policy {policy!r}; expected one of {', '.join(PROVIDER_POLICIES)}"
            )
        self.seed = seed
        self.max_actions_per_turn = max_actions_per_turn
        self.policy = policy
        self._model_served = ModelRef(
            provider=STOCHASTIC_PROVIDER_NAME, model=STOCHASTIC_MODEL_NAME_BY_POLICY[policy]
        )
        self._rng = random.Random(seed)

        # Own memory only -- never a record, never the store. See the module docstring.
        self._chosen_ids_run: set[str] = set()
        self._applied_ids_run: set[str] = set()
        self._last_step_index: int | None = None
        self._actions_this_turn = 0
        self._pair_counts_turn: dict[tuple[str, str], int] = {}
        self._refused_ids_turn: set[str] = set()

    # -- ModelProvider protocol ---------------------------------------------

    def describe(self, model: ModelRef) -> ModelCapabilities:
        """Report this adapter's own capabilities (P1 chain preflight, FR-039).

        Independent of *model*: nothing about which model a configuration names changes what this
        adapter can carry, because it sends nothing anywhere. See
        :data:`_UNBOUNDED_CONTEXT_TOKENS`.
        """
        return ModelCapabilities(
            accepts_images=True,
            max_context_tokens=_UNBOUNDED_CONTEXT_TOKENS,
            max_images_per_request=None,
            confirmed=True,
        )

    def complete(self, request: DecisionRequest) -> DecisionResponse:
        """Serve exactly one decision for *request*'s single decision step.

        Never fails: there is no wire, no quota and no parse to go wrong, so every call comes
        back ``CallOutcome.DECISION_RETURNED``. ``request`` is read and never modified (the
        "Adapter obligations" the port contract names), and ``image_count`` is reported as the
        request's own, so no image can be recorded as dropped that was not.
        """
        started = time.perf_counter()
        decision = self._decide(request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return DecisionResponse(
            decision=decision,
            model_served=self._model_served,
            latency_ms=latency_ms,
            cost=ZERO_COST,
            retry_count=0,
            fallback_occurred=False,
            image_count=len(request.images),
            outcome=CallOutcome.DECISION_RETURNED,
        )

    # -- sampling -----------------------------------------------------------

    def _decide(self, request: DecisionRequest) -> RawDecision:
        self._sync_turn(request.step_index)

        observation = request.observation
        listed = _parse_listed_actions(observation)
        listed_ids = frozenset(action.declaration_id for action in listed)
        self._refused_ids_turn |= _refused_action_ids(observation, listed_ids)
        self._applied_ids_run |= _applied_action_ids(observation, listed_ids)

        end_turn = self._end_turn_action(listed)
        index = _ObservedIndex(_parse_observed_state(observation))
        pool = [action for action in listed if action is not end_turn]
        if self.policy == COVERAGE_POLICY:
            # The owner's rule, verbatim: "the stochastic testing should only select from actions
            # of a reachable state." An action the request shows greyed out is never drawn --
            # not as a last resort, not when nothing else is left. When this empties the pool the
            # turn is ended (below), and the end-turn decision is recorded like any other, so the
            # board that offered nothing is visible in the record rather than papered over with a
            # draw that was always going to be refused.
            pool = [action for action in pool if action.available]

        # 1. A prompt blocking play is the game demanding a response, not a move this provider
        #    chose to spend -- so it is answered before the action budget is even consulted, and
        #    a successful answer does not count against it. MEASURED twice in live play: sampling
        #    uniformly with a modal up took ~7 draws to find the one acknowledge and burned the
        #    block. The per-turn repeat bound still applies, so a prompt that will not clear
        #    cannot loop here: once every answer has been tried its two times, this falls through
        #    to the ordinary draw exactly as if no prompt had been detected.
        if index.blocking_prompt:
            preferred = _prompt_answer_actions(pool, index.prompt_screen_ids)
            decision = self._sample(
                preferred,
                index,
                listed_count=len(listed),
                prompt_options=index.prompt_options,
                answers_prompt=True,
            )
            if decision is not None:
                return decision
            tried = {id(action) for action in preferred}
            pool = [action for action in pool if id(action) not in tried]

        # 2. The turn's own action budget.
        if self._actions_this_turn >= self.max_actions_per_turn:
            return self._end_turn_decision(
                end_turn,
                reason=(
                    f"this provider takes at most {self.max_actions_per_turn} action(s) per "
                    f"turn and has taken {self._actions_this_turn}"
                ),
            )

        # 3. The ordinary uniform draw.
        decision = self._sample(
            pool, index, listed_count=len(listed), prompt_options=(), answers_prompt=False
        )
        if decision is not None:
            return decision

        scope = (
            f"none of the {len(listed_ids)} action(s) this request lists is still usable this turn"
            if self.policy != COVERAGE_POLICY
            else (
                f"none of the action(s) this request shows as available now (of the "
                f"{len(listed_ids)} it lists) is still usable this turn"
            )
        )
        return self._end_turn_decision(
            end_turn,
            reason=(
                f"{scope} -- each either has no target the request shows or has already been "
                "repeated to this turn's bound"
            ),
        )

    def _sample(
        self,
        pool: Sequence[_ListedAction],
        index: _ObservedIndex,
        *,
        listed_count: int,
        prompt_options: Sequence[str],
        answers_prompt: bool,
    ) -> RawDecision | None:
        """One decision drawn from *pool*, or ``None`` when nothing in it is usable.

        *answers_prompt* marks the blocking-prompt path: the decision it produces clears a block
        the game imposed rather than spending the turn's action budget, and *prompt_options* --
        the open prompt's own offered answers, when the request shows them -- is where its target
        comes from.
        """
        remaining = list(pool)
        while remaining:
            action = self._weighted_pick(remaining)
            remaining.remove(action)
            target: Any | None = None
            if action.target_example is not None:
                allowed = [
                    value
                    for value in self._candidates(action, index, prompt_options)
                    if self._pair_count(action.declaration_id, value) < MAX_REPEATS_PER_TURN
                ]
                if not allowed:
                    # Either the request shows no target of the required shape, or every one it
                    # shows has already hit this turn's repeat bound. Both mean "pick a different
                    # action" -- never "make one up" (see the module docstring).
                    continue
                target = self._rng.choice(allowed)
            elif self._pair_count(action.declaration_id, None) >= MAX_REPEATS_PER_TURN:
                continue
            return self._action_decision(
                action, target, listed_count=listed_count, answers_prompt=answers_prompt
            )
        return None

    @staticmethod
    def _candidates(
        action: _ListedAction, index: _ObservedIndex, prompt_options: Sequence[str]
    ) -> list[Any]:
        """Target values for *action*, preferring the open prompt's own offered answers.

        A prompt answer's target is an option, so when the request shows the prompt's
        ``prompt_options`` those *are* what it may be -- more specific than the general harvest,
        and the thing the rendered hint ("one of the open prompt's offered options") points at.
        Anything else (and an action whose target is not option-shaped) goes through the ordinary
        derivation, so a prompt whose options the request does not show still falls back to what
        it does show rather than to nothing.
        """
        if prompt_options and isinstance(action.target_example, str):
            return _dedupe(prompt_options)
        return _target_candidates(action, index)

    def _sync_turn(self, step_index: int) -> None:
        """Reset the per-turn memory when *step_index* says a new turn (or attempt) has begun.

        ``run/decision_loop.py`` starts every turn attempt at ``step_index = 1`` and increments
        from there, so a step index that is 1, or that has gone backwards, is exactly the
        harness's own turn boundary -- read from the request rather than assumed from call count,
        which keeps a retried call from being mistaken for a new turn.
        """
        if self._last_step_index is None or step_index <= 1 or step_index < self._last_step_index:
            self._actions_this_turn = 0
            self._pair_counts_turn = {}
            self._refused_ids_turn = set()
        self._last_step_index = step_index

    def _weighted_pick(self, pool: Sequence[_ListedAction]) -> _ListedAction:
        """One action out of *pool*, by whichever policy this provider was built with."""
        if self.policy == COVERAGE_POLICY:
            return self._coverage_pick(pool)
        weights = [
            (REFUSED_ACTION_WEIGHT if action.declaration_id in self._refused_ids_turn else 1.0)
            * (ALREADY_CHOSEN_WEIGHT if action.declaration_id in self._chosen_ids_run else 1.0)
            for action in pool
        ]
        return self._rng.choices(list(pool), weights=weights, k=1)[0]

    def _coverage_pick(self, pool: Sequence[_ListedAction]) -> _ListedAction:
        """Uniform within the least-covered tier of *pool*, which is already available-only.

        The preference the coverage policy exists for is "something this run has not landed yet",
        and it is a *strict* tier rather than a weight: the uniform policy's mild bias needed
        hundreds of draws to walk the surface, and a block of live play is a few dozen. Two facts
        order the tiers, both from this provider's own memory and nothing else:

        1. whether a later request reported this action **applied** (:data:`_APPLIED_MARKERS`) --
           the outcome half, and the thing the owner's coverage question is actually about;
        2. whether this provider has **chosen** it at all this run -- its own record of what it
           returned, which is the honest stand-in while nothing renders outcomes back, and which
           on its own already walks the available surface rather than re-rolling one corner of it.

        ``(not yet applied, not yet chosen)`` sorts first, then ``(not yet applied, chosen)``,
        and the actions already known to have landed come last -- "then the rest uniformly".
        """
        tiers: dict[tuple[bool, bool], list[_ListedAction]] = {}
        for action in pool:
            key = (
                action.declaration_id in self._applied_ids_run,
                action.declaration_id in self._chosen_ids_run,
            )
            tiers.setdefault(key, []).append(action)
        return self._rng.choice(tiers[min(tiers)])

    @staticmethod
    def _pair_key(declaration_id: str, target: Any | None) -> tuple[str, str]:
        return declaration_id, json.dumps(target, sort_keys=True, default=str)

    def _pair_count(self, declaration_id: str, target: Any | None) -> int:
        return self._pair_counts_turn.get(self._pair_key(declaration_id, target), 0)

    def _end_turn_action(self, listed: Sequence[_ListedAction]) -> _ListedAction | None:
        """Whichever listed action the request itself presents as the end turn.

        Matched first by :data:`END_TURN_DECLARATION_ID` and otherwise by the summary's own
        wording, so a catalog that renames the declaration still ends its turns.
        """
        for action in listed:
            if action.declaration_id == str(END_TURN_DECLARATION_ID):
                return action
        for action in listed:
            if "end the current turn" in action.summary.lower():
                return action
        return None

    def _action_decision(
        self,
        action: _ListedAction,
        target: Any | None,
        *,
        listed_count: int,
        answers_prompt: bool,
    ) -> RawDecision:
        if not answers_prompt:
            # Clearing a block the game imposed is not this provider spending a move of its own,
            # so only an ordinary draw counts against the turn's action budget.
            self._actions_this_turn += 1
        self._chosen_ids_run.add(action.declaration_id)
        key = self._pair_key(action.declaration_id, target)
        self._pair_counts_turn[key] = self._pair_counts_turn.get(key, 0) + 1
        parameters: dict[str, Any] = {} if target is None else {"target": target}
        target_note = (
            "the request shows this action takes no target"
            if target is None
            else f"target sampled from what the request shows: {json.dumps(target, default=str)}"
        )
        if answers_prompt:
            drawn_from = (
                "sampled from the action(s) this request shows as answering the prompt currently "
                f"blocking play, out of the {listed_count} it lists"
            )
        elif self.policy == COVERAGE_POLICY:
            drawn_from = (
                "sampled from the action(s) this request shows in its 'available now' group, out "
                f"of the {listed_count} it lists, preferring what this run has not landed yet"
            )
        else:
            drawn_from = (
                f"sampled uniformly from the {listed_count} action(s) this request lists as "
                "available right now"
            )
        return RawDecision(
            action_declaration_id=DeclarationId(action.declaration_id),
            reasoning=(
                f"stochastic provider (seed={self.seed}, policy={self.policy}): "
                f"{action.declaration_id} {drawn_from}; "
                f"{target_note}. No model was consulted and no information outside this request "
                "was read."
            ),
            parameters=parameters,
            is_end_turn=False,
        )

    def _end_turn_decision(self, action: _ListedAction | None, *, reason: str) -> RawDecision:
        declaration_id = (
            DeclarationId(action.declaration_id) if action is not None else END_TURN_DECLARATION_ID
        )
        self._chosen_ids_run.add(str(declaration_id))
        key = self._pair_key(str(declaration_id), None)
        self._pair_counts_turn[key] = self._pair_counts_turn.get(key, 0) + 1
        return RawDecision(
            action_declaration_id=declaration_id,
            reasoning=(
                f"stochastic provider (seed={self.seed}, policy={self.policy}): ending the turn "
                f"because {reason}. "
                "No model was consulted and no information outside this request was read."
            ),
            parameters={},
            is_end_turn=True,
        )


__all__ = [
    "ALREADY_CHOSEN_WEIGHT",
    "COVERAGE_POLICY",
    "DEFAULT_MAX_ACTIONS_PER_TURN",
    "END_TURN_DECLARATION_ID",
    "MAX_REPEATS_PER_TURN",
    "PROVIDER_POLICIES",
    "REFUSED_ACTION_WEIGHT",
    "STOCHASTIC_MODEL_NAME",
    "STOCHASTIC_MODEL_NAME_BY_POLICY",
    "STOCHASTIC_MODEL_REF",
    "STOCHASTIC_PROVIDER_NAME",
    "UNIFORM_POLICY",
    "ZERO_COST",
    "StochasticModelProvider",
]
