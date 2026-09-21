"""The live-coverage scorecard: claimed harness surface versus what the store attests.

The question this answers is the owner's, verbatim: *"I have yet to see >5% of the claimed
functionality of 002."* Nothing here is hand-counted. The **claimed** surface is loaded through
the harness's own catalog loader (:func:`civsim_harness.capability.loader.load_catalog`) and
through ``lua/ingame/screens.lua``'s own tables -- the single sources of truth for "what the
harness says it can do". The **demonstrated** surface is read back out of the match store through
:class:`~civsim_harness.store.contract.MatchTrackingStore` and nothing else, so every number
below is an assertion about records a live client actually produced.

Five categories, each with the same shape (claimed set, demonstrated subset, evidence, and an
explicit never-demonstrated list):

- **actions** -- one row per ``kind: action`` declaration. *Demonstrated* means at least one
  ``ExecutionOutcome.APPLIED`` on record; *attempted* means the agent issued it and the harness
  recorded an outcome of any kind. The two are reported separately because they are wildly
  different claims, and conflating them is exactly how a coverage number lies.

  T262 splits *attempted* once more, for the same reason. An attempt the dispatcher refused as
  ``unavailable_to_human_now`` never reached the client at all: the agent chose a command the
  game was not offering, which a human does not do because the button is greyed out. That is
  **drawn while unavailable**, and it is not evidence about the harness -- it is evidence about
  how the action was chosen. *Attempts while available* is the difference, and it is what
  "Actions attempted while available: N of 38" counts. MEASURED (2026-09-21): of the fourteen
  catalog actions attempted but never applied, nine had *only* ever been drawn while
  unavailable, and the old headline counted every one of them as an attempt.
- **observations** -- one row per ``kind: observation`` declaration. *Demonstrated* means it
  produced a non-empty value at at least one decision step (:func:`_is_non_empty`).
- **views** -- one row per ``kind: view`` declaration: the capture targets. Counted from the
  ``ScreenCapture`` records themselves, split into delivered / withheld-with-reason.
- **screens and prompts** -- the screen ids ``lua/ingame/screens.lua``'s identity mapping can
  report, counted from ``Observation.screen_identity``; plus the Lua *watchlist* states, counted
  from the ``game.screen_state`` observation's own ``raw_screen_id`` and from
  ``unknown_screen`` run events (the recorded ``UnknownScreenEncountered`` stalls).
- **images** -- captures recorded, captures delivered to the agent, and the decisive number:
  decision steps whose ``ModelCall.image_count`` was greater than zero. A capture record proves
  a capture was *attempted*; only the model call proves a picture reached the model.

**Attempts, not just authoritative attempts.** Coverage walks every recorded attempt of every
turn, abandoned ones included: an action applied during an attempt that was later replayed still
ran on the live client, and pretending otherwise would undercount what was demonstrated.
Completeness auditing is a different question, and ``operator/audit.py`` already owns it.

**What the store cannot attest to is said, not omitted** (:class:`UnattestedSurface`). A claimed
screen id that no Lua state maps to can never be reported by the probe at all, so no run will
ever demonstrate it, and a percentage that silently counts it as "not yet" is misleading. Those
are derived here, from the same two sources, rather than annotated by hand.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civsim_harness.capability.loader import Catalog, load_catalog
from civsim_harness.errors import StoreReadError
from civsim_harness.models.catalog import DeclarationKind
from civsim_harness.models.common import RunId
from civsim_harness.models.decision import DecisionTrigger, ExecutionOutcome
from civsim_harness.models.records import RunEventType
from civsim_harness.models.run import Run
from civsim_harness.models.turn import ScreeningStatus
from civsim_harness.observe.screen_identity import PROMPT_SCREEN_PREFIX, UNKNOWN_SCREEN
from civsim_harness.store.contract import MatchTrackingStore

__all__ = [
    "UNAVAILABLE_REFUSAL_REASON",
    "ActionCoverage",
    "ClaimedSurface",
    "CoverageScorecard",
    "Evidence",
    "Headline",
    "ImageCoverage",
    "ObservationCoverage",
    "RunCoverage",
    "ScreenCoverage",
    "ScreenSurface",
    "UnattestedSurface",
    "ViewCoverage",
    "WatchedStateCoverage",
    "compute_coverage",
    "load_claimed_surface",
    "load_screen_surface",
    "render_json",
    "render_markdown",
    "select_runs",
]

#: ``catalogs/`` sits next to ``lua/`` in the repository, so the screen-identity source resolves
#: from the catalog root the caller already had to name. Spelled once, here.
SCREENS_LUA_RELATIVE = Path("lua") / "ingame" / "screens.lua"

#: The declaration id whose recorded value carries ``raw_screen_id`` -- the Lua watchlist state
#: the probe actually found open. Same well-known id ``observe/reader.py`` routes on.
SCREEN_STATE_DECLARATION_ID = "game.screen_state"


# --------------------------------------------------------------------------
# The claimed surface
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenSurface:
    """What ``lua/ingame/screens.lua`` claims about screens, read from the file's own tables.

    ``screen_ids`` is every id the identity probe is able to report as *recognised*:
    ``CIVSIM_KNOWN_SCREENS``, the keys of ``CIVSIM_SCREEN_ID_BY_STATE``, and any ``screen = "..."``
    literal the probe returns directly (that is where ``world`` comes from -- what the probe
    answers at the plain world view, where no watchlisted state reports open, so no entry in
    ``CIVSIM_SCREEN_ID_BY_STATE`` names it).
    ``watched_states`` is ``CIVSIM_SCREEN_WATCHLIST``, the Lua state names the probe scans, and
    ``state_by_screen_id`` is T253's ``CIVSIM_SCREEN_ID_BY_STATE`` -- the mapping that decides
    whether an open state has a catalog identity at all or stalls the run as an unknown screen.
    ``direct_screen_ids`` are the ids the probe answers with *without* consulting that mapping,
    so an id in it is reachable even though no Lua state maps to it.

    ``source`` is ``None`` when the file could not be read; the scorecard then reports the screen
    category as unattestable rather than as zero.
    """

    screen_ids: tuple[str, ...] = ()
    watched_states: tuple[str, ...] = ()
    state_by_screen_id: Mapping[str, str] = field(default_factory=dict)
    direct_screen_ids: tuple[str, ...] = ()
    source: Path | None = None

    @property
    def screen_id_by_state(self) -> Mapping[str, str]:
        return {state: screen for screen, state in self.state_by_screen_id.items()}


@dataclass(frozen=True)
class ClaimedSurface:
    """Everything the harness claims it can do, from the catalog and the screen mapping."""

    action_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    view_ids: tuple[str, ...]
    screens: ScreenSurface
    catalog_version: str
    catalog_content_hash: str
    catalog_root: Path
    #: screen id -> the catalog action whose ``availability_predicate`` names it. Derived from
    #: the predicate text rather than from the ``prompt.x`` / ``prompts.x`` naming convention,
    #: because the convention is not universal (``prompt.diplomatic_approach`` answers to
    #: ``prompts.ai_diplomatic_approach``) and the predicate is the binding that actually gates
    #: the action at runtime.
    prompt_action_by_screen: Mapping[str, str] = field(default_factory=dict)


_LUA_COMMENT = re.compile(r"--[^\n]*")
_LUA_SCREEN_LITERAL = re.compile(r"\bscreen\s*=\s*\"([^\"]+)\"")
_LUA_STRING = re.compile(r"\"([^\"]+)\"")
_LUA_PAIR = re.compile(
    r"(?:\[\s*\"(?P<quoted>[^\"]+)\"\s*\]|(?P<bare>\w+))\s*=\s*\"(?P<value>[^\"]+)\""
)

#: ``game.current_screen == "prompt.x"`` inside a catalog action's ``availability_predicate``.
_CURRENT_SCREEN_PREDICATE = re.compile(r"game\.current_screen\s*==\s*\"([^\"]+)\"")


def _lua_table_body(source: str, name: str) -> str | None:
    """The body of ``local <name> = { ... }``, comments stripped.

    A deliberately small reader: the harness ships this Lua to a game it cannot run headlessly,
    so the file itself is the only source of truth for these three tables and there is no Python
    mirror to import. Comment stripping is line-based and would mis-handle a ``--`` inside a Lua
    string literal; ``screens.lua`` contains none, and a mis-read would only ever shrink the
    *claimed* surface, never invent a demonstrated one.
    """
    match = re.search(rf"local\s+{re.escape(name)}\s*=\s*\{{(.*?)\n\}}", source, re.DOTALL)
    if match is None:
        return None
    return _LUA_COMMENT.sub("", match.group(1))


def load_screen_surface(screens_lua: Path) -> ScreenSurface:
    """Read the screen-identity surface out of ``lua/ingame/screens.lua``.

    Returns an empty surface (``source=None``) when the file is absent or carries none of the
    expected tables -- the caller reports that as "cannot attest", never as zero coverage.
    """
    try:
        source = screens_lua.read_text(encoding="utf-8")
    except OSError:
        return ScreenSurface()

    known_body = _lua_table_body(source, "CIVSIM_KNOWN_SCREENS")
    watchlist_body = _lua_table_body(source, "CIVSIM_SCREEN_WATCHLIST")
    mapping_body = _lua_table_body(source, "CIVSIM_SCREEN_ID_BY_STATE")
    if known_body is None and watchlist_body is None and mapping_body is None:
        return ScreenSurface()

    state_by_screen_id: dict[str, str] = {}
    if mapping_body is not None:
        for pair in _LUA_PAIR.finditer(mapping_body):
            screen_id = pair.group("quoted") or pair.group("bare")
            state_by_screen_id[screen_id] = pair.group("value")

    direct = set(_LUA_SCREEN_LITERAL.findall(_LUA_COMMENT.sub("", source)))
    direct.discard(UNKNOWN_SCREEN)
    screen_ids = set(_LUA_STRING.findall(known_body or ""))
    screen_ids.update(state_by_screen_id)
    screen_ids.update(direct)
    screen_ids.discard(UNKNOWN_SCREEN)

    return ScreenSurface(
        screen_ids=tuple(sorted(screen_ids)),
        watched_states=tuple(_LUA_STRING.findall(watchlist_body or "")),
        state_by_screen_id=dict(sorted(state_by_screen_id.items())),
        direct_screen_ids=tuple(sorted(direct)),
        source=screens_lua,
    )


def load_claimed_surface(
    catalog_root: Path, *, screens_lua: Path | None = None
) -> ClaimedSurface:
    """Load the claimed surface: the catalog through its own loader, plus the screen mapping."""
    catalog = load_catalog(catalog_root)
    lua_path = (
        screens_lua if screens_lua is not None else catalog_root.parent / SCREENS_LUA_RELATIVE
    )
    return claimed_surface_from_catalog(catalog, load_screen_surface(lua_path))


def claimed_surface_from_catalog(catalog: Catalog, screens: ScreenSurface) -> ClaimedSurface:
    """The :class:`ClaimedSurface` for an already-loaded catalog (the seam tests bind to)."""

    def ids_of(kind: DeclarationKind) -> tuple[str, ...]:
        return tuple(
            sorted(
                str(declaration.declaration_id)
                for declaration in catalog.declarations.values()
                if declaration.kind is kind
            )
        )

    prompt_action_by_screen: dict[str, str] = {}
    for declaration in sorted(catalog.declarations.values(), key=lambda d: str(d.declaration_id)):
        if declaration.kind is not DeclarationKind.ACTION:
            continue
        predicate = declaration.availability_predicate or ""
        for screen_id in _CURRENT_SCREEN_PREDICATE.findall(predicate):
            prompt_action_by_screen.setdefault(screen_id, str(declaration.declaration_id))

    if screens.source is not None:
        named = set(screens.screen_ids) | set(prompt_action_by_screen)
        screens = ScreenSurface(
            screen_ids=tuple(sorted(named)),
            watched_states=screens.watched_states,
            state_by_screen_id=screens.state_by_screen_id,
            direct_screen_ids=screens.direct_screen_ids,
            source=screens.source,
        )

    return ClaimedSurface(
        action_ids=ids_of(DeclarationKind.ACTION),
        observation_ids=ids_of(DeclarationKind.OBSERVATION),
        view_ids=ids_of(DeclarationKind.VIEW),
        screens=screens,
        catalog_version=catalog.version.version,
        catalog_content_hash=catalog.version.content_hash,
        catalog_root=catalog.root,
        prompt_action_by_screen=dict(sorted(prompt_action_by_screen.items())),
    )


# --------------------------------------------------------------------------
# Per-surface coverage rows
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """Where in the record something happened: the address an operator can go and look at."""

    run_id: str
    turn: int
    step: int
    attempt: int = 0

    def __str__(self) -> str:
        suffix = "" if self.attempt == 0 else f" (attempt {self.attempt})"
        return f"{self.run_id} t{self.turn}/s{self.step}{suffix}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "turn": self.turn,
            "step": self.step,
            "attempt": self.attempt,
        }


#: The dispatch-time refusal that means "the agent chose a command the game was not offering"
#: (``models/decision.py``'s ``RejectionReason.UNAVAILABLE_TO_HUMAN_NOW``). A human does not
#: make that choice, because the button is greyed out, so counting it as an *attempt at the
#: action* overstates the surface that has really been exercised. Spelled once, here.
UNAVAILABLE_REFUSAL_REASON = "unavailable_to_human_now"


@dataclass(frozen=True)
class ActionCoverage:
    """One catalog action, and what the store says it ever did.

    **Attempts, and attempts that could have worked (T262).** ``attempts`` counts every time the
    agent issued this action; ``drawn_while_unavailable`` is the subset the dispatcher refused as
    :data:`UNAVAILABLE_REFUSAL_REASON` -- the agent chose a command the game was not offering, and
    the action never reached the client. ``attempts_while_available`` is the difference, and it is
    the honest number: it counts only the times this action was actually on the board when it was
    chosen, so whether it then applied says something about the harness rather than about the
    sampler's aim. MEASURED (2026-09-21): nine of the fourteen actions that had been attempted but
    never applied had *only* ever been drawn while unavailable -- the old headline counted every
    one of them as a demonstrated attempt.
    """

    declaration_id: str
    applied: int = 0
    rejected: int = 0
    partially_applied: int = 0
    refusals_by_reason: Mapping[str, int] = field(default_factory=dict)
    first_applied: Evidence | None = None
    last_applied: Evidence | None = None
    first_attempted: Evidence | None = None
    prompt_responses: int = 0

    @property
    def attempts(self) -> int:
        """Every time the agent issued this action, whatever came of it."""
        return self.applied + self.rejected + self.partially_applied

    @property
    def drawn_while_unavailable(self) -> int:
        """Attempts refused at dispatch because the game was not offering this command."""
        return self.refusals_by_reason.get(UNAVAILABLE_REFUSAL_REASON, 0)

    @property
    def attempts_while_available(self) -> int:
        """Attempts made while the command was on the board -- never negative by construction."""
        return self.attempts - self.drawn_while_unavailable

    @property
    def demonstrated(self) -> bool:
        """Applied at least once on a live client -- the only claim worth making."""
        return self.applied > 0

    @property
    def only_drawn_while_unavailable(self) -> bool:
        """Issued at least once, and never once while the game was offering it."""
        return self.attempts > 0 and self.attempts_while_available == 0


@dataclass(frozen=True)
class ObservationCoverage:
    """One catalog observation declaration, and the steps at which it carried anything."""

    declaration_id: str
    steps_present: int = 0
    steps_with_value: int = 0
    first_with_value: Evidence | None = None

    @property
    def demonstrated(self) -> bool:
        return self.steps_with_value > 0


@dataclass(frozen=True)
class ViewCoverage:
    """One catalog view declaration, counted from the ``ScreenCapture`` records naming it."""

    declaration_id: str
    captures: int = 0
    delivered: int = 0
    withheld: int = 0
    withheld_by_reason: Mapping[str, int] = field(default_factory=dict)

    @property
    def demonstrated(self) -> bool:
        """A view is demonstrated when its image actually reached the agent, not when a capture
        of it was merely attempted and withheld."""
        return self.delivered > 0


@dataclass(frozen=True)
class ScreenCoverage:
    """One screen id the identity mapping can report."""

    screen_id: str
    encountered_steps: int = 0
    prompt_responses: int = 0
    mapped_state: str | None = None
    is_prompt: bool = False
    prompt_action_id: str | None = None
    first_encountered: Evidence | None = None

    @property
    def demonstrated(self) -> bool:
        return self.encountered_steps > 0


@dataclass(frozen=True)
class WatchedStateCoverage:
    """One ``CIVSIM_SCREEN_WATCHLIST`` Lua state: seen open, and whether it has a catalog id."""

    state_name: str
    mapped_screen_id: str | None = None
    observed_open: int = 0
    unknown_screen_events: int = 0

    @property
    def demonstrated(self) -> bool:
        return self.observed_open > 0 or self.unknown_screen_events > 0


@dataclass(frozen=True)
class ImageCoverage:
    """The capture/image path, end to end: attempted, screened, delivered."""

    steps: int = 0
    steps_with_image: int = 0
    images_sent: int = 0
    captures_recorded: int = 0
    captures_delivered: int = 0
    captures_withheld: int = 0
    withheld_by_reason: Mapping[str, int] = field(default_factory=dict)
    capture_paths: Mapping[str, int] = field(default_factory=dict)
    capture_failed_events: int = 0
    image_withheld_events: int = 0


@dataclass(frozen=True)
class RunCoverage:
    """One run's own line of the scorecard."""

    run_id: str
    lifecycle_state: str
    started_at: str | None
    models_served: tuple[str, ...]
    turns_recorded: int
    highest_turn: int
    attempts: int
    steps: int
    model_calls: int
    cost_usd: float | None
    record_completeness: str
    has_gaps: bool
    comparability: str
    capture_path: str
    actions_applied: int
    unknown_screen_events: int


@dataclass(frozen=True)
class UnattestedSurface:
    """A claimed surface the store structurally cannot speak to, and the record that is missing."""

    surface: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"surface": self.surface, "reason": self.reason}


@dataclass(frozen=True)
class Headline:
    """One headline line: ``label: demonstrated of total (pct%)``, plus its own note."""

    label: str
    demonstrated: int
    total: int
    note: str = ""

    @property
    def percent(self) -> float:
        return 0.0 if self.total == 0 else 100.0 * self.demonstrated / self.total

    @property
    def summary(self) -> str:
        return f"{self.label}: {self.demonstrated} of {self.total} ({self.percent:.1f}%)"

    def __str__(self) -> str:
        return f"{self.summary} -- {self.note}" if self.note else self.summary

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "demonstrated": self.demonstrated,
            "total": self.total,
            "percent": round(self.percent, 1),
            "note": self.note,
        }


# --------------------------------------------------------------------------
# The scorecard
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageScorecard:
    """The whole answer, in one value: claimed versus demonstrated, with its evidence."""

    generated_at: datetime
    store_path: str
    store_schema_version: str
    catalog_version: str
    catalog_content_hash: str
    runs_in_scope: int
    runs_in_store: int
    actions: tuple[ActionCoverage, ...]
    observations: tuple[ObservationCoverage, ...]
    views: tuple[ViewCoverage, ...]
    screens: tuple[ScreenCoverage, ...]
    watched_states: tuple[WatchedStateCoverage, ...]
    images: ImageCoverage
    runs: tuple[RunCoverage, ...]
    unattested: tuple[UnattestedSurface, ...]
    unclaimed_actions: Mapping[str, int] = field(default_factory=dict)
    unclaimed_observations: Mapping[str, int] = field(default_factory=dict)
    unclaimed_screens: Mapping[str, int] = field(default_factory=dict)
    screen_surface_available: bool = True

    # -- headline numbers -------------------------------------------------

    def headlines(self) -> tuple[Headline, ...]:
        lines = [
            Headline(
                "Actions demonstrated live",
                sum(1 for row in self.actions if row.demonstrated),
                len(self.actions),
                "applied at least once on the client",
            ),
            Headline(
                "Actions ever attempted",
                sum(1 for row in self.actions if row.attempts > 0),
                len(self.actions),
                "issued by the agent, applied or refused",
            ),
            Headline(
                "Actions attempted while available",
                sum(1 for row in self.actions if row.attempts_while_available > 0),
                len(self.actions),
                "issued while the game was actually offering the command -- an attempt refused "
                f"as {UNAVAILABLE_REFUSAL_REASON} is counted as drawn while unavailable, not as "
                "an attempt at the action",
            ),
            Headline(
                "Observations demonstrated live",
                sum(1 for row in self.observations if row.demonstrated),
                len(self.observations),
                "produced a non-empty value at a decision step",
            ),
            Headline(
                "Views (capture targets) demonstrated",
                sum(1 for row in self.views if row.demonstrated),
                len(self.views),
                "an image of this view reached the agent",
            ),
        ]
        if self.screen_surface_available:
            lines.append(
                Headline(
                    "Screens/prompts encountered",
                    sum(1 for row in self.screens if row.demonstrated),
                    len(self.screens),
                    "reported by the screen-identity probe at a decision step",
                )
            )
            lines.append(
                Headline(
                    "Watched screen states seen open",
                    sum(1 for row in self.watched_states if row.demonstrated),
                    len(self.watched_states),
                    "found open by the watchlist probe, or named by a stall",
                )
            )
        lines.append(
            Headline(
                "Images delivered to the agent",
                self.images.steps_with_image,
                self.images.steps,
                f"{self.images.captures_recorded} captures recorded, "
                f"{self.images.captures_delivered} delivered, "
                f"{self.images.captures_withheld} withheld",
            )
        )
        return tuple(lines)

    def never_demonstrated(self) -> dict[str, tuple[str, ...]]:
        return {
            "actions": tuple(row.declaration_id for row in self.actions if not row.demonstrated),
            "actions_attempted_never_applied": tuple(
                row.declaration_id
                for row in self.actions
                if not row.demonstrated and row.attempts > 0
            ),
            "actions_never_attempted": tuple(
                row.declaration_id for row in self.actions if row.attempts == 0
            ),
            # T262: issued, but never once while the game was offering the command. These are
            # not evidence about the harness at all -- they are evidence about how the action
            # was chosen, and the two must not be read as the same number.
            "actions_only_drawn_while_unavailable": tuple(
                row.declaration_id for row in self.actions if row.only_drawn_while_unavailable
            ),
            "observations": tuple(
                row.declaration_id for row in self.observations if not row.demonstrated
            ),
            "views": tuple(row.declaration_id for row in self.views if not row.demonstrated),
            "screens": tuple(row.screen_id for row in self.screens if not row.demonstrated),
            "watched_states": tuple(
                row.state_name for row in self.watched_states if not row.demonstrated
            ),
        }


# --------------------------------------------------------------------------
# Computation
# --------------------------------------------------------------------------


def _is_non_empty(value: Any) -> bool:
    """Whether a recorded observation value counts as "it produced something".

    ``None``, an empty string and an empty collection are nothing; every other value -- including
    ``0`` and ``False``, which are real readings -- is something.
    """
    if value is None:
        return False
    if isinstance(value, str | bytes):
        return len(value) > 0
    if isinstance(value, Mapping | list | tuple | set):
        return len(value) > 0
    return True


def _run_order_key(run: Run) -> tuple[int, str, str]:
    """Chronological, NULL ``started_at`` last -- so "first seen" means what it says."""
    if run.started_at is None:
        return (1, "", str(run.run_id))
    return (0, run.started_at.isoformat(), str(run.run_id))


def select_runs(
    store: MatchTrackingStore,
    *,
    run_ids: Sequence[str] = (),
    since_run_id: str | None = None,
) -> list[Run]:
    """The runs in scope, chronologically ascending.

    ``run_ids`` names an explicit set (a missing id is a :class:`StoreReadError`, never silently
    dropped). ``since_run_id`` selects that run and every run started at or after it -- the delta
    a live lane wants for one block of work. A run with no ``started_at`` cannot be placed on that
    timeline, so it is left out of a ``--since`` window unless it is the named run itself.
    """
    every = sorted(store.list_runs(), key=_run_order_key)
    if run_ids:
        by_id = {str(run.run_id): run for run in every}
        missing = [run_id for run_id in run_ids if run_id not in by_id]
        if missing:
            raise StoreReadError(
                "coverage: no such run in this store", detail={"run_ids": ", ".join(missing)}
            )
        selected = [by_id[run_id] for run_id in dict.fromkeys(run_ids)]
        return sorted(selected, key=_run_order_key)
    if since_run_id is not None:
        anchor = next((run for run in every if str(run.run_id) == since_run_id), None)
        if anchor is None:
            raise StoreReadError(
                "coverage: no such run in this store", detail={"run_ids": since_run_id}
            )
        if anchor.started_at is None:
            raise StoreReadError(
                "coverage: --since needs a run with a started_at to anchor the window",
                detail={"run_id": since_run_id},
            )
        started = anchor.started_at
        return [
            run
            for run in every
            if str(run.run_id) == since_run_id
            or (run.started_at is not None and run.started_at >= started)
        ]
    return every


class _Tally:
    """Mutable accumulators, turned into the frozen scorecard rows at the end."""

    def __init__(self) -> None:
        self.applied: Counter[str] = Counter()
        self.rejected: Counter[str] = Counter()
        self.partial: Counter[str] = Counter()
        self.refusals: dict[str, Counter[str]] = {}
        self.prompt_responses: Counter[str] = Counter()
        self.first_applied: dict[str, Evidence] = {}
        self.last_applied: dict[str, Evidence] = {}
        self.first_attempted: dict[str, Evidence] = {}
        self.obs_present: Counter[str] = Counter()
        self.obs_value: Counter[str] = Counter()
        self.obs_first: dict[str, Evidence] = {}
        self.screen_steps: Counter[str] = Counter()
        self.screen_first: dict[str, Evidence] = {}
        self.prompt_type_steps: Counter[str] = Counter()
        self.raw_state_steps: Counter[str] = Counter()
        self.unknown_state_events: Counter[str] = Counter()
        self.view_captures: Counter[str] = Counter()
        self.view_delivered: Counter[str] = Counter()
        self.view_withheld: Counter[str] = Counter()
        self.view_reasons: dict[str, Counter[str]] = {}
        self.withheld_reasons: Counter[str] = Counter()
        self.capture_paths: Counter[str] = Counter()
        self.steps = 0
        self.steps_with_image = 0
        self.images_sent = 0
        self.captures_recorded = 0
        self.captures_delivered = 0
        self.captures_withheld = 0
        self.capture_failed_events = 0
        self.image_withheld_events = 0

    def note_action(
        self, declaration_id: str, outcome: ExecutionOutcome, reason: str | None, where: Evidence
    ) -> None:
        self.first_attempted.setdefault(declaration_id, where)
        if outcome is ExecutionOutcome.APPLIED:
            self.applied[declaration_id] += 1
            self.first_applied.setdefault(declaration_id, where)
            self.last_applied[declaration_id] = where
        elif outcome is ExecutionOutcome.PARTIALLY_APPLIED:
            self.partial[declaration_id] += 1
        else:
            self.rejected[declaration_id] += 1
            bucket = self.refusals.setdefault(declaration_id, Counter())
            bucket[reason or "unspecified"] += 1


def compute_coverage(
    store: MatchTrackingStore,
    claimed: ClaimedSurface,
    *,
    run_ids: Sequence[str] = (),
    since_run_id: str | None = None,
    store_path: str | Path | None = None,
    now: datetime | None = None,
) -> CoverageScorecard:
    """Score *claimed* against everything *store* records for the runs in scope.

    Every read goes through :class:`~civsim_harness.store.contract.MatchTrackingStore`. The store
    is never written to and never asked for a blob: a capture's *record* is what attests whether
    its image was delivered or withheld and why, and the ``ModelCall`` attests whether a picture
    was actually sent. *store_path* is only a label for the report; when omitted the store's own
    ``store_id`` identifies the file instead.
    """
    info = store.store_info()
    runs = select_runs(store, run_ids=run_ids, since_run_id=since_run_id)
    tally = _Tally()
    run_rows: list[RunCoverage] = []

    for run in runs:
        run_rows.append(_walk_run(store, run, tally))

    return _assemble(
        claimed=claimed,
        tally=tally,
        run_rows=tuple(run_rows),
        store_path=str(store_path) if store_path is not None else f"store_id {info.store_id}",
        store_schema_version=str(info.schema_version),
        runs_in_store=int(info.counts.get("runs", len(runs))),
        now=now or datetime.now(UTC),
    )


def _walk_run(store: MatchTrackingStore, run: Run, tally: _Tally) -> RunCoverage:
    run_id = RunId(str(run.run_id))
    highest = store.highest_recorded_turn(run_id)
    turns_recorded = 0
    attempts = 0
    steps = 0
    actions_applied = 0

    for turn in range(1, highest + 1):
        summaries = store.list_turn_attempts(run_id, turn)
        if summaries:
            turns_recorded += 1
        for summary in summaries:
            record = store.get_turn_cycle_attempt(run_id, turn, summary.attempt_index)
            if record is None:  # pragma: no cover - a listed attempt always loads
                continue
            attempts += 1
            for bundle in record.steps:
                steps += 1
                where = Evidence(
                    run_id=str(run.run_id),
                    turn=turn,
                    step=bundle.step.step_index,
                    attempt=summary.attempt_index,
                )
                decision = bundle.decision
                action_id = str(decision.action_declaration_id)
                execution = decision.execution
                reason = (
                    execution.rejection_reason.value
                    if execution.rejection_reason is not None
                    else None
                )
                tally.note_action(action_id, execution.outcome, reason, where)
                if execution.outcome is ExecutionOutcome.APPLIED:
                    actions_applied += 1
                if decision.trigger is DecisionTrigger.PROMPT_RESPONSE:
                    tally.prompt_responses[action_id] += 1
                    if decision.prompt_type:
                        tally.prompt_type_steps[decision.prompt_type] += 1

                observation = bundle.observation
                seen: set[str] = set()
                for entry in observation.entries:
                    declaration_id = str(entry.declaration_id)
                    if declaration_id not in seen:
                        seen.add(declaration_id)
                        tally.obs_present[declaration_id] += 1
                    if _is_non_empty(entry.value):
                        tally.obs_value[declaration_id] += 1
                        tally.obs_first.setdefault(declaration_id, where)
                    if declaration_id == SCREEN_STATE_DECLARATION_ID:
                        raw = _raw_screen_id(entry.value)
                        if raw:
                            tally.raw_state_steps[raw] += 1

                screen = observation.screen_identity
                if screen:
                    tally.screen_steps[screen] += 1
                    tally.screen_first.setdefault(screen, where)

                image_count = bundle.model_call.image_count
                tally.steps += 1
                tally.images_sent += image_count
                if image_count > 0:
                    tally.steps_with_image += 1

    for capture in store.list_captures(run_id):
        view_id = str(capture.view_declaration_id)
        tally.captures_recorded += 1
        tally.view_captures[view_id] += 1
        tally.capture_paths[capture.capture_path.value] += 1
        if capture.screening_status is ScreeningStatus.WITHHELD:
            tally.captures_withheld += 1
            tally.view_withheld[view_id] += 1
            reason = capture.withheld_reason.value if capture.withheld_reason else "unspecified"
            tally.withheld_reasons[reason] += 1
            tally.view_reasons.setdefault(view_id, Counter())[reason] += 1
        if capture.shown_to_agent:
            tally.captures_delivered += 1
            tally.view_delivered[view_id] += 1

    unknown_events = 0
    for event in store.list_run_events(run_id):
        if event.event_type is RunEventType.UNKNOWN_SCREEN:
            unknown_events += 1
            raw = event.detail.get("raw_screen_id")
            if isinstance(raw, str) and raw:
                tally.unknown_state_events[raw] += 1
        elif event.event_type is RunEventType.CAPTURE_FAILED:
            tally.capture_failed_events += 1
        elif event.event_type is RunEventType.IMAGE_WITHHELD:
            tally.image_withheld_events += 1

    totals = store.model_call_totals(run_id)
    served = sorted(
        {
            f"{row.call.model_served.provider}/{row.call.model_served.model}"
            for row in store.list_model_calls(run_id)
        }
    )
    completeness = store.record_completeness(run_id)
    return RunCoverage(
        run_id=str(run.run_id),
        lifecycle_state=run.lifecycle_state.value,
        started_at=run.started_at.isoformat() if run.started_at else None,
        models_served=tuple(served),
        turns_recorded=turns_recorded,
        highest_turn=highest,
        attempts=attempts,
        steps=steps,
        model_calls=totals.call_count,
        cost_usd=totals.cost_usd,
        record_completeness=completeness.value,
        has_gaps=completeness.value == "has_gaps",
        comparability=run.comparability_status.value,
        capture_path=run.capture_path.value,
        actions_applied=actions_applied,
        unknown_screen_events=unknown_events,
    )


def _raw_screen_id(value: Any) -> str | None:
    """``game.screen_state``'s ``raw_screen_id`` -- the Lua watchlist state the probe found."""
    if not isinstance(value, Mapping):
        return None
    raw = value.get("raw_screen_id")
    return raw if isinstance(raw, str) and raw else None


def _assemble(
    *,
    claimed: ClaimedSurface,
    tally: _Tally,
    run_rows: tuple[RunCoverage, ...],
    store_path: str,
    store_schema_version: str,
    runs_in_store: int,
    now: datetime,
) -> CoverageScorecard:
    actions = tuple(
        ActionCoverage(
            declaration_id=action_id,
            applied=tally.applied[action_id],
            rejected=tally.rejected[action_id],
            partially_applied=tally.partial[action_id],
            refusals_by_reason=dict(sorted(tally.refusals.get(action_id, Counter()).items())),
            first_applied=tally.first_applied.get(action_id),
            last_applied=tally.last_applied.get(action_id),
            first_attempted=tally.first_attempted.get(action_id),
            prompt_responses=tally.prompt_responses[action_id],
        )
        for action_id in claimed.action_ids
    )
    observations = tuple(
        ObservationCoverage(
            declaration_id=observation_id,
            steps_present=tally.obs_present[observation_id],
            steps_with_value=tally.obs_value[observation_id],
            first_with_value=tally.obs_first.get(observation_id),
        )
        for observation_id in claimed.observation_ids
    )
    views = tuple(
        ViewCoverage(
            declaration_id=view_id,
            captures=tally.view_captures[view_id],
            delivered=tally.view_delivered[view_id],
            withheld=tally.view_withheld[view_id],
            withheld_by_reason=dict(sorted(tally.view_reasons.get(view_id, Counter()).items())),
        )
        for view_id in claimed.view_ids
    )
    state_by_screen = claimed.screens.state_by_screen_id
    screens = tuple(
        ScreenCoverage(
            screen_id=screen_id,
            encountered_steps=tally.screen_steps[screen_id],
            prompt_responses=tally.prompt_type_steps[screen_id],
            mapped_state=state_by_screen.get(screen_id),
            is_prompt=screen_id.startswith(PROMPT_SCREEN_PREFIX),
            prompt_action_id=claimed.prompt_action_by_screen.get(screen_id),
            first_encountered=tally.screen_first.get(screen_id),
        )
        for screen_id in claimed.screens.screen_ids
    )
    screen_id_by_state = claimed.screens.screen_id_by_state
    watched_states = tuple(
        WatchedStateCoverage(
            state_name=state,
            mapped_screen_id=screen_id_by_state.get(state),
            observed_open=tally.raw_state_steps[state],
            unknown_screen_events=tally.unknown_state_events[state],
        )
        for state in claimed.screens.watched_states
    )
    images = ImageCoverage(
        steps=tally.steps,
        steps_with_image=tally.steps_with_image,
        images_sent=tally.images_sent,
        captures_recorded=tally.captures_recorded,
        captures_delivered=tally.captures_delivered,
        captures_withheld=tally.captures_withheld,
        withheld_by_reason=dict(sorted(tally.withheld_reasons.items())),
        capture_paths=dict(sorted(tally.capture_paths.items())),
        capture_failed_events=tally.capture_failed_events,
        image_withheld_events=tally.image_withheld_events,
    )
    claimed_actions = set(claimed.action_ids)
    claimed_observations = set(claimed.observation_ids) | set(claimed.view_ids)
    claimed_screens = set(claimed.screens.screen_ids)
    unclaimed_actions = {
        action_id: count
        for action_id, count in sorted(
            (tally.applied + tally.rejected + tally.partial).items()
        )
        if action_id not in claimed_actions
    }
    unclaimed_observations = {
        declaration_id: count
        for declaration_id, count in sorted(tally.obs_present.items())
        if declaration_id not in claimed_observations
    }
    unclaimed_screens = {
        screen_id: count
        for screen_id, count in sorted(tally.screen_steps.items())
        if screen_id not in claimed_screens and screen_id != UNKNOWN_SCREEN
    }
    return CoverageScorecard(
        generated_at=now,
        store_path=store_path,
        store_schema_version=store_schema_version,
        catalog_version=claimed.catalog_version,
        catalog_content_hash=claimed.catalog_content_hash,
        runs_in_scope=len(run_rows),
        runs_in_store=runs_in_store,
        actions=actions,
        observations=observations,
        views=views,
        screens=screens,
        watched_states=watched_states,
        images=images,
        runs=run_rows,
        unattested=_unattested(claimed, screens, images, run_rows),
        unclaimed_actions=unclaimed_actions,
        unclaimed_observations=unclaimed_observations,
        unclaimed_screens=unclaimed_screens,
        screen_surface_available=claimed.screens.source is not None,
    )


def _unattested(
    claimed: ClaimedSurface,
    screens: Sequence[ScreenCoverage],
    images: ImageCoverage,
    run_rows: Sequence[RunCoverage],
) -> tuple[UnattestedSurface, ...]:
    """Claimed surface no run could ever demonstrate, derived rather than annotated."""
    findings: list[UnattestedSurface] = []
    if claimed.screens.source is None:
        findings.append(
            UnattestedSurface(
                "screens and prompts",
                "lua/ingame/screens.lua was not readable, so the claimed screen surface is "
                "unknown; no screen percentage is reported",
            )
        )
    else:
        direct = set(claimed.screens.direct_screen_ids)
        for row in screens:
            # Reachable if a Lua state maps to it, if the probe answers it directly, or -- the
            # empirical proof -- if the store already recorded a step where it was up.
            if row.mapped_state is not None or row.screen_id in direct or row.demonstrated:
                continue
            detail = (
                "CIVSIM_SCREEN_ID_BY_STATE maps no Lua state to it, so game.screen_state can "
                "never report it and no record can exist"
            )
            if row.prompt_action_id is not None:
                detail += f"; {row.prompt_action_id} can therefore never become available"
            findings.append(UnattestedSurface(f"screen {row.screen_id}", detail))
    if images.captures_recorded > 0 and images.captures_delivered == 0:
        findings.append(
            UnattestedSurface(
                "capture/image path",
                f"{images.captures_recorded} capture records exist and every one is withheld "
                f"({_join_counts(images.withheld_by_reason)}); no ScreenCapture on record has "
                "shown_to_agent set, so no image has ever reached the agent",
            )
        )
    if run_rows and all(row.cost_usd is None for row in run_rows):
        findings.append(
            UnattestedSurface(
                "model spend",
                "no ModelCall in scope reported an amount_usd, so cost is unattested (the "
                "provider returned no pricing, not that the run was free)",
            )
        )
    return tuple(findings)


def _join_counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items()) or "no reason recorded"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _pct(part: int, total: int) -> str:
    return "0.0%" if total == 0 else f"{100.0 * part / total:.1f}%"


def _evidence(where: Evidence | None) -> str:
    return str(where) if where is not None else "-"


def _reasons(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{name} x{count}" for name, count in counts.items()) or "-"


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _inline_list(ids: Sequence[str]) -> str:
    return ", ".join(f"`{name}`" for name in ids) if ids else "_(none)_"


def render_markdown(scorecard: CoverageScorecard) -> str:
    """The scorecard as Markdown, ready to paste into a GitHub issue."""
    never = scorecard.never_demonstrated()
    out: list[str] = ["# CivSim live coverage scorecard", ""]
    out.append(
        f"Store `{scorecard.store_path}` (schema {scorecard.store_schema_version}) · "
        f"catalog `{scorecard.catalog_version}` "
        f"(`{scorecard.catalog_content_hash[:12]}`) · "
        f"{scorecard.runs_in_scope} of {scorecard.runs_in_store} runs in scope · "
        f"generated {scorecard.generated_at.isoformat()}"
    )
    out.append("")
    out.append(
        "Claimed surface is loaded from the catalog and `lua/ingame/screens.lua`; demonstrated "
        "surface is read from the match store only. Every recorded attempt of every turn is "
        "counted, abandoned attempts included."
    )
    out.append("")
    for headline in scorecard.headlines():
        suffix = f" -- {headline.note}" if headline.note else ""
        out.append(f"- **{headline.summary}**{suffix}")
    out.append("")

    out.append("## Actions")
    out.append("")
    active = [row for row in scorecard.actions if row.attempts > 0]
    if active:
        out.extend(
            _table(
                [
                    "action",
                    "applied",
                    "refused",
                    "drawn while unavailable",
                    "attempts while available",
                    "first applied",
                    "last applied",
                    "refusals by reason",
                ],
                [
                    [
                        f"`{row.declaration_id}`",
                        str(row.applied),
                        str(row.rejected + row.partially_applied),
                        str(row.drawn_while_unavailable),
                        str(row.attempts_while_available),
                        _evidence(row.first_applied),
                        _evidence(row.last_applied),
                        _reasons(row.refusals_by_reason),
                    ]
                    for row in active
                ],
            )
        )
    else:
        out.append("_No action has ever been issued on this store's record._")
    out.append("")

    out.append("## Observations")
    out.append("")
    obs_active = [row for row in scorecard.observations if row.steps_present > 0]
    if obs_active:
        out.extend(
            _table(
                ["observation", "steps present", "steps with a value", "first with a value"],
                [
                    [
                        f"`{row.declaration_id}`",
                        str(row.steps_present),
                        str(row.steps_with_value),
                        _evidence(row.first_with_value),
                    ]
                    for row in obs_active
                ],
            )
        )
    else:
        out.append("_No observation has ever been recorded on this store's record._")
    out.append("")

    out.append("## Views and the capture/image path")
    out.append("")
    out.extend(
        _table(
            ["view", "captures", "delivered", "withheld", "withheld by reason"],
            [
                [
                    f"`{row.declaration_id}`",
                    str(row.captures),
                    str(row.delivered),
                    str(row.withheld),
                    _reasons(row.withheld_by_reason),
                ]
                for row in scorecard.views
            ],
        )
    )
    out.append("")
    images = scorecard.images
    out.append(
        f"Decision steps: {images.steps} · steps that received an image: "
        f"{images.steps_with_image} ({_pct(images.steps_with_image, images.steps)}) · "
        f"images sent to the model: {images.images_sent} · capture paths: "
        f"{_reasons(images.capture_paths)} · `capture_failed` events: "
        f"{images.capture_failed_events} · `image_withheld` events: "
        f"{images.image_withheld_events}"
    )
    out.append("")

    if scorecard.screen_surface_available:
        out.append("## Screens and prompts")
        out.append("")
        out.extend(
            _table(
                ["screen id", "mapped Lua state", "steps encountered", "prompt responses"],
                [
                    [
                        f"`{row.screen_id}`",
                        f"`{row.mapped_state}`" if row.mapped_state else "_unmapped_",
                        str(row.encountered_steps),
                        str(row.prompt_responses),
                    ]
                    for row in scorecard.screens
                ],
            )
        )
        out.append("")
        watched_active = [row for row in scorecard.watched_states if row.demonstrated]
        if watched_active:
            out.append("Watchlist states actually seen:")
            out.append("")
            out.extend(
                _table(
                    ["Lua state", "mapped screen id", "seen open", "unknown-screen stalls"],
                    [
                        [
                            f"`{row.state_name}`",
                            f"`{row.mapped_screen_id}`" if row.mapped_screen_id else "_unmapped_",
                            str(row.observed_open),
                            str(row.unknown_screen_events),
                        ]
                        for row in watched_active
                    ],
                )
            )
            out.append("")

    out.append("## Runs")
    out.append("")
    out.extend(
        _table(
            [
                "run",
                "state",
                "model(s) served",
                "turns",
                "steps",
                "calls",
                "cost usd",
                "applied",
                "completeness",
            ],
            [
                [
                    f"`{row.run_id}`",
                    row.lifecycle_state,
                    ", ".join(row.models_served) or "-",
                    f"{row.turns_recorded}/{row.highest_turn}",
                    str(row.steps),
                    str(row.model_calls),
                    f"{row.cost_usd:.6f}" if row.cost_usd is not None else "unpriced",
                    str(row.actions_applied),
                    row.record_completeness,
                ]
                for row in scorecard.runs
            ],
        )
    )
    out.append("")

    out.append("## Never demonstrated")
    out.append("")
    tried = [row for row in scorecard.actions if not row.demonstrated and row.attempts > 0]
    untried = [row.declaration_id for row in scorecard.actions if row.attempts == 0]
    out.append(
        f"**Actions attempted but never applied ({len(tried)})**: "
        + (
            ", ".join(
                f"`{row.declaration_id}` ({row.attempts} refused"
                + (
                    f", {row.drawn_while_unavailable} of them drawn while unavailable)"
                    if row.drawn_while_unavailable
                    else ")"
                )
                for row in tried
            )
            or "_(none)_"
        )
    )
    out.append("")
    out.append(f"**Actions never even attempted ({len(untried)})**: {_inline_list(untried)}")
    out.append("")
    only_unavailable = never["actions_only_drawn_while_unavailable"]
    out.append(
        f"**Actions only ever drawn while unavailable ({len(only_unavailable)})**: "
        f"{_inline_list(only_unavailable)} -- issued, but never once while the game was "
        "offering the command, so the dispatcher refused them before they reached the client. "
        "These say nothing about the harness; they say the action was chosen off a board that "
        "did not offer it."
    )
    out.append("")
    out.append(
        f"**Observations ({len(never['observations'])})**: {_inline_list(never['observations'])}"
    )
    out.append("")
    out.append(f"**Views ({len(never['views'])})**: {_inline_list(never['views'])}")
    out.append("")
    if scorecard.screen_surface_available:
        out.append(f"**Screens ({len(never['screens'])})**: {_inline_list(never['screens'])}")
        out.append("")
        out.append(
            f"**Watched states ({len(never['watched_states'])})**: "
            f"{_inline_list(never['watched_states'])}"
        )
        out.append("")

    if scorecard.unattested:
        out.append("## What the store cannot attest to")
        out.append("")
        for finding in scorecard.unattested:
            out.append(f"- **{finding.surface}** -- {finding.reason}")
        out.append("")

    extras: list[str] = []
    if scorecard.unclaimed_actions:
        extras.append(f"actions not in the catalog: {_reasons(scorecard.unclaimed_actions)}")
    if scorecard.unclaimed_observations:
        extras.append(
            f"observations not in the catalog: {_reasons(scorecard.unclaimed_observations)}"
        )
    if scorecard.unclaimed_screens:
        extras.append(
            f"screen ids the mapping does not claim: {_reasons(scorecard.unclaimed_screens)}"
        )
    if extras:
        out.append("## Recorded but never claimed")
        out.append("")
        for line in extras:
            out.append(f"- {line}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_json(scorecard: CoverageScorecard) -> dict[str, Any]:
    """The same content as :func:`render_markdown`, as a JSON-serialisable mapping."""
    never = scorecard.never_demonstrated()
    return {
        "generated_at": scorecard.generated_at.isoformat(),
        "store_path": scorecard.store_path,
        "store_schema_version": scorecard.store_schema_version,
        "catalog_version": scorecard.catalog_version,
        "catalog_content_hash": scorecard.catalog_content_hash,
        "runs_in_scope": scorecard.runs_in_scope,
        "runs_in_store": scorecard.runs_in_store,
        "screen_surface_available": scorecard.screen_surface_available,
        "headlines": [headline.as_dict() for headline in scorecard.headlines()],
        "actions": [
            {
                "declaration_id": row.declaration_id,
                "demonstrated": row.demonstrated,
                "applied": row.applied,
                "rejected": row.rejected,
                "partially_applied": row.partially_applied,
                "attempts": row.attempts,
                "drawn_while_unavailable": row.drawn_while_unavailable,
                "attempts_while_available": row.attempts_while_available,
                "only_drawn_while_unavailable": row.only_drawn_while_unavailable,
                "prompt_responses": row.prompt_responses,
                "refusals_by_reason": dict(row.refusals_by_reason),
                "first_applied": row.first_applied.as_dict() if row.first_applied else None,
                "last_applied": row.last_applied.as_dict() if row.last_applied else None,
                "first_attempted": row.first_attempted.as_dict() if row.first_attempted else None,
            }
            for row in scorecard.actions
        ],
        "observations": [
            {
                "declaration_id": row.declaration_id,
                "demonstrated": row.demonstrated,
                "steps_present": row.steps_present,
                "steps_with_value": row.steps_with_value,
                "first_with_value": (
                    row.first_with_value.as_dict() if row.first_with_value else None
                ),
            }
            for row in scorecard.observations
        ],
        "views": [
            {
                "declaration_id": row.declaration_id,
                "demonstrated": row.demonstrated,
                "captures": row.captures,
                "delivered": row.delivered,
                "withheld": row.withheld,
                "withheld_by_reason": dict(row.withheld_by_reason),
            }
            for row in scorecard.views
        ],
        "screens": [
            {
                "screen_id": row.screen_id,
                "demonstrated": row.demonstrated,
                "encountered_steps": row.encountered_steps,
                "prompt_responses": row.prompt_responses,
                "mapped_state": row.mapped_state,
                "is_prompt": row.is_prompt,
                "prompt_action_id": row.prompt_action_id,
                "first_encountered": (
                    row.first_encountered.as_dict() if row.first_encountered else None
                ),
            }
            for row in scorecard.screens
        ],
        "watched_states": [
            {
                "state_name": row.state_name,
                "mapped_screen_id": row.mapped_screen_id,
                "observed_open": row.observed_open,
                "unknown_screen_events": row.unknown_screen_events,
                "demonstrated": row.demonstrated,
            }
            for row in scorecard.watched_states
        ],
        "images": {
            "steps": scorecard.images.steps,
            "steps_with_image": scorecard.images.steps_with_image,
            "images_sent": scorecard.images.images_sent,
            "captures_recorded": scorecard.images.captures_recorded,
            "captures_delivered": scorecard.images.captures_delivered,
            "captures_withheld": scorecard.images.captures_withheld,
            "withheld_by_reason": dict(scorecard.images.withheld_by_reason),
            "capture_paths": dict(scorecard.images.capture_paths),
            "capture_failed_events": scorecard.images.capture_failed_events,
            "image_withheld_events": scorecard.images.image_withheld_events,
        },
        "runs": [
            {
                "run_id": row.run_id,
                "lifecycle_state": row.lifecycle_state,
                "started_at": row.started_at,
                "models_served": list(row.models_served),
                "turns_recorded": row.turns_recorded,
                "highest_turn": row.highest_turn,
                "attempts": row.attempts,
                "steps": row.steps,
                "model_calls": row.model_calls,
                "cost_usd": row.cost_usd,
                "record_completeness": row.record_completeness,
                "has_gaps": row.has_gaps,
                "comparability": row.comparability,
                "capture_path": row.capture_path,
                "actions_applied": row.actions_applied,
                "unknown_screen_events": row.unknown_screen_events,
            }
            for row in scorecard.runs
        ],
        "never_demonstrated": {key: list(value) for key, value in never.items()},
        "unattested": [finding.as_dict() for finding in scorecard.unattested],
        "unclaimed_actions": dict(scorecard.unclaimed_actions),
        "unclaimed_observations": dict(scorecard.unclaimed_observations),
        "unclaimed_screens": dict(scorecard.unclaimed_screens),
    }
