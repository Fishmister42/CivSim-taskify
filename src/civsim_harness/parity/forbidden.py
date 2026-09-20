"""The forbidden-field guard (T128, research R9 design rule 5).

Everything upstream of this module (the catalog's load-time validation, the
structural filter in :mod:`civsim_harness.parity.filter`, the four screening
gates in :mod:`civsim_harness.parity.screening`) is a *preventive* control:
each makes a class of leak structurally hard to produce in the first place.
This module is the *detective* control the research calls "belt-and-braces":
a red-team list asserted against the context that is actually about to leave
the harness, right before it does, so a leak that slipped past every
preventive control still fails loudly instead of silently reaching the
agent.

Call :func:`enforce_parity_boundary` **once per decision step**, on the
fully assembled context for that step -- not once per turn, since a turn may
run many steps and each one gets its own fresh
:class:`~civsim_harness.models.turn.Observation` (FR-008, FR-015). It never
edits or strips anything: FR-025/FR-030 and this guard's own rationale both
say *withhold*/*reject*, never "pass through with the bad part removed" --
silently editing a leak would hide the very defect SC-006/SC-008 exist to
surface.

Two independent techniques, matching the two categories FR-019 and FR-020
name:

- **Structural (key-name) scanning** (:func:`scan_observation_entries`) --
  walks every :class:`~civsim_harness.models.turn.ObservationEntry`'s key and
  value (recursively, since a value may itself be a nested mapping/list) and
  flags any key whose normalized tokens match a known game-state-leakage or
  harness-telemetry vocabulary. This is a heuristic, keyword-based net: it
  cannot know every way a future catalog entry might misname a leaked field,
  so it is deliberately generous rather than narrowly tuned, and it is the
  *second* line of defence -- the first is that ``ObservationEntry`` values
  can only be produced by :func:`civsim_harness.parity.filter.filter_to_entries`
  from a declared capability at all.
- **Literal (value) scanning** (:func:`find_literal_leaks` /
  :func:`assert_no_literal_leaks`) -- checks whether specific, concrete
  values (a real model name, a real save name, a real RNG seed, ...) appear
  verbatim anywhere in a block of free text, e.g. the fully rendered
  ``DecisionRequest.system``/``.observation`` a provider call would actually
  receive. This is what a red-team test built from a realistic transcript
  uses to assert a *specific* forbidden value never reached the surface that
  matters (research R15, T122).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from civsim_harness.errors import ParityViolation
from civsim_harness.models.common import DeclarationId
from civsim_harness.models.turn import Observation
from civsim_harness.provider.port import DecisionRequest

# --------------------------------------------------------------------------
# Categories (mirrors FR-019 / FR-020 exactly)
# --------------------------------------------------------------------------


class ForbiddenCategory(StrEnum):
    """Which half of the parity boundary a finding violates."""

    GAME_STATE_LEAKAGE = "game_state_leakage"  # FR-019
    HARNESS_TELEMETRY = "harness_telemetry"  # FR-020


@dataclass(frozen=True)
class Violation:
    """One red-team finding."""

    category: ForbiddenCategory
    location: str
    description: str
    declaration_id: DeclarationId | None = None


# --------------------------------------------------------------------------
# Structural (key-name) scanning
# --------------------------------------------------------------------------

# Splits on any non-alphanumeric run, and inserts a split at a lower-to-upper
# transition, so "hidden_ai_intent", "hiddenAiIntent", and "Hidden AI Intent"
# all normalize to the same token set.
_TOKEN_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _tokenize(key: str) -> frozenset[str]:
    return frozenset(token.lower() for token in _TOKEN_SPLIT_RE.split(key) if token)


@dataclass(frozen=True)
class _ForbiddenPhrase:
    category: ForbiddenCategory
    tokens: frozenset[str]
    description: str


def _phrase(category: ForbiddenCategory, description: str, *tokens: str) -> _ForbiddenPhrase:
    return _ForbiddenPhrase(category=category, tokens=frozenset(tokens), description=description)


# FR-019: game-state leakage. Each phrase is a bag of tokens that must ALL be
# present (in any order/adjacency) in a normalized key for it to match --
# this is deliberately generous (a heuristic net, not the primary defence;
# see module docstring) but bagged-token matching keeps innocuous compounds
# like "owner_player_id" or "available_promotions" (present in the real
# catalog, see catalogs/observations/units.yaml) from tripping a single-word
# ban on "player" or "available".
_GAME_STATE_PHRASES: tuple[_ForbiddenPhrase, ...] = (
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "RNG state", "rng"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "RNG/map seed", "seed"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "debug data", "debug"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "provenance data", "provenance"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "unrevealed map contents", "unrevealed"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "undisclosed opponent state", "undisclosed"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "hidden state or intent", "hidden"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "opponent internal state", "internal"),
    _phrase(
        ForbiddenCategory.GAME_STATE_LEAKAGE,
        "opponent's undisclosed research",
        "opponent",
        "research",
    ),
    _phrase(
        ForbiddenCategory.GAME_STATE_LEAKAGE, "opponent's undisclosed civics", "opponent", "civic"
    ),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "hidden AI intent", "ai", "intent"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "hidden AI strategy", "ai", "strategy"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "cheat/debug backdoor", "cheat"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "cheat/debug backdoor", "godmode"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "omniscient (fog-bypassing) view", "omniscient"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "fog-of-war bypass", "fog", "bypass"),
    _phrase(ForbiddenCategory.GAME_STATE_LEAKAGE, "raw Lua passthrough", "raw", "lua"),
)

# FR-020: harness operational telemetry presented as game information.
_HARNESS_TELEMETRY_PHRASES: tuple[_ForbiddenPhrase, ...] = (
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "model identity", "model"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "call cost", "cost"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "token accounting", "tokens"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "call latency", "latency"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "retry count", "retry"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "retry count", "retries"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "provider fallback", "fallback"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "save lineage", "lineage"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "run configuration", "run", "config"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "run configuration", "run", "configuration"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "run configuration", "config", "id"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "wall-clock timing", "wall", "clock"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "wall-clock timing", "started", "at"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "wall-clock timing", "ended", "at"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "game build", "game", "build"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "harness client identity", "client", "identity"),
    _phrase(ForbiddenCategory.HARNESS_TELEMETRY, "host platform telemetry", "host", "platform"),
)

_ALL_PHRASES: tuple[_ForbiddenPhrase, ...] = _GAME_STATE_PHRASES + _HARNESS_TELEMETRY_PHRASES


def _matches(tokens: frozenset[str]) -> list[_ForbiddenPhrase]:
    return [phrase for phrase in _ALL_PHRASES if phrase.tokens <= tokens]


def _key_violations(
    *, declaration_id: DeclarationId | None, key: str, location: str
) -> list[Violation]:
    return [
        Violation(
            category=phrase.category,
            location=location,
            description=f"key {key!r} looks like {phrase.description}",
            declaration_id=declaration_id,
        )
        for phrase in _matches(_tokenize(key))
    ]


def _scan_nested(
    *, declaration_id: DeclarationId | None, value: Any, path: str
) -> list[Violation]:
    """Recurse into a structured value, flagging any nested field name that looks forbidden.

    ``observe/assemble.py`` keys every ``ObservationEntry`` by its own
    ``declaration_id`` (one entry per capability result) and carries that
    capability's *whole* decoded value in ``ObservationEntry.value`` -- so a
    leaked field (an ``rng_seed`` buried inside a ``map.state`` result, say)
    would show up as a nested key inside ``value``, not as the entry's own
    top-level ``key``. This is why the scan below recurses through mappings
    and sequences rather than only inspecting the entry's own key.
    """
    violations: list[Violation] = []
    if isinstance(value, Mapping):
        for sub_key, sub_value in value.items():
            child_path = f"{path}.{sub_key}"
            violations.extend(
                _key_violations(
                    declaration_id=declaration_id, key=str(sub_key), location=child_path
                )
            )
            violations.extend(
                _scan_nested(declaration_id=declaration_id, value=sub_value, path=child_path)
            )
    elif isinstance(value, Sequence) and not isinstance(value, str):
        for index, item in enumerate(value):
            violations.extend(
                _scan_nested(declaration_id=declaration_id, value=item, path=f"{path}[{index}]")
            )
    return violations


def scan_observation_entries(observation: Observation) -> list[Violation]:
    """Structurally scan every entry's key and nested value fields for forbidden fields.

    This is the FR-019/FR-020 key-name net described in the module docstring.
    Returns an empty list for a clean observation.
    """
    violations: list[Violation] = []
    for entry in observation.entries:
        base_path = str(entry.declaration_id)
        violations.extend(
            _key_violations(declaration_id=entry.declaration_id, key=entry.key, location=base_path)
        )
        violations.extend(
            _scan_nested(declaration_id=entry.declaration_id, value=entry.value, path=base_path)
        )
    return violations


# --------------------------------------------------------------------------
# Literal (value) scanning
# --------------------------------------------------------------------------


def find_literal_leaks(text: str, forbidden_values: Iterable[Any]) -> list[str]:
    """Return which of *forbidden_values* appear verbatim as a substring of *text*.

    Values are stringified before comparison; empty/whitespace-only values
    are ignored (they would match everything and tell an auditor nothing).
    Used to check a specific, concrete value -- a real model name, a real
    save name, a real RNG seed planted in a realistic transcript fixture --
    never reached rendered context text (research R15, T122/T126).
    """
    hits: list[str] = []
    for value in forbidden_values:
        rendered = str(value)
        if rendered.strip() and rendered in text:
            hits.append(rendered)
    return hits


def assert_no_literal_leaks(
    text: str,
    forbidden_values: Iterable[Any],
    *,
    context_label: str,
) -> None:
    """Raise :class:`~civsim_harness.errors.ParityViolation` if any forbidden value leaked.

    ``detail`` deliberately omits the leaked values themselves -- the point
    of this guard is to fail loudly and be fixed, not to smuggle the very
    values it caught into an exception payload/log line (the redaction
    filter would also catch credential-shaped ones, but a game-state or
    telemetry value has no reason to look credential-shaped at all).
    """
    hits = find_literal_leaks(text, forbidden_values)
    if hits:
        raise ParityViolation(
            f"{context_label} contains forbidden literal value(s) (FR-019, FR-020)",
            detail={"context_label": context_label, "hit_count": len(hits)},
        )


# --------------------------------------------------------------------------
# The runtime guard entry point (T128)
# --------------------------------------------------------------------------


def enforce_parity_boundary(
    *,
    observation: Observation,
    decision_request: DecisionRequest | None = None,
    extra_forbidden_values: Iterable[Any] = (),
) -> None:
    """Assert the fully assembled context for one decision step is parity-clean.

    Call this exactly once per decision step, on the ``Observation`` just
    assembled and (once a provider call is being made) the
    ``DecisionRequest`` built from it -- never once per turn, since each step
    gets its own fresh observation (FR-008, FR-015).

    Raises :class:`~civsim_harness.errors.ParityViolation` on the first
    category of finding (structural or literal); never edits or strips
    content. ``extra_forbidden_values`` lets a caller add run-specific
    literal values it knows must never appear (e.g. this run's own model
    name, save name, or RNG seed) on top of the fixed structural scan.
    """
    violations = scan_observation_entries(observation)
    if violations:
        _raise(violations)

    if decision_request is not None:
        combined_text = f"{decision_request.system}\n{decision_request.observation}"
        literal_hits = find_literal_leaks(combined_text, extra_forbidden_values)
        if literal_hits:
            raise ParityViolation(
                "assembled decision request contains forbidden literal value(s) "
                "(FR-019, FR-020)",
                detail={"hit_count": len(literal_hits)},
            )


def _raise(violations: list[Violation]) -> None:
    by_category: dict[ForbiddenCategory, int] = {}
    for violation in violations:
        by_category[violation.category] = by_category.get(violation.category, 0) + 1
    raise ParityViolation(
        "assembled context violates the human-parity boundary (FR-019, FR-020)",
        detail={
            "count": len(violations),
            "by_category": {str(category): count for category, count in by_category.items()},
            "locations": [violation.location for violation in violations][:20],
        },
    )
